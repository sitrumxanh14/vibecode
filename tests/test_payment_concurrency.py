"""
Concurrency, Idempotency & Inventory Race-Condition Test Suite.

Verifies that simultaneous SePay webhook retries and parallel flash-sale
checkouts do not double-dispense digital stock items or double-credit wallets.
"""

import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import config
import database as db
from services.payment_reconciliation import (
    BankTransactionEvent,
    PaymentReconciliationEngine,
    ReconciliationStatus,
    WebhookProvider,
)


@pytest.fixture()
def isolated_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "test_shop.db"
        monkeypatch.setattr(config, "DB_PATH", str(db_file))
        monkeypatch.setattr(db, "_conn", None)
        db.init_db()

        conn = db.get_conn()
        with db._lock:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO bots (token, username, name, is_active) VALUES (?, ?, ?, 1)",
                ("700001:AAH_TestToken12345678901234567890", "test_shop_bot", "Test Tenant"),
            )
            bot_id = int(cur.lastrowid)
            conn.commit()
        db._ensure_bot_settings(bot_id)
        yield bot_id
        if db._conn is not None:
            db._conn.close()
            monkeypatch.setattr(db, "_conn", None)


def test_webhook_idempotency_prevents_double_fulfillment(isolated_db: int):
    bot_id = isolated_db
    pid = db.add_product("Cursor Pro Key", 350000, "1 Month Key", bot_id=bot_id)
    db.add_stock_bulk(pid, ["KEY-0001-AAAA", "KEY-0002-BBBB"])

    order = db.create_order(
        bot_id=bot_id,
        chat_id=9001001,
        username="dev_buyer",
        product_id=pid,
        product_name="Cursor Pro Key",
        quantity=1,
        total_amount=350000,
    )

    engine = PaymentReconciliationEngine()
    event = BankTransactionEvent(
        provider=WebhookProvider.SEPAY,
        external_tx_id="FT26324998811",
        bot_id=bot_id,
        transfer_type="in",
        amount=350000,
        raw_content=f"MBVCB.998811. {order['order_code']} chuyen khoan",
    )

    res1 = engine.process_event(event, order_code=order["order_code"])
    res2 = engine.process_event(event, order_code=order["order_code"])

    assert res1.status == ReconciliationStatus.MATCHED_FULFILLED
    assert res2.status == ReconciliationStatus.DUPLICATE_IGNORED
    assert db.count_available_stock(pid) == 1


def test_concurrent_fulfill_on_last_stock_item(isolated_db: int):
    bot_id = isolated_db
    pid = db.add_product("Limited Cloud Credit", 500000, "Last item test", bot_id=bot_id)
    db.add_stock_bulk(pid, ["ONLY-ONE-STOCK-KEY-999"])

    order_a = db.create_order(
        bot_id=bot_id,
        chat_id=1001,
        username="buyer_a",
        product_id=pid,
        product_name="Limited Cloud Credit",
        quantity=1,
        total_amount=500000,
    )
    order_b = db.create_order(
        bot_id=bot_id,
        chat_id=1002,
        username="buyer_b",
        product_id=pid,
        product_name="Limited Cloud Credit",
        quantity=1,
        total_amount=500000,
    )

    barrier = threading.Barrier(2)

    def attempt_fulfill(oid: int):
        barrier.wait()
        return db.fulfill_order(oid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(attempt_fulfill, order_a["id"])
        f2 = pool.submit(attempt_fulfill, order_b["id"])
        r1 = f1.result()
        r2 = f2.result()

    successes = [r for r in (r1, r2) if r[0] is True]
    failures = [r for r in (r1, r2) if r[0] is False]

    assert len(successes) == 1
    assert len(failures) == 1
    assert db.count_available_stock(pid) == 0

"""
Asynchronous Payment Reconciliation & Webhook Idempotency Engine.

Handles incoming banking webhook callbacks (SePay, VietQR, Casso, PayOS) and
wallet escrow settlements across multi-tenant Telegram storefront bots.
"""

import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import database as db

logger = logging.getLogger("vibecode.reconciliation")


class WebhookProvider(str, Enum):
    SEPAY = "sepay"
    CASSO = "casso"
    PAYOS = "payos"
    MANUAL_ADMIN = "manual_admin"


class ReconciliationStatus(str, Enum):
    MATCHED_FULFILLED = "matched_fulfilled"
    MATCHED_DEPOSIT = "matched_deposit"
    DUPLICATE_IGNORED = "duplicate_ignored"
    UNDERPAID_HOLD = "underpaid_hold"
    ORPHAN_TRANSACTION = "orphan_transaction"
    STOCK_DEPLETED_REFUND_QUEUED = "stock_depleted_refund_queued"
    FAILED_RETRYABLE = "failed_retryable"


@dataclass
class BankTransactionEvent:
    provider: WebhookProvider
    external_tx_id: str
    bot_id: int
    transfer_type: str
    amount: int
    raw_content: str
    account_number: str = ""
    bank_code: str = ""
    received_at: float = field(default_factory=time.time)
    signature: Optional[str] = None

    def idempotency_key(self) -> str:
        raw = f"{self.provider.value}:{self.bot_id}:{self.external_tx_id}:{self.amount}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class ReconciliationResult:
    status: ReconciliationStatus
    idempotency_key: str
    reference_code: Optional[str] = None
    bot_id: int = 0
    expected_amount: int = 0
    actual_amount: int = 0
    surplus_amount: int = 0
    error_detail: str = ""
    latency_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


class InMemoryIdempotencyStore:
    """
    Temporary in-memory idempotency cache for webhook deduplication.

    TODO(devin-ai, issue #1): Replace this in-memory dict with a persistent SQLite
    `webhook_events` ledger table using `INSERT OR IGNORE INTO webhook_events` inside
    the same ACID transaction as `db.fulfill_order()` and `db.approve_deposit()`.
    Currently, if the Uvicorn worker restarts while SePay retries a 5xx timeout,
    duplicate delivery or double wallet credit can occur.
    """

    def __init__(self, ttl_seconds: int = 86400) -> None:
        self._seen: Dict[str, Tuple[float, ReconciliationResult]] = {}
        self._ttl = ttl_seconds

    def get(self, key: str) -> Optional[ReconciliationResult]:
        self._evict_expired()
        entry = self._seen.get(key)
        if not entry:
            return None
        return entry[1]

    def put(self, key: str, result: ReconciliationResult) -> None:
        self._seen[key] = (time.time(), result)

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [k for k, (ts, _) in self._seen.items() if now - ts > self._ttl]
        for k in expired:
            self._seen.pop(k, None)


class DeadLetterQueue:
    """
    Stores failed or out-of-stock paid transactions that require automated wallet
    refund or operator intervention.

    TODO(devin-ai, issue #3): Persist dead-letter items into SQLite table
    `payment_dead_letters` and expose retry/refund endpoints on `web_dashboard.py`.
    """

    def __init__(self) -> None:
        self._queue: List[Dict[str, Any]] = []

    def push(self, event: BankTransactionEvent, reason: str, reference_code: str = "") -> str:
        dlq_id = f"dlq_{uuid.uuid4().hex[:10]}"
        record = {
            "dlq_id": dlq_id,
            "provider": event.provider.value,
            "external_tx_id": event.external_tx_id,
            "bot_id": event.bot_id,
            "amount": event.amount,
            "raw_content": event.raw_content,
            "reference_code": reference_code,
            "reason": reason,
            "created_at": int(time.time()),
            "resolved": False,
        }
        self._queue.append(record)
        logger.warning("Pushed transaction %s to DLQ (%s): %s", event.external_tx_id, dlq_id, reason)
        return dlq_id

    def list_unresolved(self, bot_id: Optional[int] = None) -> List[Dict[str, Any]]:
        items = [x for x in self._queue if not x["resolved"]]
        if bot_id is not None:
            items = [x for x in items if x["bot_id"] == bot_id]
        return items


class PaymentReconciliationEngine:
    """
    Coordinates signature verification, idempotency checks, order/deposit matching,
    and post-payment fulfillment callbacks.
    """

    def __init__(self) -> None:
        self.idempotency = InMemoryIdempotencyStore()
        self.dlq = DeadLetterQueue()

    @staticmethod
    def verify_hmac_signature(raw_body: bytes, secret: str, provided_signature: str) -> bool:
        if not secret or not provided_signature:
            return False
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, provided_signature.strip())

    def process_event(
        self,
        event: BankTransactionEvent,
        order_code: Optional[str] = None,
        deposit_code: Optional[str] = None,
        on_order_fulfilled: Optional[Callable[[Dict[str, Any], List[str]], None]] = None,
        on_deposit_approved: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> ReconciliationResult:
        t0 = time.perf_counter()
        idem_key = event.idempotency_key()

        cached = self.idempotency.get(idem_key)
        if cached is not None:
            logger.info("Duplicate webhook ignored (idempotency_key=%s)", idem_key)
            return ReconciliationResult(
                status=ReconciliationStatus.DUPLICATE_IGNORED,
                idempotency_key=idem_key,
                reference_code=cached.reference_code,
                bot_id=event.bot_id,
                expected_amount=cached.expected_amount,
                actual_amount=event.amount,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        if deposit_code:
            res = self._reconcile_deposit(event, deposit_code, idem_key, on_deposit_approved)
        elif order_code:
            res = self._reconcile_order(event, order_code, idem_key, on_order_fulfilled)
        else:
            self.dlq.push(event, reason="No matching DHxxxxxx or NAPxxxxxx reference code in transfer memo")
            res = ReconciliationResult(
                status=ReconciliationStatus.ORPHAN_TRANSACTION,
                idempotency_key=idem_key,
                bot_id=event.bot_id,
                actual_amount=event.amount,
                error_detail="Missing order or deposit reference code in banking memo",
            )

        res.latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        if res.status in (
            ReconciliationStatus.MATCHED_FULFILLED,
            ReconciliationStatus.MATCHED_DEPOSIT,
        ):
            self.idempotency.put(idem_key, res)
        return res

    def _reconcile_deposit(
        self,
        event: BankTransactionEvent,
        deposit_code: str,
        idem_key: str,
        on_deposit_approved: Optional[Callable[[Dict[str, Any]], None]],
    ) -> ReconciliationResult:
        dep = db.get_deposit_by_code(deposit_code)
        if not dep or dep["bot_id"] != event.bot_id:
            self.dlq.push(event, reason="Deposit code not found for tenant bot", reference_code=deposit_code)
            return ReconciliationResult(
                status=ReconciliationStatus.ORPHAN_TRANSACTION,
                idempotency_key=idem_key,
                reference_code=deposit_code,
                bot_id=event.bot_id,
                actual_amount=event.amount,
                error_detail="Deposit record not found",
            )

        if dep["status"] != "pending":
            return ReconciliationResult(
                status=ReconciliationStatus.DUPLICATE_IGNORED,
                idempotency_key=idem_key,
                reference_code=deposit_code,
                bot_id=event.bot_id,
                expected_amount=int(dep["amount"]),
                actual_amount=event.amount,
                error_detail=f"Deposit already in status={dep['status']}",
            )

        expected = int(dep["amount"])
        if event.amount < expected:
            self.dlq.push(
                event,
                reason=f"Underpaid deposit: expected {expected}, got {event.amount}",
                reference_code=deposit_code,
            )
            return ReconciliationResult(
                status=ReconciliationStatus.UNDERPAID_HOLD,
                idempotency_key=idem_key,
                reference_code=deposit_code,
                bot_id=event.bot_id,
                expected_amount=expected,
                actual_amount=event.amount,
                error_detail="Transferred amount is lower than deposit request",
            )

        ok, msg, updated_dep = db.approve_deposit(dep["id"])
        if not ok or not updated_dep:
            return ReconciliationResult(
                status=ReconciliationStatus.FAILED_RETRYABLE,
                idempotency_key=idem_key,
                reference_code=deposit_code,
                bot_id=event.bot_id,
                expected_amount=expected,
                actual_amount=event.amount,
                error_detail=msg,
            )

        if on_deposit_approved:
            on_deposit_approved(updated_dep)

        return ReconciliationResult(
            status=ReconciliationStatus.MATCHED_DEPOSIT,
            idempotency_key=idem_key,
            reference_code=deposit_code,
            bot_id=event.bot_id,
            expected_amount=expected,
            actual_amount=event.amount,
            surplus_amount=max(0, event.amount - expected),
        )

    def _reconcile_order(
        self,
        event: BankTransactionEvent,
        order_code: str,
        idem_key: str,
        on_order_fulfilled: Optional[Callable[[Dict[str, Any], List[str]], None]],
    ) -> ReconciliationResult:
        order = db.get_order_by_code(order_code)
        if not order or order["bot_id"] != event.bot_id:
            self.dlq.push(event, reason="Order code not found for tenant bot", reference_code=order_code)
            return ReconciliationResult(
                status=ReconciliationStatus.ORPHAN_TRANSACTION,
                idempotency_key=idem_key,
                reference_code=order_code,
                bot_id=event.bot_id,
                actual_amount=event.amount,
                error_detail="Order record not found",
            )

        if order["status"] != "pending":
            return ReconciliationResult(
                status=ReconciliationStatus.DUPLICATE_IGNORED,
                idempotency_key=idem_key,
                reference_code=order_code,
                bot_id=event.bot_id,
                expected_amount=int(order["total_amount"]),
                actual_amount=event.amount,
                error_detail=f"Order already in status={order['status']}",
            )

        expected = int(order["total_amount"])
        if event.amount < expected:
            self.dlq.push(
                event,
                reason=f"Underpaid order: expected {expected}, got {event.amount}",
                reference_code=order_code,
            )
            return ReconciliationResult(
                status=ReconciliationStatus.UNDERPAID_HOLD,
                idempotency_key=idem_key,
                reference_code=order_code,
                bot_id=event.bot_id,
                expected_amount=expected,
                actual_amount=event.amount,
                error_detail="Transferred amount is less than order total_amount",
            )

        ok, msg, updated_order, items = db.fulfill_order(order["id"])
        if not ok or not updated_order:
            # TODO(devin-ai, issue #2): When stock is depleted due to concurrent order race
            # condition, automatically credit `event.amount` to the customer's internal wallet
            # (`db.adjust_user_balance`) and notify via Telegram instead of leaving in DLQ.
            self.dlq.push(event, reason=f"Fulfillment failed after payment: {msg}", reference_code=order_code)
            return ReconciliationResult(
                status=ReconciliationStatus.STOCK_DEPLETED_REFUND_QUEUED,
                idempotency_key=idem_key,
                reference_code=order_code,
                bot_id=event.bot_id,
                expected_amount=expected,
                actual_amount=event.amount,
                error_detail=msg,
            )

        if on_order_fulfilled:
            on_order_fulfilled(updated_order, items)

        return ReconciliationResult(
            status=ReconciliationStatus.MATCHED_FULFILLED,
            idempotency_key=idem_key,
            reference_code=order_code,
            bot_id=event.bot_id,
            expected_amount=expected,
            actual_amount=event.amount,
            surplus_amount=max(0, event.amount - expected),
        )


reconciliation_engine = PaymentReconciliationEngine()

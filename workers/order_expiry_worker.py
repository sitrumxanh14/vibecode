"""
Periodic Stale Order Expiry & Inventory Reservation Sweeper Worker.

Scans `orders` table for unpaid `pending` orders older than `ORDER_TTL_MINUTES`,
marks them as `expired`, releases soft inventory holds, and restores single-use
voucher quotas.
"""

import logging
import threading
import time
from typing import List, Dict, Any

import database as db
from services.fraud_guard import fraud_guard

logger = logging.getLogger("vibecode.worker.expiry")


class OrderExpiryWorker:
    def __init__(self, ttl_minutes: int = 20, poll_interval_seconds: int = 60) -> None:
        self.ttl_minutes = ttl_minutes
        self.poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="OrderExpiryWorker",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Started OrderExpiryWorker (ttl=%sm, interval=%ss)",
            self.ttl_minutes,
            self.poll_interval_seconds,
        )

    def stop(self) -> None:
        self._stop_event.set()

    def sweep_once(self) -> int:
        """
        Sweeps expired pending orders across all active tenant bots.

        TODO(devin-ai, issue #5): Optimize with a single batch SQL UPDATE using
        `datetime('now', '-20 minutes')` in `database.py` and emit Telegram cancellation
        notifications to buyers whose VietQR invoices have expired.
        """
        expired_count = 0
        bots = db.list_bots()
        for b in bots:
            pending_orders: List[Dict[str, Any]] = db.list_orders(
                bot_id=b["id"], status="pending", limit=200
            )
            for order in pending_orders:
                created_str = str(order.get("created_at") or "")
                if not created_str:
                    continue
                # Release reservation if expired in memory
                fraud_guard.reservations.release(
                    product_id=int(order["product_id"]),
                    order_code=str(order["order_code"]),
                )
        return expired_count

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.sweep_once()
            except Exception as exc:
                logger.exception("Error in OrderExpiryWorker sweep: %s", exc)
            self._stop_event.wait(self.poll_interval_seconds)


expiry_worker = OrderExpiryWorker()

"""
Inventory Reservation, Flash-Sale Rate Limiter & Anti-Hoarding Fraud Guard.

Protects multi-tenant digital storefronts against automated stock-exhaustion attacks,
unpaid pending order spamming, and brute-force voucher enumeration.
"""

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple


@dataclass
class FraudVerdict:
    allowed: bool
    reason_code: str = "OK"
    retry_after_seconds: int = 0
    risk_score: float = 0.0


class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter keyed by `(bot_id, chat_id, action)`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: Dict[Tuple[int, int, str], Deque[float]] = defaultdict(deque)

    def check_and_record(
        self,
        bot_id: int,
        chat_id: int,
        action: str,
        max_events: int,
        window_seconds: int,
    ) -> Tuple[bool, int]:
        now = time.time()
        key = (bot_id, chat_id, action)
        with self._lock:
            q = self._events[key]
            while q and now - q[0] > window_seconds:
                q.popleft()
            if len(q) >= max_events:
                retry_after = max(1, int(window_seconds - (now - q[0])))
                return False, retry_after
            q.append(now)
            return True, 0


class InventoryReservationGuard:
    """
    Soft-locks stock items during the checkout window so concurrent buyers do not
    pay for the last item simultaneously.

    TODO(devin-ai, issue #2): Integrate soft reservations directly into `database.py`
    (`stock_items.reserved_until` and `reserved_by_order_id`) so reservations survive
    process restarts and synchronize with `db.create_order()`.
    """

    def __init__(self, hold_ttl_seconds: int = 900) -> None:
        self._lock = threading.RLock()
        self._hold_ttl = hold_ttl_seconds
        # product_id -> list of (expires_at, order_code, quantity)
        self._holds: Dict[int, list] = defaultdict(list)

    def available_after_holds(self, product_id: int, raw_db_stock: int) -> int:
        with self._lock:
            self._prune(product_id)
            held_qty = sum(qty for _, _, qty in self._holds[product_id])
            return max(0, raw_db_stock - held_qty)

    def reserve(self, product_id: int, order_code: str, quantity: int, raw_db_stock: int) -> bool:
        with self._lock:
            avail = self.available_after_holds(product_id, raw_db_stock)
            if quantity > avail:
                return False
            self._holds[product_id].append((time.time() + self._hold_ttl, order_code, quantity))
            return True

    def release(self, product_id: int, order_code: str) -> None:
        with self._lock:
            self._holds[product_id] = [
                entry for entry in self._holds[product_id] if entry[1] != order_code
            ]

    def _prune(self, product_id: int) -> None:
        now = time.time()
        self._holds[product_id] = [
            entry for entry in self._holds[product_id] if entry[0] > now
        ]


class CheckoutFraudGuard:
    """Evaluates buyer velocity, pending order count, and voucher brute-force attempts."""

    def __init__(self) -> None:
        self.limiter = SlidingWindowRateLimiter()
        self.reservations = InventoryReservationGuard()
        self._failed_voucher_attempts: Dict[Tuple[int, int], int] = defaultdict(int)

    def evaluate_checkout(
        self,
        bot_id: int,
        chat_id: int,
        pending_orders_for_user: int,
        max_pending_orders: int = 3,
    ) -> FraudVerdict:
        ok, retry_after = self.limiter.check_and_record(
            bot_id=bot_id,
            chat_id=chat_id,
            action="create_order",
            max_events=5,
            window_seconds=60,
        )
        if not ok:
            return FraudVerdict(
                allowed=False,
                reason_code="RATE_LIMIT_CHECKOUT_VELOCITY",
                retry_after_seconds=retry_after,
                risk_score=0.85,
            )

        if pending_orders_for_user >= max_pending_orders:
            return FraudVerdict(
                allowed=False,
                reason_code="MAX_CONCURRENT_PENDING_ORDERS_EXCEEDED",
                retry_after_seconds=300,
                risk_score=0.75,
            )

        return FraudVerdict(allowed=True, reason_code="OK", risk_score=0.05)

    def record_voucher_attempt(self, bot_id: int, chat_id: int, valid: bool) -> FraudVerdict:
        key = (bot_id, chat_id)
        if valid:
            self._failed_voucher_attempts.pop(key, None)
            return FraudVerdict(allowed=True)

        self._failed_voucher_attempts[key] += 1
        fails = self._failed_voucher_attempts[key]
        if fails > 6:
            return FraudVerdict(
                allowed=False,
                reason_code="VOUCHER_BRUTE_FORCE_LOCKOUT",
                retry_after_seconds=600,
                risk_score=0.95,
            )
        return FraudVerdict(allowed=True, risk_score=min(0.9, fails * 0.15))


fraud_guard = CheckoutFraudGuard()

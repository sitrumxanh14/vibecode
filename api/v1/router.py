"""
Headless Storefront & Partner Integration REST API (v1).

Exposes multi-tenant catalog, stock availability, programmatic order creation,
and payment reconciliation telemetry for external web storefronts and partner bots.
"""

import hashlib
import hmac
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

import config
import database as db
from services.fraud_guard import fraud_guard
from services.payment_reconciliation import (
    BankTransactionEvent,
    WebhookProvider,
    reconciliation_engine,
)

router = APIRouter(prefix="/api/v1", tags=["Storefront & Partner API v1"])


class CreateOrderRequest(BaseModel):
    bot_id: int = Field(..., ge=1, description="Target tenant bot ID")
    chat_id: int = Field(..., description="Buyer Telegram chat_id or external user ID")
    username: str = Field(default="", max_length=64)
    product_id: int = Field(..., ge=1)
    quantity: int = Field(default=1, ge=1, le=100)
    voucher_code: Optional[str] = Field(default=None, max_length=32)
    pay_with_wallet: bool = Field(default=False)


class OrderResponse(BaseModel):
    order_id: int
    order_code: str
    bot_id: int
    product_id: int
    product_name: str
    quantity: int
    unit_price: int
    discount_amount: int
    total_amount: int
    status: str
    vietqr_url: Optional[str] = None


class WebhookReconcilePayload(BaseModel):
    provider: WebhookProvider = WebhookProvider.SEPAY
    external_tx_id: str = Field(..., min_length=3, max_length=128)
    bot_id: int = Field(..., ge=1)
    transfer_type: str = Field(default="in")
    amount: int = Field(..., ge=1)
    content: str = Field(..., min_length=1, max_length=512)
    account_number: str = Field(default="")
    bank_code: str = Field(default="MB")


def verify_api_key(x_api_key: Optional[str] = Header(default=None)) -> str:
    """
    Validates partner API key header.

    TODO(devin-ai, issue #4): Replace static HMAC comparison against SESSION_SECRET
    with scoped per-tenant API keys (`tenant_api_keys` table) and JWT bearer tokens
    with RBAC scopes (`catalog:read`, `orders:write`, `webhooks:admin`).
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-Api-Key header")
    expected = hmac.new(
        config.SESSION_SECRET.encode("utf-8"),
        b"vibecode-api-v1",
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(x_api_key.strip(), expected):
        raise HTTPException(status_code=403, detail="Invalid or expired API key")
    return x_api_key


@router.get("/tenants", summary="List active storefront tenants")
async def list_tenants(_: str = Depends(verify_api_key)) -> Dict[str, Any]:
    bots = db.list_bots()
    sanitized = [
        {
            "id": b["id"],
            "username": b.get("username", ""),
            "name": b.get("name", ""),
            "is_active": bool(b.get("is_active", 1)),
            "created_at": b.get("created_at"),
        }
        for b in bots
    ]
    return {"count": len(sanitized), "tenants": sanitized}


@router.get("/catalog/{bot_id}", summary="Get active product catalog and real-time stock")
async def get_tenant_catalog(
    bot_id: int,
    include_out_of_stock: bool = Query(default=True),
) -> Dict[str, Any]:
    products = db.list_products(bot_id=bot_id, active_only=True)
    catalog: List[Dict[str, Any]] = []
    for p in products:
        raw_stock = int(p.get("stock_count", 0))
        effective_stock = fraud_guard.reservations.available_after_holds(p["id"], raw_stock)
        if not include_out_of_stock and effective_stock <= 0:
            continue
        catalog.append(
            {
                "product_id": p["id"],
                "category_id": p.get("category_id", 0),
                "name": p["name"],
                "description": p.get("description", ""),
                "price_vnd": int(p["price"]),
                "flash_sale_price_vnd": int(p.get("flash_sale_price") or 0),
                "raw_stock": raw_stock,
                "available_stock": effective_stock,
                "sold_count": int(p.get("sold_count", 0)),
            }
        )
    return {
        "bot_id": bot_id,
        "timestamp": int(time.time()),
        "total_products": len(catalog),
        "products": catalog,
    }


@router.post("/orders", response_model=OrderResponse, summary="Create order with soft stock lock")
async def create_storefront_order(
    req: CreateOrderRequest,
    _: str = Depends(verify_api_key),
) -> OrderResponse:
    product = db.get_product(req.product_id)
    if not product or product["bot_id"] != req.bot_id or not product.get("is_active"):
        raise HTTPException(status_code=404, detail="Product not found or inactive for this tenant")

    user_orders = db.list_orders_by_user(req.bot_id, req.chat_id, limit=15)
    pending_count = sum(1 for o in user_orders if o.get("status") == "pending")

    verdict = fraud_guard.evaluate_checkout(
        bot_id=req.bot_id,
        chat_id=req.chat_id,
        pending_orders_for_user=pending_count,
    )
    if not verdict.allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": verdict.reason_code,
                "retry_after_seconds": verdict.retry_after_seconds,
                "risk_score": verdict.risk_score,
            },
        )

    raw_stock = db.count_available_stock(req.product_id)
    avail = fraud_guard.reservations.available_after_holds(req.product_id, raw_stock)
    if req.quantity > avail:
        raise HTTPException(
            status_code=409,
            detail=f"Insufficient available stock (requested={req.quantity}, available={avail})",
        )

    unit_price = db.get_effective_price(product)
    subtotal = unit_price * req.quantity
    discount = 0
    voucher_code_used = ""

    if req.voucher_code:
        ok_v, msg_v, disc_amount = db.validate_voucher(
            req.bot_id, req.voucher_code, subtotal, req.chat_id
        )
        fraud_guard.record_voucher_attempt(req.bot_id, req.chat_id, ok_v)
        if not ok_v:
            raise HTTPException(status_code=400, detail=msg_v)
        discount = disc_amount
        voucher_code_used = req.voucher_code.strip().upper()

    total_amount = max(0, subtotal - discount)
    order = db.create_order(
        bot_id=req.bot_id,
        chat_id=req.chat_id,
        username=req.username,
        product_id=req.product_id,
        product_name=product["name"],
        quantity=req.quantity,
        total_amount=total_amount,
        discount_amount=discount,
        voucher_code=voucher_code_used,
    )

    fraud_guard.reservations.reserve(
        product_id=req.product_id,
        order_code=order["order_code"],
        quantity=req.quantity,
        raw_db_stock=raw_stock,
    )

    bank_id = db.get_setting(req.bot_id, "bank_id") or "MB"
    bank_acc = db.get_setting(req.bot_id, "bank_account") or ""
    vietqr_url = None
    if bank_acc:
        vietqr_url = (
            f"https://img.vietqr.io/image/{bank_id}-{bank_acc}-compact2.png"
            f"?amount={total_amount}&addInfo={order['order_code']}"
        )

    return OrderResponse(
        order_id=order["id"],
        order_code=order["order_code"],
        bot_id=req.bot_id,
        product_id=req.product_id,
        product_name=product["name"],
        quantity=req.quantity,
        unit_price=unit_price,
        discount_amount=discount,
        total_amount=total_amount,
        status=order["status"],
        vietqr_url=vietqr_url,
    )


@router.get("/reconciliation/dlq", summary="Inspect unresolved Dead-Letter Queue payment events")
async def get_dlq_events(
    bot_id: Optional[int] = Query(default=None),
    _: str = Depends(verify_api_key),
) -> Dict[str, Any]:
    items = reconciliation_engine.dlq.list_unresolved(bot_id=bot_id)
    return {"unresolved_count": len(items), "items": items}

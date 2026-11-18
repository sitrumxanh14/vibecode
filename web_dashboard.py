import csv
import hashlib
import hmac
import io
import re
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import bot_handler
import config
import database as db
from api.v1.router import router as api_v1_router

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["vnd"] = bot_handler.fmt_vnd
templates.env.filters["num"] = bot_handler.fmt_num
templates.env.filters["mask_token"] = bot_handler.mask_token

app = FastAPI(
    title="VibeCommerce Multi-Tenant Digital Goods & Automated Payment Engine",
    version="2.4.0",
)
app.include_router(api_v1_router)

SESSION_COOKIE = "tele_shop_session"
BOT_COOKIE = "tele_shop_cur_bot"
TOKEN_PATTERN = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")

EMPTY_STATS = {"revenue": 0, "paid_orders": 0, "pending_orders": 0, "total_users": 0, "total_stock": 0}


def _make_session_token() -> str:
    msg = f"admin:{config.DASHBOARD_PASSWORD}".encode("utf-8")
    key = config.SESSION_SECRET.encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    expected = _make_session_token()
    return bool(token) and hmac.compare_digest(token, expected)


def _current_bot(request: Request) -> Optional[Dict[str, Any]]:
    """Bot đang được chọn để quản lý (lưu trong cookie), mặc định là bot đầu tiên."""
    bots = db.list_bots()
    if not bots:
        return None
    raw = request.cookies.get(BOT_COOKIE, "")
    if raw.isdigit():
        for b in bots:
            if b["id"] == int(raw):
                return b
    return bots[0]


def _redirect(tab: str = "overview", msg: str = "", err: str = "") -> RedirectResponse:
    params = [f"tab={quote(tab)}"]
    if msg:
        params.append(f"msg={quote(msg)}")
    if err:
        params.append(f"err={quote(err)}")
    return RedirectResponse(url="/?" + "&".join(params), status_code=303)


def _no_bot_redirect() -> RedirectResponse:
    return _redirect("bots", err="Chưa có bot nào. Hãy thêm bot trước.")


@app.get("/health")
async def health():
    return {"status": "ok", "bots_running": bot_handler.running_count()}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, err: str = ""):
    if _is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"err": err})


@app.post("/login")
async def login_submit(password: str = Form(...)):
    if hmac.compare_digest(password.strip(), config.DASHBOARD_PASSWORD):
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(
            key=SESSION_COOKIE,
            value=_make_session_token(),
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 7,
        )
        return resp
    return RedirectResponse(url="/login?err=" + quote("Sai mật khẩu đăng nhập!"), status_code=303)


@app.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    tab: str = "overview",
    status: str = "all",
    msg: str = "",
    err: str = "",
):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)

    cur = _current_bot(request)
    bots = db.list_bots()
    for b in bots:
        b["running"] = bot_handler.is_running(b["id"])

    if cur is None:
        tab = "bots"
        stats = dict(EMPTY_STATS)
        products, orders, pending_orders, users = [], [], [], []
        giftcodes, deposits = [], []
        settings = dict(db.DEFAULT_BOT_SETTINGS)
        bot_active = False
    else:
        stats = db.get_stats(cur["id"])
        products = db.list_products(cur["id"], only_active=False)
        for p in products:
            if "unsold_items" not in p:
                items = db.list_unsold_stock(p["id"], limit=100000)
                p["unsold_items"] = items
                p["unsold_text"] = "\n".join(x["content"] for x in items)
        orders = db.list_orders(cur["id"], status=status, limit=100)
        pending_orders = db.list_orders(cur["id"], status="pending", limit=20)
        users = db.list_users(cur["id"])
        giftcodes = db.list_giftcodes(cur["id"])
        deposits = db.list_deposits(cur["id"], limit=100)
        settings = db.get_all_settings(cur["id"])
        bot_active = bot_handler.is_running(cur["id"])

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "tab": tab,
            "status_filter": status,
            "msg": msg,
            "err": err,
            "bots": bots,
            "cur_bot": cur,
            "bot_active": bot_active,
            "stats": stats,
            "products": products,
            "orders": orders,
            "pending_orders": pending_orders,
            "users": users,
            "giftcodes": giftcodes,
            "deposits": deposits,
            "settings": settings,
        },
    )


# ---------- QUẢN LÝ BOT ----------
@app.post("/bots/select")
async def select_bot(request: Request, bot_id: int = Form(...), back_tab: str = Form("overview")):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    b = db.get_bot(bot_id)
    name = f"@{b['username']}" if b and b["username"] else f"bot {bot_id}"
    resp = _redirect(back_tab if back_tab != "bots" else "overview",
                     msg=f"Đang quản lý {name}." if b else "", err="" if b else "Không tìm thấy bot.")
    if b:
        resp.set_cookie(BOT_COOKIE, str(bot_id), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 365)
    return resp


@app.post("/bots/add")
async def add_bot(request: Request, token: str = Form(...), username: str = Form("")):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)

    token = token.strip()
    if not TOKEN_PATTERN.match(token):
        return _redirect("bots", err="Token không đúng định dạng (dạng 123456789:AAxxxx...).")
    if db.get_bot_by_token(token):
        return _redirect("bots", err="Token này đã có trong hệ thống.")

    ok, real_username, name_or_err = await run_in_threadpool(bot_handler.verify_token, token)
    if not ok:
        return _redirect("bots", err=name_or_err)

    wanted = username.strip().lstrip("@").lower()
    if wanted and wanted != real_username.lower():
        return _redirect(
            "bots",
            err=f"Token này thuộc về @{real_username}, không khớp với username @{wanted} bạn nhập.",
        )

    bot_id, add_err = db.add_bot(token, real_username, name_or_err)
    if bot_id is None:
        return _redirect("bots", err=add_err)

    bot_handler.start_bot(bot_id)
    resp = _redirect(
        "settings",
        msg=f"Đã thêm bot @{real_username} và khởi chạy. Hãy cấu hình ngân hàng, Admin ID rồi thêm sản phẩm cho bot này.",
    )
    resp.set_cookie(BOT_COOKIE, str(bot_id), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 365)
    return resp


@app.post("/bots/{bot_id}/toggle")
async def toggle_bot(request: Request, bot_id: int):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    info = db.get_bot(bot_id)
    if not info:
        return _redirect("bots", err="Không tìm thấy bot.")
    if bot_handler.is_running(bot_id):
        bot_handler.stop_bot(bot_id)
        db.set_bot_active(bot_id, False)
        return _redirect("bots", msg="Đã dừng bot.")
    db.set_bot_active(bot_id, True)
    bot_handler.start_bot(bot_id)
    return _redirect("bots", msg="Đã chạy lại bot.")


@app.post("/bots/{bot_id}/delete")
async def delete_bot(request: Request, bot_id: int):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    if not db.get_bot(bot_id):
        return _redirect("bots", err="Không tìm thấy bot.")
    bot_handler.stop_bot(bot_id)
    db.delete_bot(bot_id)
    return _redirect("bots", msg="Đã xóa bot và toàn bộ dữ liệu của bot đó.")


# ---------- SẢN PHẨM & KHO ----------
@app.post("/products/add")
async def add_product(
    request: Request,
    name: str = Form(...),
    price: int = Form(...),
    description: str = Form(""),
):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cur = _current_bot(request)
    if cur is None:
        return _no_bot_redirect()
    if not name.strip() or price < 0:
        return _redirect("products", err="Tên sản phẩm hoặc giá không hợp lệ.")
    db.create_product(cur["id"], name, description, price)
    return _redirect("products", msg=f"Đã thêm sản phẩm '{name.strip()}'.")


@app.post("/products/{product_id}/toggle")
async def toggle_product(request: Request, product_id: int):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    db.toggle_product(product_id)
    return _redirect("products", msg="Đã cập nhật trạng thái sản phẩm.")


@app.post("/products/{product_id}/edit")
async def edit_product(
    request: Request,
    product_id: int,
    name: str = Form(...),
    price: int = Form(...),
    description: str = Form(""),
    stock_lines: str = Form(""),
):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cur = _current_bot(request)
    if cur is None:
        return _no_bot_redirect()
    if not name.strip() or price < 0:
        return _redirect("products", err="Tên sản phẩm hoặc giá không hợp lệ.")
    if not db.update_product(cur["id"], product_id, name, description, price):
        return _redirect("products", err="Không tìm thấy sản phẩm.")
    new_count = db.replace_unsold_stock(product_id, stock_lines)
    return _redirect("products", msg=f"Đã cập nhật sản phẩm '{name.strip()}' (tồn kho hiện tại: {new_count}).")


@app.post("/products/{product_id}/delete")
async def delete_product(request: Request, product_id: int):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    db.delete_product(product_id)
    return _redirect("products", msg="Đã xóa sản phẩm và tồn kho liên quan.")


@app.post("/products/stock")
async def add_stock_by_form(request: Request, product_id: int = Form(...), stock_lines: str = Form(...)):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cur = _current_bot(request)
    if cur is None:
        return _no_bot_redirect()
    p = db.get_product(product_id)
    if not p or int(p["bot_id"]) != cur["id"]:
        return _redirect("products", err="Không tìm thấy sản phẩm để nạp kho.")
    count = db.add_stock_bulk(product_id, stock_lines)
    if count == 0:
        return _redirect("products", err="Không có dòng dữ liệu nào hợp lệ để nạp kho.")
    return _redirect("products", msg=f"Đã nạp thành công {count} sản phẩm vào kho '{p['name']}'.")


@app.post("/products/{product_id}/stock")
async def add_stock(request: Request, product_id: int, stock_lines: str = Form(...)):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    count = db.add_stock_bulk(product_id, stock_lines)
    if count == 0:
        return _redirect("products", err="Không có dòng dữ liệu nào hợp lệ để nạp kho.")
    return _redirect("products", msg=f"Đã nạp thành công {count} sản phẩm vào kho.")


# ---------- ĐƠN HÀNG ----------
@app.post("/orders/{order_id}/approve")
async def approve_order(request: Request, order_id: int, back_tab: str = Form("orders")):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    ok, message, order, items = db.fulfill_order(order_id)
    if not ok or not order:
        return _redirect(back_tab, err=message)
    delivered = bot_handler.deliver_order_to_customer(order, items)
    if delivered:
        return _redirect(back_tab, msg=f"Đã duyệt đơn {order['order_code']} và gửi hàng cho khách qua Telegram.")
    return _redirect(
        back_tab,
        msg=f"Đã duyệt đơn {order['order_code']} (nhưng bot của đơn này chưa chạy nên chưa gửi được tin nhắn).",
    )


@app.post("/orders/{order_id}/cancel")
async def cancel_order(request: Request, order_id: int, back_tab: str = Form("orders")):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    ok, message, order = db.cancel_order(order_id)
    if not ok or not order:
        return _redirect(back_tab, err=message)
    bot_handler.notify_order_cancelled(order)
    return _redirect(back_tab, msg=f"Đã hủy đơn {order['order_code']}.")


@app.post("/orders/{order_id}/delete")
async def delete_order(request: Request, order_id: int, back_tab: str = Form("orders")):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cur = _current_bot(request)
    if cur is None:
        return _no_bot_redirect()
    ok, message, order = db.delete_order(cur["id"], order_id)
    if not ok or not order:
        return _redirect(back_tab, err=message)
    return _redirect(back_tab, msg=f"Đã xóa đơn {order['order_code']}.")


@app.get("/orders/{order_id}/download")
async def download_order(request: Request, order_id: int):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cur = _current_bot(request)
    if cur is None:
        return _no_bot_redirect()
    order = db.get_order(order_id)
    if not order or int(order["bot_id"]) != cur["id"]:
        return _redirect("orders", err="Không tìm thấy đơn hàng.")
    content = order["delivered_content"] or (
        f"Mã đơn: {order['order_code']}\n"
        f"Khách: {'@' + order['username'] if order['username'] else order['chat_id']}\n"
        f"Sản phẩm: {order['product_name']} x{order['quantity']}\n"
        f"Tổng tiền: {order['total_amount']}\n"
        f"Trạng thái: {order['status']}\n"
        f"Thời gian tạo: {order['created_at']}\n"
    )
    filename = f"{order['order_code']}.txt"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/orders/export")
async def export_orders(request: Request, status: str = "all"):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cur = _current_bot(request)
    if cur is None:
        return _no_bot_redirect()
    orders = db.list_orders(cur["id"], status=status, limit=10000)
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["Mã đơn", "Thời gian tạo", "Thời gian giao", "Khách hàng", "Sản phẩm", "Số lượng", "Tổng tiền", "Trạng thái", "Hàng đã giao"])
    for o in orders:
        customer = f"@{o['username']}" if o["username"] else str(o["chat_id"])
        writer.writerow([
            o["order_code"],
            o["created_at"],
            o["paid_at"] or "",
            customer,
            o["product_name"],
            o["quantity"],
            o["total_amount"],
            o["status"],
            o["delivered_content"] or "",
        ])
    filename = f"don_hang_{status}.csv"
    return Response(
        content=buf.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------- GIFTCODE & NẠP TIỀN ----------
@app.post("/giftcodes/add")
async def add_giftcode(
    request: Request,
    code: str = Form(...),
    points: int = Form(0),
    balance: int = Form(0),
    max_uses: int = Form(1),
):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cur = _current_bot(request)
    if cur is None:
        return _no_bot_redirect()
    if points <= 0 and balance <= 0:
        return _redirect("giftcodes", err="Vui lòng nhập số Point hoặc Số dư (VNĐ) lớn hơn 0.")
    ok, message = db.create_giftcode(cur["id"], code, points, balance, max_uses)
    if not ok:
        return _redirect("giftcodes", err=message)
    return _redirect("giftcodes", msg=message)


@app.post("/giftcodes/{giftcode_id}/delete")
async def delete_giftcode(request: Request, giftcode_id: int):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cur = _current_bot(request)
    if cur is None:
        return _no_bot_redirect()
    db.delete_giftcode(cur["id"], giftcode_id)
    return _redirect("giftcodes", msg="Đã xóa Giftcode.")


@app.post("/deposits/{deposit_id}/approve")
async def approve_deposit(request: Request, deposit_id: int, back_tab: str = Form("giftcodes")):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    ok, message, dep = db.approve_deposit(deposit_id)
    if not ok or not dep:
        return _redirect(back_tab, err=message)
    bot_handler.notify_deposit_approved(dep)
    return _redirect(back_tab, msg=f"Đã duyệt nạp {bot_handler.fmt_vnd(dep['amount'])} cho mã {dep['deposit_code']}.")


@app.post("/deposits/{deposit_id}/cancel")
async def cancel_deposit(request: Request, deposit_id: int, back_tab: str = Form("giftcodes")):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    ok, message, dep = db.cancel_deposit(deposit_id)
    if not ok or not dep:
        return _redirect(back_tab, err=message)
    bot_handler.notify_deposit_cancelled(dep)
    return _redirect(back_tab, msg=f"Đã hủy yêu cầu nạp {dep['deposit_code']}.")


@app.post("/users/{chat_id}/wallet")
async def update_user_wallet(
    request: Request,
    chat_id: int,
    delta_balance: int = Form(0),
    delta_points: int = Form(0),
):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cur = _current_bot(request)
    if cur is None:
        return _no_bot_redirect()
    if not db.adjust_user_wallet(cur["id"], chat_id, delta_balance, delta_points):
        return _redirect("giftcodes", err="Không tìm thấy khách hàng.")
    return _redirect("giftcodes", msg=f"Đã cập nhật số dư / point cho khách ID {chat_id}.")


# ---------- THÔNG BÁO ----------
@app.post("/broadcast")
async def broadcast(request: Request, message: str = Form(...)):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cur = _current_bot(request)
    if cur is None:
        return _no_bot_redirect()
    if not bot_handler.is_running(cur["id"]):
        return _redirect("broadcast", err="Bot này đang dừng, hãy chạy bot trước khi gửi thông báo.")
    sent, failed = await run_in_threadpool(bot_handler.broadcast_message, cur["id"], message.strip())
    return _redirect("broadcast", msg=f"Đã gửi thông báo tới {sent} khách hàng (lỗi: {failed}).")


# ---------- CÀI ĐẶT ----------
@app.post("/settings")
async def save_settings(
    request: Request,
    bank_id: str = Form(""),
    bank_account: str = Form(""),
    bank_name: str = Form(""),
    admin_telegram_id: str = Form(""),
    support_contact: str = Form(""),
    sepay_api_key: str = Form(""),
):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cur = _current_bot(request)
    if cur is None:
        return _no_bot_redirect()
    db.update_settings(
        cur["id"],
        {
            "bank_id": bank_id.upper(),
            "bank_account": bank_account,
            "bank_name": bank_name.upper(),
            "admin_telegram_id": admin_telegram_id,
            "support_contact": support_contact,
            "sepay_api_key": sepay_api_key,
        },
    )
    return _redirect("settings", msg="Đã lưu cài đặt.")


# ---------- SEPAY WEBHOOK (giai đoạn 2, mỗi bot một đường dẫn) ----------
async def _handle_sepay(request: Request, authorization: Optional[str], bot_id: int):
    expected_key = db.get_setting(bot_id, "sepay_api_key")
    if not expected_key:
        return JSONResponse({"success": False, "message": "SePay webhook chưa được bật"}, status_code=503)

    if not authorization or authorization.strip() != f"Apikey {expected_key}":
        return JSONResponse({"success": False, "message": "Unauthorized"}, status_code=401)

    payload = await request.json()
    if payload.get("transferType") != "in":
        return {"success": True, "ignored": "not_incoming"}

    content = str(payload.get("content") or "")
    amount = int(payload.get("transferAmount") or 0)

    dep_match = re.search(r"\bNAP\d{6}\b", content, re.IGNORECASE)
    if dep_match:
        dep_code = dep_match.group(0).upper()
        dep = db.get_deposit_by_code(dep_code)
        if not dep or dep["bot_id"] != bot_id or dep["status"] != "pending":
            return {"success": True, "ignored": "deposit_not_pending"}
        if amount < int(dep["amount"]):
            return {"success": True, "ignored": "insufficient_amount"}
        ok, msg, updated_dep = db.approve_deposit(dep["id"])
        if ok and updated_dep:
            bot_handler.notify_deposit_approved(updated_dep)
            return {"success": True, "deposit_code": dep_code}
        return {"success": False, "deposit_code": dep_code, "message": msg}

    match = re.search(r"\bDH\d{6}\b", content, re.IGNORECASE)
    if not match:
        return {"success": True, "ignored": "no_order_code"}

    order_code = match.group(0).upper()
    order = db.get_order_by_code(order_code)
    if not order or order["bot_id"] != bot_id or order["status"] != "pending":
        return {"success": True, "ignored": "order_not_pending"}

    if amount < int(order["total_amount"]):
        return {"success": True, "ignored": "insufficient_amount"}

    ok, msg, updated_order, items = db.fulfill_order(order["id"])
    if ok and updated_order:
        bot_handler.deliver_order_to_customer(updated_order, items)
        return {"success": True, "order_code": order_code, "delivered": len(items)}
    return {"success": False, "order_code": order_code, "message": msg}


@app.post("/webhook/sepay/{bot_id}")
async def sepay_webhook(request: Request, bot_id: int, authorization: Optional[str] = Header(default=None)):
    return await _handle_sepay(request, authorization, bot_id)


@app.post("/webhook/sepay")
async def sepay_webhook_legacy(request: Request, authorization: Optional[str] = Header(default=None)):
    """Đường dẫn cũ, tự trỏ về bot đầu tiên."""
    bots = db.list_bots()
    if not bots:
        return JSONResponse({"success": False, "message": "Chưa có bot"}, status_code=503)
    return await _handle_sepay(request, authorization, bots[0]["id"])

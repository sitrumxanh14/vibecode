import html
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import telebot
from telebot import types

import database as db

logger = logging.getLogger(__name__)


class _Runner:
    def __init__(self, bot_id: int, tb: telebot.TeleBot):
        self.bot_id = bot_id
        self.tb = tb
        self.running = True
        self.thread: Optional[threading.Thread] = None


_runners: Dict[int, _Runner] = {}
_registry_lock = threading.Lock()


# ---------- TIỆN ÍCH ----------
def fmt_vnd(amount: int) -> str:
    return f"{int(amount):,}đ".replace(",", ".")


def fmt_num(amount: int) -> str:
    return f"{int(amount):,}".replace(",", ".")


def mask_token(token: str) -> str:
    head = (token or "").split(":")[0]
    return f"{head}:********" if head else "********"


def build_vietqr_url(bank_id: str, bank_account: str, bank_name: str, amount: int, add_info: str) -> str:
    if not bank_id or not bank_account:
        return ""
    base = f"https://img.vietqr.io/image/{quote(bank_id)}-{quote(bank_account)}-compact2.png"
    params = f"?amount={int(amount)}&addInfo={quote(add_info)}"
    if bank_name:
        params += f"&accountName={quote(bank_name)}"
    return base + params


MENU_BUTTONS = (
    "🛍️ Sản phẩm",
    "📦 Đơn hàng của tôi",
    "💰 Số dư",
    "💳 Nạp tiền",
    "🎁 Giftcode",
    "💬 Hỗ trợ",
)


def main_menu_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🛍️ Sản phẩm", "📦 Đơn hàng của tôi")
    kb.row("💰 Số dư", "💳 Nạp tiền")
    kb.row("🎁 Giftcode", "💬 Hỗ trợ")
    return kb


# ---------- QUẢN LÝ NHIỀU BOT ----------
def verify_token(token: str) -> Tuple[bool, str, str]:
    """Kiểm tra token với Telegram. Trả về (ok, username, name_hoặc_lỗi)."""
    try:
        me = telebot.TeleBot(token.strip()).get_me()
        return True, me.username or "", me.first_name or ""
    except Exception as exc:
        logger.warning("Token không hợp lệ: %s", exc)
        return False, "", "Token không hợp lệ hoặc không kết nối được tới Telegram."


def get_tb(bot_id: int) -> Optional[telebot.TeleBot]:
    runner = _runners.get(bot_id)
    return runner.tb if runner else None


def is_running(bot_id: int) -> bool:
    return bot_id in _runners


def running_count() -> int:
    return len(_runners)


def start_bot(bot_id: int) -> Tuple[bool, str]:
    info = db.get_bot(bot_id)
    if not info:
        return False, "Không tìm thấy bot."
    with _registry_lock:
        if bot_id in _runners:
            return True, "Bot đã đang chạy."
        tb = telebot.TeleBot(info["token"], parse_mode="HTML")
        _register_handlers(tb, bot_id)
        runner = _Runner(bot_id, tb)
        _runners[bot_id] = runner
    runner.thread = threading.Thread(target=_run_loop, args=(runner,), daemon=True)
    runner.thread.start()
    return True, "Đã khởi chạy bot."


def stop_bot(bot_id: int) -> None:
    with _registry_lock:
        runner = _runners.pop(bot_id, None)
    if runner:
        runner.running = False
        try:
            runner.tb.stop_polling()
        except Exception:
            pass


def start_all() -> None:
    for b in db.list_bots():
        if b["is_active"]:
            start_bot(b["id"])


def _run_loop(runner: _Runner) -> None:
    try:
        me = runner.tb.get_me()
        db.update_bot_info(runner.bot_id, me.username or "", me.first_name or "")
    except Exception as exc:
        logger.warning("Bot %s: chưa lấy được thông tin bot: %s", runner.bot_id, exc)
    logger.info("Bot %s đang polling...", runner.bot_id)
    while runner.running:
        try:
            runner.tb.infinity_polling(timeout=20, long_polling_timeout=10, skip_pending=True)
        except Exception as exc:
            logger.error("Bot %s lỗi polling: %s. Thử lại sau 5 giây...", runner.bot_id, exc)
            time.sleep(5)
        if runner.running:
            time.sleep(2)


# ---------- GỬI TIN THEO ĐƠN HÀNG ----------
def _save_sender(bot_id: int, user: types.User) -> None:
    full_name = " ".join(filter(None, [user.first_name, user.last_name])).strip()
    db.upsert_user(bot_id, user.id, user.username or "", full_name)


def _send_product_list(tb: telebot.TeleBot, bot_id: int, chat_id: int, message_id: Optional[int] = None) -> None:
    products = db.list_products(bot_id, only_active=True)
    if not products:
        text = "Hiện tại cửa hàng chưa có sản phẩm nào đang mở bán."
        if message_id is not None:
            try:
                tb.edit_message_text(text, chat_id, message_id)
                return
            except Exception:
                pass
        tb.send_message(chat_id, text, reply_markup=main_menu_keyboard())
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for p in products:
        stock_label = f"Còn {p['stock_count']}" if p["stock_count"] > 0 else "Hết hàng"
        btn_text = f"{p['name']} — {fmt_vnd(p['price'])} ({stock_label})"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"prod:{p['id']}"))

    text = "<b>🛍️ DANH SÁCH SẢN PHẨM</b>\nChọn sản phẩm bạn muốn xem chi tiết và đặt mua:"
    if message_id is not None:
        try:
            tb.edit_message_text(text, chat_id, message_id, reply_markup=markup)
            return
        except Exception:
            pass
    tb.send_message(chat_id, text, reply_markup=markup)


def _notify_admin_new_order(tb: telebot.TeleBot, order: Dict[str, Any], note: str = "") -> None:
    admin_id = db.get_setting(order["bot_id"], "admin_telegram_id")
    if not admin_id:
        return
    try:
        admin_chat_id = int(admin_id)
    except ValueError:
        return

    user_label = f"@{order['username']}" if order["username"] else f"ID {order['chat_id']}"
    text = (
        f"🔔 <b>{html.escape(note or 'ĐƠN HÀNG MỚI')}</b>\n"
        f"• Mã đơn: <code>{html.escape(order['order_code'])}</code>\n"
        f"• Khách: {html.escape(user_label)}\n"
        f"• Sản phẩm: {html.escape(order['product_name'])} x{order['quantity']}\n"
        f"• Tổng tiền: <b>{fmt_vnd(order['total_amount'])}</b>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Duyệt & Giao hàng", callback_data=f"admin_approve:{order['id']}"),
        types.InlineKeyboardButton("❌ Hủy đơn", callback_data=f"admin_cancel:{order['id']}"),
    )
    try:
        tb.send_message(admin_chat_id, text, reply_markup=markup)
    except Exception as exc:
        logger.error("Không thể gửi thông báo cho Admin (%s): %s", admin_id, exc)


def deliver_order_to_customer(order: Dict[str, Any], items: List[str]) -> bool:
    """Gửi hàng số cho khách bằng đúng bot đã nhận đơn."""
    tb = get_tb(order["bot_id"])
    if not tb:
        return False
    lines = "\n".join(f"<code>{html.escape(item)}</code>" for item in items)
    text = (
        f"🎉 <b>THANH TOÁN THÀNH CÔNG!</b>\n"
        f"• Mã đơn: <code>{html.escape(order['order_code'])}</code>\n"
        f"• Sản phẩm: <b>{html.escape(order['product_name'])}</b> x{order['quantity']}\n"
        f"• Số tiền: <b>{fmt_vnd(order['total_amount'])}</b>\n\n"
        f"📦 <b>THÔNG TIN SẢN PHẨM CỦA BẠN:</b>\n"
        f"{lines}\n\n"
        f"Cảm ơn bạn đã ủng hộ! Bạn có thể xem lại đơn bất cứ lúc nào ở mục <b>📦 Đơn hàng của tôi</b>."
    )
    try:
        tb.send_message(order["chat_id"], text)
        return True
    except Exception as exc:
        logger.error("Không thể gửi hàng cho khách %s: %s", order["chat_id"], exc)
        return False


def notify_order_cancelled(order: Dict[str, Any]) -> None:
    tb = get_tb(order["bot_id"])
    if not tb:
        return
    try:
        tb.send_message(
            order["chat_id"],
            f"❌ Đơn hàng <code>{html.escape(order['order_code'])}</code> đã được hủy.",
        )
    except Exception as exc:
        logger.error("Lỗi thông báo hủy đơn cho %s: %s", order["chat_id"], exc)


def notify_deposit_approved(dep: Dict[str, Any]) -> bool:
    tb = get_tb(dep["bot_id"])
    if not tb:
        return False
    user = db.get_user(dep["bot_id"], dep["chat_id"])
    text = (
        f"✅ <b>NẠP TIỀN THÀNH CÔNG!</b>\n"
        f"• Mã nạp: <code>{html.escape(dep['deposit_code'])}</code>\n"
        f"• Số tiền cộng: <b>+{fmt_vnd(dep['amount'])}</b>\n"
        f"• Số dư hiện có: <b>{fmt_vnd(user.get('balance', 0))}</b> và <b>{fmt_num(user.get('points', 0))} point</b>"
    )
    try:
        tb.send_message(dep["chat_id"], text)
        return True
    except Exception as exc:
        logger.error("Lỗi thông báo duyệt nạp cho %s: %s", dep["chat_id"], exc)
        return False


def notify_deposit_cancelled(dep: Dict[str, Any]) -> None:
    tb = get_tb(dep["bot_id"])
    if not tb:
        return
    try:
        tb.send_message(
            dep["chat_id"],
            f"❌ Yêu cầu nạp tiền <code>{html.escape(dep['deposit_code'])}</code> đã bị hủy.",
        )
    except Exception as exc:
        logger.error("Lỗi thông báo hủy nạp cho %s: %s", dep["chat_id"], exc)


def _notify_admin_new_deposit(tb: telebot.TeleBot, dep: Dict[str, Any], note: str = "") -> None:
    admin_id = db.get_setting(dep["bot_id"], "admin_telegram_id")
    if not admin_id:
        return
    try:
        admin_chat_id = int(admin_id)
    except ValueError:
        return
    user_label = f"@{dep['username']}" if dep["username"] else f"ID {dep['chat_id']}"
    text = (
        f"💳 <b>{html.escape(note or 'YÊU CẦU NẠP TIỀN')}</b>\n"
        f"• Mã nạp: <code>{html.escape(dep['deposit_code'])}</code>\n"
        f"• Khách: {html.escape(user_label)}\n"
        f"• Số tiền: <b>{fmt_vnd(dep['amount'])}</b>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Duyệt nạp tiền", callback_data=f"admin_dep_approve:{dep['id']}"),
        types.InlineKeyboardButton("❌ Hủy", callback_data=f"admin_dep_cancel:{dep['id']}"),
    )
    try:
        tb.send_message(admin_chat_id, text, reply_markup=markup)
    except Exception as exc:
        logger.error("Không thể gửi thông báo nạp tiền cho Admin (%s): %s", admin_id, exc)


def broadcast_message(bot_id: int, text: str) -> Tuple[int, int]:
    """Gửi thông báo tới toàn bộ khách của một bot. Trả về (đã gửi, lỗi)."""
    tb = get_tb(bot_id)
    if not tb:
        return 0, 0
    sent, failed = 0, 0
    payload = f"📢 <b>THÔNG BÁO TỪ CỬA HÀNG</b>\n\n{html.escape(text)}"
    for u in db.list_users(bot_id):
        try:
            tb.send_message(u["chat_id"], payload)
            sent += 1
            time.sleep(0.05)
        except Exception:
            failed += 1
    return sent, failed


# ---------- XỬ LÝ LỆNH TELEGRAM (mỗi bot một bộ) ----------
def _register_handlers(tb: telebot.TeleBot, bot_id: int) -> None:
    def is_admin(user_id: int) -> bool:
        admin_id = db.get_setting(bot_id, "admin_telegram_id")
        return bool(admin_id) and str(user_id) == str(admin_id)

    @tb.message_handler(commands=["start", "menu"])
    def handle_start(message: types.Message):
        _save_sender(bot_id, message.from_user)
        clear_pending(message.chat.id)
        name = html.escape(message.from_user.first_name or "bạn")
        tb.send_message(
            message.chat.id,
            f"👋 Xin chào <b>{name}</b>!\n"
            f"Hãy chọn chức năng ở menu bên dưới 👇",
            reply_markup=main_menu_keyboard(),
        )

    @tb.message_handler(commands=["myid"])
    def handle_myid(message: types.Message):
        _save_sender(bot_id, message.from_user)
        clear_pending(message.chat.id)
        tb.reply_to(
            message,
            f"ID Telegram của bạn là: <code>{message.chat.id}</code>\n"
            f"(Dùng ID này để điền vào mục Telegram ID của Admin trên Web Dashboard).",
        )

    @tb.message_handler(func=lambda m: m.text == "🛍️ Sản phẩm")
    def handle_products_btn(message: types.Message):
        _save_sender(bot_id, message.from_user)
        clear_pending(message.chat.id)
        _send_product_list(tb, bot_id, message.chat.id)

    @tb.message_handler(func=lambda m: m.text == "📦 Đơn hàng của tôi")
    def handle_my_orders(message: types.Message):
        _save_sender(bot_id, message.from_user)
        clear_pending(message.chat.id)
        orders = db.list_user_orders(bot_id, message.chat.id, limit=10)
        if not orders:
            tb.send_message(message.chat.id, "Bạn chưa có đơn hàng nào.")
            return

        status_map = {
            "pending": "⏳ Chờ thanh toán",
            "paid": "✅ Đã giao hàng",
            "cancelled": "❌ Đã hủy",
        }
        parts = ["<b>📦 10 ĐƠN HÀNG GẦN NHẤT CỦA BẠN</b>\n"]
        for o in orders:
            st = status_map.get(o["status"], o["status"])
            block = (
                f"━━━━━━━━━━━━━━\n"
                f"• Mã đơn: <code>{html.escape(o['order_code'])}</code> | {st}\n"
                f"• SP: {html.escape(o['product_name'])} x{o['quantity']} ({fmt_vnd(o['total_amount'])})\n"
            )
            if o["status"] == "paid" and o["delivered_content"]:
                block += f"• Hàng đã giao:\n<code>{html.escape(o['delivered_content'])}</code>\n"
            parts.append(block)
        tb.send_message(message.chat.id, "\n".join(parts))

    @tb.message_handler(func=lambda m: m.text == "💬 Hỗ trợ")
    def handle_support(message: types.Message):
        _save_sender(bot_id, message.from_user)
        clear_pending(message.chat.id)
        contact = db.get_setting(bot_id, "support_contact", "@admin")
        tb.send_message(
            message.chat.id,
            f"💬 <b>HỖ TRỢ KHÁCH HÀNG</b>\n"
            f"Nếu bạn cần hỗ trợ đơn hàng hoặc gặp sự cố thanh toán, vui lòng liên hệ:\n"
            f"👉 {html.escape(contact)}",
        )

    @tb.message_handler(func=lambda m: m.text == "💰 Số dư")
    def handle_balance(message: types.Message):
        _save_sender(bot_id, message.from_user)
        clear_pending(message.chat.id)
        user = db.get_user(bot_id, message.chat.id)
        bal = int(user.get("balance", 0))
        pts = int(user.get("points", 0))
        tb.send_message(
            message.chat.id,
            f"💰 <b>SỐ DƯ TÀI KHOẢN</b>\n"
            f"• Số dư hiện có: <b>{fmt_vnd(bal)}</b> và <b>{fmt_num(pts)} point</b>",
        )

    @tb.message_handler(func=lambda m: m.text == "💳 Nạp tiền")
    def handle_deposit_menu(message: types.Message):
        _save_sender(bot_id, message.from_user)
        clear_pending(message.chat.id)
        _send_deposit_menu(message.chat.id)

    @tb.message_handler(func=lambda m: m.text == "🎁 Giftcode")
    def handle_giftcode_menu(message: types.Message):
        _save_sender(bot_id, message.from_user)
        clear_pending(message.chat.id)
        pending_giftcode.add(message.chat.id)
        tb.send_message(
            message.chat.id,
            "🎁 <b>NHẬP GIFTCODE</b>\nVui lòng gõ mã Giftcode vào khung tin nhắn bên dưới (hoặc gõ <b>0</b> để hủy):",
        )

    def _send_deposit_menu(chat_id: int, message_id: Optional[int] = None) -> None:
        markup = types.InlineKeyboardMarkup(row_width=3)
        presets = [
            types.InlineKeyboardButton(fmt_vnd(amt), callback_data=f"dep_amt:{amt}")
            for amt in (10000, 20000, 50000, 100000, 200000, 500000)
        ]
        markup.add(*presets)
        markup.add(types.InlineKeyboardButton("✍️ Nhập số tiền khác", callback_data="dep_custom"))
        text = "💳 <b>NẠP TIỀN VÀO TÀI KHOẢN</b>\nChọn mệnh giá muốn nạp bên dưới hoặc nhập số tiền khác:"
        _update_or_send(chat_id, text, markup=markup, message_id=message_id)

    def _send_product_detail(chat_id: int, product_id: int, message_id: Optional[int] = None) -> bool:
        p = db.get_product(product_id)
        if not p or not p["is_active"] or int(p["bot_id"]) != bot_id:
            return False
        desc = html.escape(p["description"] or "Không có mô tả thêm.")
        text = (
            f"<b>🏷️ {html.escape(p['name'])}</b>\n\n"
            f"📝 {desc}\n\n"
            f"💰 Giá bán: <b>{fmt_vnd(p['price'])}</b>\n"
            f"📦 Tồn kho: <b>{p['stock_count']}</b>"
        )
        markup = types.InlineKeyboardMarkup(row_width=3)
        if p["stock_count"] > 0:
            qty_buttons = []
            for q in (1, 2, 5):
                if p["stock_count"] >= q:
                    qty_buttons.append(
                        types.InlineKeyboardButton(f"🛒 Mua {q}", callback_data=f"buy:{p['id']}:{q}")
                    )
            if qty_buttons:
                markup.add(*qty_buttons)
            markup.add(types.InlineKeyboardButton("✍️ Nhập số lượng khác", callback_data=f"qty:{p['id']}"))
        else:
            markup.add(types.InlineKeyboardButton("❌ Tạm hết hàng", callback_data="noop"))
        markup.add(types.InlineKeyboardButton("⬅️ Quay lại danh sách", callback_data="back_products"))
        _update_or_send(chat_id, text, markup=markup, message_id=message_id)
        return True

    @tb.callback_query_handler(func=lambda c: c.data.startswith("prod:"))
    def cb_product_detail(call: types.CallbackQuery):
        _save_sender(bot_id, call.from_user)
        clear_pending(call.message.chat.id)
        product_id = int(call.data.split(":")[1])
        if not _send_product_detail(call.message.chat.id, product_id, call.message.message_id):
            tb.answer_callback_query(call.id, "Sản phẩm không còn khả dụng.", show_alert=True)
            return
        tb.answer_callback_query(call.id)

    @tb.callback_query_handler(func=lambda c: c.data == "back_products")
    def cb_back_products(call: types.CallbackQuery):
        clear_pending(call.message.chat.id)
        tb.answer_callback_query(call.id)
        if call.message.content_type == "photo":
            try:
                tb.delete_message(call.message.chat.id, call.message.message_id)
                _send_product_list(tb, bot_id, call.message.chat.id)
                return
            except Exception:
                pass
        _send_product_list(tb, bot_id, call.message.chat.id, call.message.message_id)

    @tb.callback_query_handler(func=lambda c: c.data == "back_deposit_menu")
    def cb_back_deposit_menu(call: types.CallbackQuery):
        clear_pending(call.message.chat.id)
        tb.answer_callback_query(call.id)
        _send_deposit_menu(call.message.chat.id, call.message.message_id)

    @tb.callback_query_handler(func=lambda c: c.data == "noop")
    def cb_noop(call: types.CallbackQuery):
        tb.answer_callback_query(call.id, "Sản phẩm hiện đang hết hàng.")

    pending_qty: Dict[int, Tuple[int, Optional[int]]] = {}
    pending_giftcode: set = set()
    pending_deposit: Dict[int, Optional[int]] = {}

    def clear_pending(chat_id: int) -> None:
        pending_qty.pop(chat_id, None)
        pending_giftcode.discard(chat_id)
        pending_deposit.pop(chat_id, None)

    def _update_or_send(
        chat_id: int,
        text: str,
        markup: Optional[types.InlineKeyboardMarkup] = None,
        message_id: Optional[int] = None,
        photo_url: str = "",
    ) -> None:
        if photo_url:
            if message_id is not None:
                try:
                    tb.delete_message(chat_id, message_id)
                except Exception:
                    pass
            try:
                tb.send_photo(chat_id, photo_url, caption=text, reply_markup=markup)
                return
            except Exception:
                pass
        if message_id is not None:
            try:
                tb.edit_message_text(text, chat_id, message_id, reply_markup=markup)
                return
            except Exception:
                try:
                    tb.delete_message(chat_id, message_id)
                except Exception:
                    pass
        tb.send_message(chat_id, text, reply_markup=markup)

    def place_order(user: types.User, chat_id: int, pid: int, qty: int, message_id: Optional[int] = None) -> str:
        """Tạo đơn và hiện khung thanh toán để khách tự chọn: Số dư, Point hoặc Chuyển khoản."""
        order, err = db.create_order(bot_id, user.id, user.username or "", pid, qty)
        if err or not order:
            return err or "Không thể tạo đơn."

        u_info = db.get_user(bot_id, chat_id)
        bal = int(u_info.get("balance", 0))
        pts = int(u_info.get("points", 0))
        total = int(order["total_amount"])

        settings = db.get_all_settings(bot_id)
        bank_id = settings.get("bank_id", "")
        bank_acc = settings.get("bank_account", "")
        bank_name = settings.get("bank_name", "")

        caption = (
            f"🧾 <b>THÔNG TIN THANH TOÁN</b>\n"
            f"• Mã đơn: <code>{html.escape(order['order_code'])}</code>\n"
            f"• Sản phẩm: <b>{html.escape(order['product_name'])}</b> x{order['quantity']}\n"
            f"• Tổng tiền: <b>{fmt_vnd(total)}</b>\n"
            f"• Số dư hiện có: <b>{fmt_vnd(bal)}</b> và <b>{fmt_num(pts)} point</b>\n\n"
        )
        if bank_id and bank_acc:
            caption += (
                f"🏦 Ngân hàng: <b>{html.escape(bank_id)}</b>\n"
                f"💳 Số tài khoản: <code>{html.escape(bank_acc)}</code>\n"
                f"👤 Chủ TK: <b>{html.escape(bank_name or 'Chưa cập nhật')}</b>\n"
                f"✍️ Nội dung CK: <code>{html.escape(order['order_code'])}</code>\n\n"
                f"⚠️ <i>Chọn thanh toán bằng Point / Số dư bên dưới để nhận hàng ngay, hoặc chuyển khoản theo thông tin trên.</i>"
            )
        else:
            caption += (
                f"ℹ️ <i>Cửa hàng chưa cấu hình số tài khoản tự động. "
                f"Vui lòng thanh toán bằng Số dư / Point bên dưới hoặc liên hệ Admin cho đơn <code>{html.escape(order['order_code'])}</code>.</i>"
            )

        markup = types.InlineKeyboardMarkup(row_width=1)
        if pts >= total:
            markup.add(
                types.InlineKeyboardButton(
                    f"🎁 Thanh toán bằng Point (-{fmt_num(total)} point)",
                    callback_data=f"pay_pts:{order['id']}",
                )
            )
        if bal >= total:
            markup.add(
                types.InlineKeyboardButton(
                    f"💰 Thanh toán bằng Số dư (-{fmt_vnd(total)})",
                    callback_data=f"pay_bal:{order['id']}",
                )
            )
        markup.add(
            types.InlineKeyboardButton("✅ Tôi đã chuyển khoản", callback_data=f"paid_notify:{order['id']}"),
            types.InlineKeyboardButton("❌ Hủy đơn & Quay lại", callback_data=f"user_cancel:{order['id']}"),
        )

        qr_url = build_vietqr_url(bank_id, bank_acc, bank_name, order["total_amount"], order["order_code"])
        _update_or_send(chat_id, caption, markup=markup, message_id=message_id, photo_url=qr_url)

        _notify_admin_new_order(tb, order, note="ĐƠN HÀNG MỚI TẠO")
        return ""

    def place_deposit(user: types.User, chat_id: int, amount: int, message_id: Optional[int] = None) -> str:
        dep, err = db.create_deposit(bot_id, user.id, user.username or "", amount)
        if err or not dep:
            return err or "Không thể tạo yêu cầu nạp tiền."
        settings = db.get_all_settings(bot_id)
        bank_id = settings.get("bank_id", "")
        bank_acc = settings.get("bank_account", "")
        bank_name = settings.get("bank_name", "")

        caption = (
            f"💳 <b>THÔNG TIN NẠP TIỀN</b>\n"
            f"• Mã nạp: <code>{html.escape(dep['deposit_code'])}</code>\n"
            f"• Số tiền nạp: <b>{fmt_vnd(dep['amount'])}</b>\n\n"
        )
        if bank_id and bank_acc:
            caption += (
                f"🏦 Ngân hàng: <b>{html.escape(bank_id)}</b>\n"
                f"💳 Số tài khoản: <code>{html.escape(bank_acc)}</code>\n"
                f"👤 Chủ TK: <b>{html.escape(bank_name or 'Chưa cập nhật')}</b>\n"
                f"✍️ Nội dung CK: <code>{html.escape(dep['deposit_code'])}</code>\n\n"
                f"⚠️ <i>Giữ nguyên nội dung chuyển khoản để được cộng số dư nhanh nhất.</i>"
            )
        else:
            caption += (
                f"ℹ️ <i>Cửa hàng chưa cấu hình ngân hàng tự động. "
                f"Vui lòng liên hệ Admin kèm mã <code>{html.escape(dep['deposit_code'])}</code>.</i>"
            )
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("✅ Tôi đã chuyển khoản", callback_data=f"dep_notify:{dep['id']}"),
            types.InlineKeyboardButton("❌ Hủy & Quay lại", callback_data=f"dep_cancel:{dep['id']}"),
        )
        qr_url = build_vietqr_url(bank_id, bank_acc, bank_name, dep["amount"], dep["deposit_code"])
        _update_or_send(chat_id, caption, markup=markup, message_id=message_id, photo_url=qr_url)
        _notify_admin_new_deposit(tb, dep, note="YÊU CẦU NẠP TIỀN MỚI")
        return ""

    @tb.callback_query_handler(func=lambda c: c.data.startswith("buy:"))
    def cb_buy_product(call: types.CallbackQuery):
        _save_sender(bot_id, call.from_user)
        _, pid_str, qty_str = call.data.split(":")
        clear_pending(call.message.chat.id)
        err = place_order(call.from_user, call.message.chat.id, int(pid_str), int(qty_str), call.message.message_id)
        if err:
            tb.answer_callback_query(call.id, err, show_alert=True)
        else:
            tb.answer_callback_query(call.id)

    @tb.callback_query_handler(func=lambda c: c.data.startswith("qty:"))
    def cb_ask_quantity(call: types.CallbackQuery):
        _save_sender(bot_id, call.from_user)
        pid = int(call.data.split(":")[1])
        p = db.get_product(pid)
        if not p or not p["is_active"] or int(p["bot_id"]) != bot_id or p["stock_count"] <= 0:
            tb.answer_callback_query(call.id, "Sản phẩm không còn khả dụng.", show_alert=True)
            return
        clear_pending(call.message.chat.id)
        pending_qty[call.message.chat.id] = (pid, call.message.message_id)
        tb.answer_callback_query(call.id)
        back_kb = types.InlineKeyboardMarkup(row_width=1)
        back_kb.add(types.InlineKeyboardButton("⬅️ Quay lại sản phẩm", callback_data=f"prod:{pid}"))
        _update_or_send(
            call.message.chat.id,
            f"✍️ Nhập số lượng muốn mua cho <b>{html.escape(p['name'])}</b> "
            f"(từ 1 đến {p['stock_count']}):\nGõ số vào tin nhắn hoặc bấm nút Quay lại bên dưới.",
            markup=back_kb,
            message_id=call.message.message_id,
        )

    @tb.callback_query_handler(func=lambda c: c.data.startswith("dep_amt:"))
    def cb_deposit_preset(call: types.CallbackQuery):
        _save_sender(bot_id, call.from_user)
        amt = int(call.data.split(":")[1])
        clear_pending(call.message.chat.id)
        err = place_deposit(call.from_user, call.message.chat.id, amt, call.message.message_id)
        if err:
            tb.answer_callback_query(call.id, err, show_alert=True)
        else:
            tb.answer_callback_query(call.id)

    @tb.callback_query_handler(func=lambda c: c.data == "dep_custom")
    def cb_deposit_custom(call: types.CallbackQuery):
        _save_sender(bot_id, call.from_user)
        clear_pending(call.message.chat.id)
        pending_deposit[call.message.chat.id] = call.message.message_id
        tb.answer_callback_query(call.id)
        back_kb = types.InlineKeyboardMarkup(row_width=1)
        back_kb.add(types.InlineKeyboardButton("⬅️ Quay lại chọn mệnh giá", callback_data="back_deposit_menu"))
        _update_or_send(
            call.message.chat.id,
            "✍️ Nhập số tiền VNĐ muốn nạp (tối thiểu <b>1.000</b>, ví dụ: <code>50000</code>):\nGõ số vào tin nhắn hoặc bấm Quay lại bên dưới.",
            markup=back_kb,
            message_id=call.message.message_id,
        )

    @tb.message_handler(
        func=lambda m: bool(m.text)
        and (m.chat.id in pending_qty or m.chat.id in pending_giftcode or m.chat.id in pending_deposit)
        and not m.text.startswith("/")
        and m.text not in MENU_BUTTONS
    )
    def handle_pending_input(message: types.Message):
        _save_sender(bot_id, message.from_user)
        cid = message.chat.id
        text = message.text.strip()

        if cid in pending_giftcode:
            pending_giftcode.discard(cid)
            if text == "0":
                tb.send_message(cid, "Đã hủy nhập Giftcode.")
                return
            ok, msg, pts, bal = db.redeem_giftcode(bot_id, cid, text)
            if not ok:
                tb.send_message(cid, f"⚠️ {html.escape(msg)}")
                return
            user = db.get_user(bot_id, cid)
            rewards = []
            if pts > 0:
                rewards.append(f"+{fmt_num(pts)} point")
            if bal > 0:
                rewards.append(f"+{fmt_vnd(bal)}")
            reward_str = " và ".join(rewards) if rewards else "0 point"
            tb.send_message(
                cid,
                f"🎉 <b>NHẬN GIFTCODE THÀNH CÔNG!</b>\n"
                f"• Mã: <code>{html.escape(text.upper())}</code>\n"
                f"• Nhận được: <b>{reward_str}</b>\n"
                f"• Số dư hiện có: <b>{fmt_vnd(user.get('balance', 0))}</b> và <b>{fmt_num(user.get('points', 0))} point</b>",
            )
            return

        if cid in pending_deposit:
            mid = pending_deposit.get(cid)
            clean_num = text.replace(".", "").replace(",", "").strip()
            back_kb = types.InlineKeyboardMarkup(row_width=1)
            back_kb.add(types.InlineKeyboardButton("⬅️ Quay lại chọn mệnh giá", callback_data="back_deposit_menu"))
            if not clean_num.isdigit():
                _update_or_send(cid, "⚠️ Vui lòng chỉ gõ số tiền VNĐ (ví dụ: 50000).\nGõ 0 hoặc bấm Quay lại để hủy.", markup=back_kb, message_id=mid)
                return
            amt = int(clean_num)
            if amt == 0:
                pending_deposit.pop(cid, None)
                _send_deposit_menu(cid, mid)
                return
            if amt < 1000:
                _update_or_send(cid, "⚠️ Số tiền nạp tối thiểu là 1.000đ. Hãy nhập lại hoặc bấm Quay lại để hủy.", markup=back_kb, message_id=mid)
                return
            pending_deposit.pop(cid, None)
            err = place_deposit(message.from_user, cid, amt, mid)
            if err:
                _update_or_send(cid, f"⚠️ {html.escape(err)}", markup=back_kb, message_id=mid)
            return

        if cid in pending_qty:
            pid, mid = pending_qty.get(cid, (None, None))
            back_kb = types.InlineKeyboardMarkup(row_width=1)
            if pid is not None:
                back_kb.add(types.InlineKeyboardButton("⬅️ Quay lại sản phẩm", callback_data=f"prod:{pid}"))
            if not text.isdigit():
                _update_or_send(cid, "⚠️ Vui lòng chỉ gõ một số nguyên (ví dụ: 3). Gõ 0 hoặc bấm Quay lại để hủy.", markup=back_kb, message_id=mid)
                return
            qty = int(text)
            if qty == 0 or pid is None:
                pending_qty.pop(cid, None)
                if pid is not None:
                    _send_product_detail(cid, pid, mid)
                return
            p = db.get_product(pid)
            if p and qty > p["stock_count"]:
                _update_or_send(cid, f"⚠️ Kho chỉ còn {p['stock_count']} sản phẩm, hãy nhập số nhỏ hơn hoặc bằng.", markup=back_kb, message_id=mid)
                return
            pending_qty.pop(cid, None)
            err = place_order(message.from_user, cid, pid, qty, mid)
            if err:
                _update_or_send(cid, f"⚠️ {html.escape(err)}", markup=back_kb, message_id=mid)

    @tb.callback_query_handler(func=lambda c: c.data.startswith("pay_bal:") or c.data.startswith("pay_pts:"))
    def cb_pay_with_wallet(call: types.CallbackQuery):
        _save_sender(bot_id, call.from_user)
        mode, oid_str = call.data.split(":")
        order_id = int(oid_str)
        order = db.get_order(order_id)
        if not order or order["chat_id"] != call.from_user.id or order["bot_id"] != bot_id:
            tb.answer_callback_query(call.id, "Không tìm thấy đơn hàng.", show_alert=True)
            return
        pay_with = "points" if mode == "pay_pts" else "balance"
        ok, msg, updated_order, items = db.fulfill_order_with_wallet(order_id, pay_with=pay_with)
        tb.answer_callback_query(call.id, msg, show_alert=not ok)
        if ok and updated_order:
            after = db.get_user(bot_id, call.message.chat.id)
            lines = "\n".join(f"<code>{html.escape(item)}</code>" for item in items)
            text = (
                f"🎉 <b>THANH TOÁN THÀNH CÔNG!</b> ({html.escape(msg)})\n"
                f"• Mã đơn: <code>{html.escape(updated_order['order_code'])}</code>\n"
                f"• Sản phẩm: <b>{html.escape(updated_order['product_name'])}</b> x{updated_order['quantity']}\n"
                f"• Tổng thanh toán: <b>{fmt_vnd(updated_order['total_amount'])}</b>\n"
                f"• Số dư còn lại: <b>{fmt_vnd(after.get('balance', 0))}</b> và <b>{fmt_num(after.get('points', 0))} point</b>\n\n"
                f"📦 <b>THÔNG TIN SẢN PHẨM CỦA BẠN:</b>\n"
                f"{lines}\n\n"
                f"Cảm ơn bạn đã ủng hộ! Bạn có thể xem lại đơn bất cứ lúc nào ở mục <b>📦 Đơn hàng của tôi</b>."
            )
            _update_or_send(call.message.chat.id, text, markup=None, message_id=call.message.message_id)

    @tb.callback_query_handler(func=lambda c: c.data.startswith("dep_notify:"))
    def cb_dep_notify(call: types.CallbackQuery):
        dep_id = int(call.data.split(":")[1])
        dep = db.get_deposit(dep_id)
        if not dep or dep["chat_id"] != call.from_user.id or dep["bot_id"] != bot_id:
            tb.answer_callback_query(call.id, "Không tìm thấy yêu cầu nạp.", show_alert=True)
            return
        if dep["status"] == "paid":
            tb.answer_callback_query(call.id, "Yêu cầu nạp này đã được duyệt rồi!", show_alert=True)
            return
        if dep["status"] == "cancelled":
            tb.answer_callback_query(call.id, "Yêu cầu nạp này đã bị hủy.", show_alert=True)
            return
        tb.answer_callback_query(call.id, "⏳ Đã gửi thông báo chuyển khoản đến Admin! Hệ thống đang kiểm tra.", show_alert=True)
        _notify_admin_new_deposit(tb, dep, note="KHÁCH BÁO ĐÃ CHUYỂN KHOẢN NẠP TIỀN")

    @tb.callback_query_handler(func=lambda c: c.data.startswith("dep_cancel:"))
    def cb_dep_cancel(call: types.CallbackQuery):
        dep_id = int(call.data.split(":")[1])
        dep = db.get_deposit(dep_id)
        if not dep or dep["chat_id"] != call.from_user.id or dep["bot_id"] != bot_id:
            tb.answer_callback_query(call.id, "Không tìm thấy yêu cầu nạp.", show_alert=True)
            return
        ok, msg, _ = db.cancel_deposit(dep_id)
        tb.answer_callback_query(call.id, msg, show_alert=not ok)
        if ok:
            if call.message.content_type == "photo":
                try:
                    tb.delete_message(call.message.chat.id, call.message.message_id)
                    _send_deposit_menu(call.message.chat.id)
                    return
                except Exception:
                    pass
            _send_deposit_menu(call.message.chat.id, call.message.message_id)

    @tb.callback_query_handler(func=lambda c: c.data.startswith("admin_dep_approve:"))
    def cb_admin_dep_approve(call: types.CallbackQuery):
        if not is_admin(call.from_user.id):
            tb.answer_callback_query(call.id, "Bạn không có quyền Admin.", show_alert=True)
            return
        dep_id = int(call.data.split(":")[1])
        existing = db.get_deposit(dep_id)
        if not existing or existing["bot_id"] != bot_id:
            tb.answer_callback_query(call.id, "Không tìm thấy yêu cầu nạp.", show_alert=True)
            return
        ok, msg, dep = db.approve_deposit(dep_id)
        tb.answer_callback_query(call.id, msg, show_alert=True)
        if ok and dep:
            notify_deposit_approved(dep)
            tb.send_message(
                call.message.chat.id,
                f"✅ Đã duyệt nạp <b>{fmt_vnd(dep['amount'])}</b> cho mã <code>{html.escape(dep['deposit_code'])}</code>.",
            )

    @tb.callback_query_handler(func=lambda c: c.data.startswith("admin_dep_cancel:"))
    def cb_admin_dep_cancel(call: types.CallbackQuery):
        if not is_admin(call.from_user.id):
            tb.answer_callback_query(call.id, "Bạn không có quyền Admin.", show_alert=True)
            return
        dep_id = int(call.data.split(":")[1])
        existing = db.get_deposit(dep_id)
        if not existing or existing["bot_id"] != bot_id:
            tb.answer_callback_query(call.id, "Không tìm thấy yêu cầu nạp.", show_alert=True)
            return
        ok, msg, dep = db.cancel_deposit(dep_id)
        tb.answer_callback_query(call.id, msg, show_alert=True)
        if ok and dep:
            notify_deposit_cancelled(dep)
            tb.send_message(
                call.message.chat.id,
                f"❌ Admin đã hủy yêu cầu nạp <code>{html.escape(dep['deposit_code'])}</code>.",
            )

    @tb.callback_query_handler(func=lambda c: c.data.startswith("paid_notify:"))
    def cb_paid_notify(call: types.CallbackQuery):
        order_id = int(call.data.split(":")[1])
        order = db.get_order(order_id)
        if not order or order["chat_id"] != call.from_user.id or order["bot_id"] != bot_id:
            tb.answer_callback_query(call.id, "Không tìm thấy đơn hàng.", show_alert=True)
            return
        if order["status"] == "paid":
            tb.answer_callback_query(call.id, "Đơn này đã được giao hàng rồi!", show_alert=True)
            return
        if order["status"] == "cancelled":
            tb.answer_callback_query(call.id, "Đơn này đã bị hủy.", show_alert=True)
            return

        tb.answer_callback_query(call.id, "⏳ Đã gửi thông báo đến Admin! Hệ thống đang kiểm tra và sẽ giao hàng ngay.", show_alert=True)
        _notify_admin_new_order(tb, order, note="KHÁCH BÁO ĐÃ CHUYỂN KHOẢN")

    @tb.callback_query_handler(func=lambda c: c.data.startswith("user_cancel:"))
    def cb_user_cancel(call: types.CallbackQuery):
        order_id = int(call.data.split(":")[1])
        order = db.get_order(order_id)
        if not order or order["chat_id"] != call.from_user.id or order["bot_id"] != bot_id:
            tb.answer_callback_query(call.id, "Không tìm thấy đơn hàng.", show_alert=True)
            return
        ok, msg, _ = db.cancel_order(order_id)
        tb.answer_callback_query(call.id, msg, show_alert=not ok)
        if ok:
            if call.message.content_type == "photo":
                try:
                    tb.delete_message(call.message.chat.id, call.message.message_id)
                    _send_product_detail(call.message.chat.id, order["product_id"])
                    return
                except Exception:
                    pass
            _send_product_detail(call.message.chat.id, order["product_id"], call.message.message_id)

    @tb.callback_query_handler(func=lambda c: c.data.startswith("admin_approve:"))
    def cb_admin_approve(call: types.CallbackQuery):
        if not is_admin(call.from_user.id):
            tb.answer_callback_query(call.id, "Bạn không có quyền Admin.", show_alert=True)
            return
        order_id = int(call.data.split(":")[1])
        existing = db.get_order(order_id)
        if not existing or existing["bot_id"] != bot_id:
            tb.answer_callback_query(call.id, "Không tìm thấy đơn hàng.", show_alert=True)
            return
        ok, msg, order, items = db.fulfill_order(order_id)
        tb.answer_callback_query(call.id, msg, show_alert=True)
        if ok and order:
            deliver_order_to_customer(order, items)
            tb.send_message(
                call.message.chat.id,
                f"✅ Đã duyệt và giao hàng thành công cho đơn <code>{html.escape(order['order_code'])}</code>.",
            )

    @tb.callback_query_handler(func=lambda c: c.data.startswith("admin_cancel:"))
    def cb_admin_cancel(call: types.CallbackQuery):
        if not is_admin(call.from_user.id):
            tb.answer_callback_query(call.id, "Bạn không có quyền Admin.", show_alert=True)
            return
        order_id = int(call.data.split(":")[1])
        existing = db.get_order(order_id)
        if not existing or existing["bot_id"] != bot_id:
            tb.answer_callback_query(call.id, "Không tìm thấy đơn hàng.", show_alert=True)
            return
        ok, msg, order = db.cancel_order(order_id)
        tb.answer_callback_query(call.id, msg, show_alert=True)
        if ok and order:
            notify_order_cancelled(order)
            tb.send_message(
                call.message.chat.id,
                f"❌ Admin đã hủy đơn <code>{html.escape(order['order_code'])}</code>.",
            )

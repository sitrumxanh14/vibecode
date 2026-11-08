import random
import sqlite3
import string
import threading
from typing import Any, Dict, List, Optional, Tuple

import config

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

# Cài đặt mặc định cho mỗi bot (mỗi bot có bộ cài đặt riêng)
DEFAULT_BOT_SETTINGS: Dict[str, str] = {
    "bank_id": "MB",
    "bank_account": "",
    "bank_name": "",
    "admin_telegram_id": "",
    "support_contact": "@admin",
    "sepay_api_key": "",
}


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON;")
    return _conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def init_db() -> None:
    with _lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                username TEXT DEFAULT '',
                name TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER NOT NULL DEFAULT 0,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                price INTEGER NOT NULL,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                is_sold INTEGER DEFAULT 0,
                order_id INTEGER DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sold_at TIMESTAMP DEFAULT NULL,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER NOT NULL DEFAULT 0,
                order_code TEXT UNIQUE NOT NULL,
                chat_id INTEGER NOT NULL,
                username TEXT DEFAULT '',
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                total_amount INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                delivered_content TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP DEFAULT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_users (
                bot_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                username TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (bot_id, chat_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                bot_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT DEFAULT '',
                PRIMARY KEY (bot_id, key)
            )
            """
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT DEFAULT '')"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS giftcodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER NOT NULL DEFAULT 0,
                code TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                balance INTEGER NOT NULL DEFAULT 0,
                max_uses INTEGER NOT NULL DEFAULT 1,
                used_count INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(bot_id, code)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS giftcode_redemptions (
                bot_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                giftcode_id INTEGER NOT NULL,
                redeemed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (bot_id, chat_id, giftcode_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER NOT NULL DEFAULT 0,
                deposit_code TEXT UNIQUE NOT NULL,
                chat_id INTEGER NOT NULL,
                username TEXT DEFAULT '',
                amount INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP DEFAULT NULL
            )
            """
        )

        # Nâng cấp CSDL cũ (bản 1 bot) lên bản nhiều bot: thêm cột bot_id
        for table in ("products", "orders"):
            if not _column_exists(conn, table, "bot_id"):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN bot_id INTEGER NOT NULL DEFAULT 0")
        for col in ("balance", "points"):
            if not _column_exists(conn, "bot_users", col):
                conn.execute(f"ALTER TABLE bot_users ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_products_bot ON products(bot_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_bot ON orders(bot_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_deposits_bot ON deposits(bot_id)")
        conn.commit()

    adopt_legacy_if_needed()


def adopt_legacy_if_needed() -> None:
    """Gán dữ liệu cũ (từ bản 1 bot) cho bot đầu tiên trong hệ thống, chỉ làm 1 lần."""
    with _lock:
        conn = get_conn()
        done = conn.execute("SELECT 1 FROM meta WHERE key = 'legacy_adopted'").fetchone()
        if done:
            return
        first = conn.execute("SELECT id FROM bots ORDER BY id ASC LIMIT 1").fetchone()
        if not first:
            return
        bot_id = int(first["id"])
        conn.execute("UPDATE products SET bot_id = ? WHERE bot_id = 0", (bot_id,))
        conn.execute("UPDATE orders SET bot_id = ? WHERE bot_id = 0", (bot_id,))
        if _table_exists(conn, "users"):
            conn.execute(
                """
                INSERT OR IGNORE INTO bot_users (bot_id, chat_id, username, full_name, created_at)
                SELECT ?, chat_id, username, full_name, created_at FROM users
                """,
                (bot_id,),
            )
        if _table_exists(conn, "settings"):
            conn.execute(
                "INSERT OR IGNORE INTO bot_settings (bot_id, key, value) SELECT ?, key, value FROM settings",
                (bot_id,),
            )
        conn.execute("INSERT INTO meta (key, value) VALUES ('legacy_adopted', '1')")
        conn.commit()


# ---------- BOTS ----------
def add_bot(token: str, username: str = "", name: str = "") -> Tuple[Optional[int], str]:
    token = token.strip()
    with _lock:
        conn = get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO bots (token, username, name, is_active) VALUES (?, ?, ?, 1)",
                (token, username.strip().lstrip("@"), name.strip()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return None, "Token này đã có trong hệ thống."
        bot_id = int(cur.lastrowid)
    adopt_legacy_if_needed()
    return bot_id, ""


def list_bots() -> List[Dict[str, Any]]:
    with _lock:
        rows = get_conn().execute("SELECT * FROM bots ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]


def get_bot(bot_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        return dict(row) if row else None


def get_bot_by_token(token: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM bots WHERE token = ?", (token.strip(),)).fetchone()
        return dict(row) if row else None


def update_bot_info(bot_id: int, username: str, name: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE bots SET username = ?, name = ? WHERE id = ?",
            ((username or "").lstrip("@"), name or "", bot_id),
        )
        conn.commit()


def set_bot_active(bot_id: int, active: bool) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE bots SET is_active = ? WHERE id = ?", (1 if active else 0, bot_id))
        conn.commit()


def delete_bot(bot_id: int) -> None:
    """Xóa bot cùng toàn bộ sản phẩm, kho, đơn hàng, khách hàng và cài đặt của bot đó."""
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM products WHERE bot_id = ?", (bot_id,))  # kho xóa theo (CASCADE)
        conn.execute("DELETE FROM orders WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM deposits WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM giftcode_redemptions WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM giftcodes WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM bot_users WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM bot_settings WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
        conn.commit()


# ---------- SETTINGS (theo từng bot) ----------
def get_all_settings(bot_id: int) -> Dict[str, str]:
    with _lock:
        rows = get_conn().execute(
            "SELECT key, value FROM bot_settings WHERE bot_id = ?", (bot_id,)
        ).fetchall()
        data = dict(DEFAULT_BOT_SETTINGS)
        for r in rows:
            data[r["key"]] = r["value"]
        return data


def get_setting(bot_id: int, key: str, default: str = "") -> str:
    with _lock:
        row = get_conn().execute(
            "SELECT value FROM bot_settings WHERE bot_id = ? AND key = ?", (bot_id, key)
        ).fetchone()
        if row is None:
            return DEFAULT_BOT_SETTINGS.get(key, default)
        return row["value"]


def update_settings(bot_id: int, updates: Dict[str, str]) -> None:
    with _lock:
        conn = get_conn()
        for key, value in updates.items():
            conn.execute(
                "INSERT INTO bot_settings (bot_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(bot_id, key) DO UPDATE SET value = excluded.value",
                (bot_id, key, value.strip()),
            )
        conn.commit()


# ---------- USERS ----------
def upsert_user(bot_id: int, chat_id: int, username: str, full_name: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO bot_users (bot_id, chat_id, username, full_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(bot_id, chat_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name
            """,
            (bot_id, chat_id, username or "", full_name or ""),
        )
        conn.commit()


def list_users(bot_id: int) -> List[Dict[str, Any]]:
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM bot_users WHERE bot_id = ? ORDER BY created_at DESC", (bot_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_user(bot_id: int, chat_id: int) -> Dict[str, Any]:
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM bot_users WHERE bot_id = ? AND chat_id = ?", (bot_id, chat_id)
        ).fetchone()
        if row:
            return dict(row)
        return {"bot_id": bot_id, "chat_id": chat_id, "username": "", "full_name": "", "balance": 0, "points": 0}


def adjust_user_wallet(bot_id: int, chat_id: int, delta_balance: int = 0, delta_points: int = 0) -> bool:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            """
            UPDATE bot_users
            SET balance = MAX(0, balance + ?),
                points = MAX(0, points + ?)
            WHERE bot_id = ? AND chat_id = ?
            """,
            (int(delta_balance), int(delta_points), bot_id, chat_id),
        )
        conn.commit()
        return cur.rowcount > 0


# ---------- GIFTCODES ----------
def create_giftcode(bot_id: int, code: str, points: int, balance: int = 0, max_uses: int = 1) -> Tuple[bool, str]:
    clean_code = code.strip().upper()
    if not clean_code:
        return False, "Mã Giftcode không được để trống."
    with _lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO giftcodes (bot_id, code, points, balance, max_uses) VALUES (?, ?, ?, ?, ?)",
                (bot_id, clean_code, max(0, int(points)), max(0, int(balance)), max(1, int(max_uses))),
            )
            conn.commit()
            return True, f"Đã tạo Giftcode {clean_code}."
        except sqlite3.IntegrityError:
            return False, "Mã Giftcode này đã tồn tại."


def list_giftcodes(bot_id: int) -> List[Dict[str, Any]]:
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM giftcodes WHERE bot_id = ? ORDER BY id DESC", (bot_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_giftcode(bot_id: int, giftcode_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM giftcode_redemptions WHERE bot_id = ? AND giftcode_id = ?", (bot_id, giftcode_id))
        conn.execute("DELETE FROM giftcodes WHERE id = ? AND bot_id = ?", (giftcode_id, bot_id))
        conn.commit()


def redeem_giftcode(bot_id: int, chat_id: int, code: str) -> Tuple[bool, str, int, int]:
    clean_code = code.strip().upper()
    with _lock:
        conn = get_conn()
        gc = conn.execute(
            "SELECT * FROM giftcodes WHERE bot_id = ? AND UPPER(code) = ?", (bot_id, clean_code)
        ).fetchone()
        if not gc or not gc["is_active"]:
            return False, "Mã Giftcode không tồn tại hoặc đã ngừng hoạt động.", 0, 0
        if gc["used_count"] >= gc["max_uses"]:
            return False, "Mã Giftcode này đã hết lượt sử dụng.", 0, 0
        already = conn.execute(
            "SELECT 1 FROM giftcode_redemptions WHERE bot_id = ? AND chat_id = ? AND giftcode_id = ?",
            (bot_id, chat_id, gc["id"]),
        ).fetchone()
        if already:
            return False, "Bạn đã sử dụng mã Giftcode này rồi.", 0, 0

        pts = int(gc["points"])
        bal = int(gc["balance"])
        conn.execute(
            "INSERT INTO giftcode_redemptions (bot_id, chat_id, giftcode_id) VALUES (?, ?, ?)",
            (bot_id, chat_id, gc["id"]),
        )
        conn.execute(
            "UPDATE giftcodes SET used_count = used_count + 1 WHERE id = ?", (gc["id"],)
        )
        conn.execute(
            "UPDATE bot_users SET points = points + ?, balance = balance + ? WHERE bot_id = ? AND chat_id = ?",
            (pts, bal, bot_id, chat_id),
        )
        conn.commit()
        return True, "Nhận Giftcode thành công!", pts, bal


# ---------- DEPOSITS (NẠP TIỀN) ----------
def _generate_deposit_code(conn: sqlite3.Connection) -> str:
    for _ in range(20):
        code = "NAP" + "".join(random.choices(string.digits, k=6))
        exists = conn.execute("SELECT 1 FROM deposits WHERE deposit_code = ?", (code,)).fetchone()
        if not exists:
            return code
    return "NAP" + "".join(random.choices(string.digits, k=8))


def create_deposit(bot_id: int, chat_id: int, username: str, amount: int) -> Tuple[Optional[Dict[str, Any]], str]:
    if amount < 1000:
        return None, "Số tiền nạp tối thiểu là 1.000đ."
    with _lock:
        conn = get_conn()
        code = _generate_deposit_code(conn)
        cur = conn.execute(
            """
            INSERT INTO deposits (bot_id, deposit_code, chat_id, username, amount, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (bot_id, code, chat_id, username or "", int(amount)),
        )
        conn.commit()
        return get_deposit(int(cur.lastrowid)), ""


def get_deposit(deposit_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM deposits WHERE id = ?", (deposit_id,)).fetchone()
        return dict(row) if row else None


def get_deposit_by_code(deposit_code: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM deposits WHERE UPPER(deposit_code) = UPPER(?)", (deposit_code.strip(),)
        ).fetchone()
        return dict(row) if row else None


def list_deposits(bot_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM deposits WHERE bot_id = ? ORDER BY id DESC LIMIT ?", (bot_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def approve_deposit(deposit_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    with _lock:
        conn = get_conn()
        dep = get_deposit(deposit_id)
        if not dep:
            return False, "Không tìm thấy yêu cầu nạp tiền.", None
        if dep["status"] == "paid":
            return False, "Yêu cầu nạp này đã được duyệt trước đó.", dep
        if dep["status"] == "cancelled":
            return False, "Yêu cầu nạp này đã bị hủy.", dep
        conn.execute(
            "UPDATE deposits SET status = 'paid', paid_at = CURRENT_TIMESTAMP WHERE id = ?",
            (deposit_id,),
        )
        conn.execute(
            "UPDATE bot_users SET balance = balance + ? WHERE bot_id = ? AND chat_id = ?",
            (int(dep["amount"]), dep["bot_id"], dep["chat_id"]),
        )
        conn.commit()
        return True, "Đã duyệt nạp tiền và cộng số dư cho khách!", get_deposit(deposit_id)


def cancel_deposit(deposit_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    with _lock:
        dep = get_deposit(deposit_id)
        if not dep:
            return False, "Không tìm thấy yêu cầu nạp tiền.", None
        if dep["status"] != "pending":
            return False, "Chỉ có thể hủy yêu cầu đang chờ thanh toán.", dep
        conn = get_conn()
        conn.execute("UPDATE deposits SET status = 'cancelled' WHERE id = ?", (deposit_id,))
        conn.commit()
        return True, "Đã hủy yêu cầu nạp tiền.", get_deposit(deposit_id)


# ---------- PRODUCTS & STOCK ----------
def create_product(bot_id: int, name: str, description: str, price: int) -> int:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO products (bot_id, name, description, price) VALUES (?, ?, ?, ?)",
            (bot_id, name.strip(), description.strip(), int(price)),
        )
        conn.commit()
        return int(cur.lastrowid)


def toggle_product(product_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE products SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?",
            (product_id,),
        )
        conn.commit()


def update_product(bot_id: int, product_id: int, name: str, description: str, price: int) -> bool:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "UPDATE products SET name = ?, description = ?, price = ? WHERE id = ? AND bot_id = ?",
            (name.strip(), description.strip(), int(price), product_id, bot_id),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_product(product_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()


def list_products(bot_id: int, only_active: bool = False) -> List[Dict[str, Any]]:
    with _lock:
        conn = get_conn()
        where = "WHERE p.bot_id = ?"
        if only_active:
            where += " AND p.is_active = 1"
        rows = conn.execute(
            f"""
            SELECT p.*,
                   COALESCE(SUM(CASE WHEN s.is_sold = 0 THEN 1 ELSE 0 END), 0) AS stock_count,
                   COALESCE(SUM(CASE WHEN s.is_sold = 1 THEN 1 ELSE 0 END), 0) AS sold_count
            FROM products p
            LEFT JOIN stock_items s ON s.product_id = p.id
            {where}
            GROUP BY p.id
            ORDER BY p.id DESC
            """,
            (bot_id,),
        ).fetchall()
        result: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if not only_active:
                items = conn.execute(
                    "SELECT id, content, created_at FROM stock_items WHERE product_id = ? AND is_sold = 0 ORDER BY id ASC",
                    (d["id"],),
                ).fetchall()
                d["unsold_items"] = [dict(x) for x in items]
                d["unsold_text"] = "\n".join(x["content"] for x in items)
            result.append(d)
        return result


def replace_unsold_stock(product_id: int, raw_lines: str) -> int:
    """Thay toàn bộ danh sách tồn kho chưa bán của sản phẩm bằng danh sách mới (mỗi dòng 1 mục)."""
    items = [line.strip() for line in raw_lines.splitlines() if line.strip()]
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM stock_items WHERE product_id = ? AND is_sold = 0", (product_id,))
        if items:
            conn.executemany(
                "INSERT INTO stock_items (product_id, content) VALUES (?, ?)",
                [(product_id, item) for item in items],
            )
        conn.commit()
        return len(items)


def get_product(product_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        row = get_conn().execute(
            """
            SELECT p.*,
                   COALESCE(SUM(CASE WHEN s.is_sold = 0 THEN 1 ELSE 0 END), 0) AS stock_count
            FROM products p
            LEFT JOIN stock_items s ON s.product_id = p.id
            WHERE p.id = ?
            GROUP BY p.id
            """,
            (product_id,),
        ).fetchone()
        return dict(row) if row else None


def add_stock_bulk(product_id: int, raw_lines: str) -> int:
    items = [line.strip() for line in raw_lines.splitlines() if line.strip()]
    if not items:
        return 0
    with _lock:
        conn = get_conn()
        conn.executemany(
            "INSERT INTO stock_items (product_id, content) VALUES (?, ?)",
            [(product_id, item) for item in items],
        )
        conn.commit()
        return len(items)


def list_unsold_stock(product_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    with _lock:
        rows = get_conn().execute(
            "SELECT id, content, created_at FROM stock_items WHERE product_id = ? AND is_sold = 0 ORDER BY id ASC LIMIT ?",
            (product_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_stock_item(stock_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM stock_items WHERE id = ? AND is_sold = 0", (stock_id,))
        conn.commit()


# ---------- ORDERS ----------
def _generate_order_code() -> str:
    suffix = "".join(random.choices(string.digits, k=6))
    return f"DH{suffix}"


def create_order(
    bot_id: int, chat_id: int, username: str, product_id: int, quantity: int
) -> Tuple[Optional[Dict[str, Any]], str]:
    if quantity <= 0:
        return None, "Số lượng không hợp lệ."
    with _lock:
        prod = get_product(product_id)
        if not prod or not prod["is_active"] or int(prod["bot_id"]) != int(bot_id):
            return None, "Sản phẩm không tồn tại hoặc đã ngừng bán."
        if prod["stock_count"] < quantity:
            return None, f"Kho chỉ còn {prod['stock_count']} sản phẩm, không đủ số lượng bạn chọn."

        conn = get_conn()
        order_code = _generate_order_code()
        while conn.execute("SELECT 1 FROM orders WHERE order_code = ?", (order_code,)).fetchone():
            order_code = _generate_order_code()

        total_amount = int(prod["price"]) * int(quantity)
        cur = conn.execute(
            """
            INSERT INTO orders (bot_id, order_code, chat_id, username, product_id, product_name, quantity, total_amount, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (bot_id, order_code, chat_id, username or "", product_id, prod["name"], quantity, total_amount),
        )
        conn.commit()
        order = get_order(int(cur.lastrowid))
        return order, ""


def get_order(order_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None


def get_order_by_code(order_code: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM orders WHERE UPPER(order_code) = UPPER(?)",
            (order_code.strip(),),
        ).fetchone()
        return dict(row) if row else None


def list_orders(bot_id: int, status: str = "all", limit: int = 100) -> List[Dict[str, Any]]:
    with _lock:
        if status and status != "all":
            rows = get_conn().execute(
                "SELECT * FROM orders WHERE bot_id = ? AND status = ? ORDER BY id DESC LIMIT ?",
                (bot_id, status, limit),
            ).fetchall()
        else:
            rows = get_conn().execute(
                "SELECT * FROM orders WHERE bot_id = ? ORDER BY id DESC LIMIT ?",
                (bot_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def list_user_orders(bot_id: int, chat_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM orders WHERE bot_id = ? AND chat_id = ? ORDER BY id DESC LIMIT ?",
            (bot_id, chat_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def fulfill_order(order_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]], List[str]]:
    """Atomically deduct stock and mark order as paid."""
    with _lock:
        conn = get_conn()
        order = get_order(order_id)
        if not order:
            return False, "Không tìm thấy đơn hàng.", None, []
        if order["status"] == "paid":
            return False, "Đơn hàng này đã được duyệt và giao trước đó.", order, []
        if order["status"] == "cancelled":
            return False, "Đơn hàng này đã bị hủy.", order, []

        qty = int(order["quantity"])
        stock_rows = conn.execute(
            "SELECT id, content FROM stock_items WHERE product_id = ? AND is_sold = 0 ORDER BY id ASC LIMIT ?",
            (order["product_id"], qty),
        ).fetchall()

        if len(stock_rows) < qty:
            return (
                False,
                f"Không đủ hàng trong kho (cần {qty}, còn {len(stock_rows)}). Hãy nạp thêm kho rồi duyệt lại.",
                order,
                [],
            )

        stock_ids = [r["id"] for r in stock_rows]
        items = [r["content"] for r in stock_rows]
        delivered_text = "\n".join(items)

        placeholders = ",".join("?" for _ in stock_ids)
        conn.execute(
            f"UPDATE stock_items SET is_sold = 1, order_id = ?, sold_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            [order_id] + stock_ids,
        )
        conn.execute(
            "UPDATE orders SET status = 'paid', delivered_content = ?, paid_at = CURRENT_TIMESTAMP WHERE id = ?",
            (delivered_text, order_id),
        )
        conn.commit()
        updated_order = get_order(order_id)
        return True, "Duyệt đơn và xuất kho thành công!", updated_order, items


def fulfill_order_with_wallet(order_id: int, pay_with: str = "auto") -> Tuple[bool, str, Optional[Dict[str, Any]], List[str]]:
    """Thanh toán đơn hàng trực tiếp bằng point và/hoặc số dư (1 point = 1đ)."""
    with _lock:
        conn = get_conn()
        order = get_order(order_id)
        if not order:
            return False, "Không tìm thấy đơn hàng.", None, []
        if order["status"] != "pending":
            return False, "Đơn hàng này đã được xử lý.", order, []
        user = get_user(order["bot_id"], order["chat_id"])
        cost = int(order["total_amount"])
        pts = int(user.get("points", 0))
        bal = int(user.get("balance", 0))

        if pay_with == "points":
            if pts < cost:
                return False, "Không đủ point để thanh toán.", order, []
            use_pts, use_bal = cost, 0
        elif pay_with == "balance":
            if bal < cost:
                return False, "Không đủ số dư để thanh toán.", order, []
            use_pts, use_bal = 0, cost
        else:
            if pts + bal < cost:
                return False, "Không đủ point / số dư để thanh toán.", order, []
            use_pts = min(pts, cost)
            use_bal = cost - use_pts

        qty = int(order["quantity"])
        stock_rows = conn.execute(
            "SELECT id, content FROM stock_items WHERE product_id = ? AND is_sold = 0 ORDER BY id ASC LIMIT ?",
            (order["product_id"], qty),
        ).fetchall()
        if len(stock_rows) < qty:
            return False, f"Kho chỉ còn {len(stock_rows)} sản phẩm, không đủ để giao.", order, []

        conn.execute(
            "UPDATE bot_users SET points = points - ?, balance = balance - ? WHERE bot_id = ? AND chat_id = ?",
            (use_pts, use_bal, order["bot_id"], order["chat_id"]),
        )
        stock_ids = [r["id"] for r in stock_rows]
        items = [r["content"] for r in stock_rows]
        delivered_text = "\n".join(items)
        placeholders = ",".join("?" for _ in stock_ids)
        conn.execute(
            f"UPDATE stock_items SET is_sold = 1, order_id = ?, sold_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            [order_id] + stock_ids,
        )
        conn.execute(
            "UPDATE orders SET status = 'paid', delivered_content = ?, paid_at = CURRENT_TIMESTAMP WHERE id = ?",
            (delivered_text, order_id),
        )
        conn.commit()
        parts = []
        if use_pts > 0:
            parts.append(f"{use_pts:,} point".replace(",", "."))
        if use_bal > 0:
            parts.append(f"{use_bal:,}đ".replace(",", "."))
        spent_label = " + ".join(parts) if parts else "0đ"
        return True, f"Đã trừ {spent_label} và xuất hàng thành công!", get_order(order_id), items


def cancel_order(order_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    with _lock:
        order = get_order(order_id)
        if not order:
            return False, "Không tìm thấy đơn hàng.", None
        if order["status"] != "pending":
            return False, "Chỉ có thể hủy đơn đang chờ thanh toán.", order
        conn = get_conn()
        conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
        conn.commit()
        return True, "Đã hủy đơn hàng.", get_order(order_id)


def delete_order(bot_id: int, order_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    with _lock:
        order = get_order(order_id)
        if not order or int(order["bot_id"]) != bot_id:
            return False, "Không tìm thấy đơn hàng.", None
        conn = get_conn()
        conn.execute("UPDATE stock_items SET order_id = NULL WHERE order_id = ?", (order_id,))
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        conn.commit()
        return True, "Đã xóa đơn hàng.", order


# ---------- STATS ----------
def get_stats(bot_id: int) -> Dict[str, int]:
    with _lock:
        conn = get_conn()
        revenue = conn.execute(
            "SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE bot_id = ? AND status = 'paid'",
            (bot_id,),
        ).fetchone()[0]
        paid_orders = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE bot_id = ? AND status = 'paid'", (bot_id,)
        ).fetchone()[0]
        pending_orders = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE bot_id = ? AND status = 'pending'", (bot_id,)
        ).fetchone()[0]
        total_users = conn.execute(
            "SELECT COUNT(*) FROM bot_users WHERE bot_id = ?", (bot_id,)
        ).fetchone()[0]
        total_stock = conn.execute(
            """
            SELECT COUNT(*) FROM stock_items s
            JOIN products p ON p.id = s.product_id
            WHERE p.bot_id = ? AND s.is_sold = 0
            """,
            (bot_id,),
        ).fetchone()[0]
        return {
            "revenue": int(revenue),
            "paid_orders": int(paid_orders),
            "pending_orders": int(pending_orders),
            "total_users": int(total_users),
            "total_stock": int(total_stock),
        }

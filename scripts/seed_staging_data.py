"""
Seed & Benchmark Dataset Generator for VibeCommerce Multi-Tenant Platform.

Populates realistic multi-tenant bots, categories, digital inventory items,
voucher campaigns, customer wallets, and historical orders for staging/CI tests.
"""

import random
import string
from typing import Dict, Any

import database as db


SAMPLE_CATEGORIES = [
    ("AI & Developer Subscriptions", 1),
    ("Cloud & VPS Credits", 2),
    ("Productivity & Design SaaS", 3),
    ("Gaming & Entertainment Keys", 4),
]

SAMPLE_PRODUCTS = [
    ("Cursor Pro 1-Month Activation Key", "Official activation license with 500 fast requests", 380000, 349000, 12),
    ("ChatGPT Plus Shared Workspace Seat", "30-day warranty, automated instant key delivery", 150000, 129000, 35),
    ("GitHub Copilot Individual 1-Year Redeem", "Global redeem code for individual developer accounts", 890000, 0, 8),
    ("JetBrains All Products Pack (1 Year)", "Commercial license key for IntelliJ, PyCharm, WebStorm", 1450000, 1250000, 6),
    ("DigitalOcean $200 Cloud Credit Code", "Valid for 60 days on new and existing team billing accounts", 650000, 590000, 15),
    ("Figma Professional Team Seat (Annual)", "Automated invite token + license key", 720000, 0, 10),
    ("Canva Pro Team License Key (12 Months)", "Instant activation via licensing portal", 210000, 175000, 40),
    ("Steam Wallet 500,000 VND Gift Code", "VN Region instant digital PIN code", 485000, 0, 25),
]

SAMPLE_VOUCHERS = [
    ("DEVIN2026", "percent", 15, 200000, 100000, 100),
    ("FLASH50K", "fixed", 50000, 350000, 50000, 50),
    ("VIPMEMBER10", "percent", 10, 100000, 200000, 500),
]


def _fake_serial(prefix: str, idx: int) -> str:
    rand_block = "".join(random.choices(string.ascii_uppercase + string.digits, k=12))
    return f"{prefix}-{idx:04d}-{rand_block[:4]}-{rand_block[4:8]}-{rand_block[8:]}"


def seed_demo_environment() -> Dict[str, Any]:
    db.init_db()
    bots = db.list_bots()
    if not bots:
        conn = db.get_conn()
        with db._lock:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO bots (token, username, name, is_active) VALUES (?, ?, ?, 1)",
                ("7100000001:AAH_StagingDemoTenantBotToken0001", "vibe_cloud_store_bot", "VibeCloud Digital Store"),
            )
            bot_id = int(cur.lastrowid)
            conn.commit()
        db._ensure_bot_settings(bot_id)
    else:
        bot_id = int(bots[0]["id"])

    cat_ids = []
    for name, sort_order in SAMPLE_CATEGORIES:
        cid = db.add_category(bot_id, name, sort_order=sort_order)
        cat_ids.append(cid)

    created_products = 0
    seeded_stock = 0
    for idx, (pname, pdesc, price, flash_price, stock_qty) in enumerate(SAMPLE_PRODUCTS):
        cid = cat_ids[idx % len(cat_ids)]
        pid = db.add_product(
            name=pname,
            price=price,
            description=pdesc,
            bot_id=bot_id,
            category_id=cid,
            flash_sale_price=flash_price,
        )
        created_products += 1
        prefix = "".join(w[0] for w in pname.split()[:3]).upper()
        lines = [_fake_serial(prefix, i + 1) for i in range(stock_qty)]
        seeded_stock += db.add_stock_bulk(pid, lines)

    for code, vtype, val, min_ord, max_disc, max_uses in SAMPLE_VOUCHERS:
        db.add_voucher(
            bot_id=bot_id,
            code=code,
            discount_type=vtype,
            discount_value=val,
            min_order=min_ord,
            max_discount=max_disc,
            max_uses=max_uses,
        )

    return {
        "bot_id": bot_id,
        "categories": len(cat_ids),
        "products": created_products,
        "stock_items": seeded_stock,
        "vouchers": len(SAMPLE_VOUCHERS),
    }


if __name__ == "__main__":
    summary = seed_demo_environment()
    print(f"Seeded demo dataset: {summary}")

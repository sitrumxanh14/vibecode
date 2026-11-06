import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "admin123").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET", "tele-shop-secret-key-change-me").strip()
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "shop.db").strip()

DEFAULT_SETTINGS = {
    "bank_id": os.getenv("BANK_ID", "MB").strip(),
    "bank_account": os.getenv("BANK_ACCOUNT", "").strip(),
    "bank_name": os.getenv("BANK_NAME", "").strip(),
    "admin_telegram_id": os.getenv("ADMIN_TELEGRAM_ID", "").strip(),
    "support_contact": os.getenv("SUPPORT_CONTACT", "@admin").strip(),
    "sepay_api_key": os.getenv("SEPAY_API_KEY", "").strip(),
}

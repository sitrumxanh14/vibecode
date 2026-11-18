import logging
import sys
from pathlib import Path

import uvicorn

# Nếu chạy ngầm bằng pythonw.exe (không có cửa sổ console), ghi log ra file bot.log
if sys.stdout is None or sys.stderr is None:
    _log_file = open(Path(__file__).resolve().parent / "bot.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = _log_file
    sys.stderr = _log_file

import bot_handler
import config
import database as db
from web_dashboard import app
from workers.order_expiry_worker import expiry_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tele-shop-bot")


def main() -> None:
    db.init_db()

    # BOT_TOKEN trong .env (nếu có) được thêm làm bot đầu tiên; các bot khác thêm trên web
    if config.BOT_TOKEN and not db.get_bot_by_token(config.BOT_TOKEN):
        bot_id, add_err = db.add_bot(config.BOT_TOKEN)
        if bot_id is None:
            logger.warning("Không thêm được bot từ .env: %s", add_err)
        else:
            logger.info("Đã thêm bot từ BOT_TOKEN trong .env (id=%s).", bot_id)

    bots = db.list_bots()
    if bots:
        bot_handler.start_all()
        logger.info("Đã khởi chạy %s bot Telegram ở luồng nền.", bot_handler.running_count())
    else:
        logger.warning("Chưa có bot nào — vào Web Dashboard, tab Quản lý Bot để thêm bot.")

    expiry_worker.start()
    logger.info("Web Dashboard đang chạy tại: http://localhost:%s", config.PORT)
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, log_config=None)


if __name__ == "__main__":
    main()

import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("توکن ربات تنظیم نشده است. مقدار BOT_TOKEN را در فایل .env یا متغیرهای Railway قرار دهید.")

OWNER_ID = int(os.getenv("OWNER_ID", "1375809015"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "-1003112877277"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.db").strip() or "bot.db"
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups").strip() or "backups"

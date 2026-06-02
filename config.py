import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip()

OWNER_ID = int(os.getenv("OWNER_ID", "1375809015"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "-1003112877277"))

DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/database.db").strip()
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tehran").strip()

database_parent = Path(DATABASE_PATH).parent
database_parent.mkdir(parents=True, exist_ok=True)

BACKUP_DIR = os.getenv("BACKUP_DIR", "/data/backups").strip()
Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)

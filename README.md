# ربات آپلودر مدیا تلگرام

این پروژه یک ربات آپلودر مدیا با Python 3.12، Aiogram 3.x، SQLite، Long Polling و APScheduler است.

## راه‌اندازی

ابتدا فایل `.env.example` را به `.env` تغییر نام دهید و توکن ربات را وارد کنید:

```env
BOT_TOKEN=توکن_ربات
OWNER_ID=1375809015
LOG_CHANNEL_ID=-1003112877277
DATABASE_PATH=bot.db
BACKUP_DIR=backups

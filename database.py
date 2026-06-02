import random
import sqlite3
import string
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from config import BACKUP_DIR, DATABASE_PATH, OWNER_ID


db_lock = threading.RLock()


class DatabaseManager:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.database_path,
            timeout=30,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with db_lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def init_database(self) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    joined_at TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL DEFAULT '',
                    added_by INTEGER NOT NULL DEFAULT 0,
                    added_at TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contents (
                    code TEXT PRIMARY KEY,
                    content_type TEXT NOT NULL,
                    file_id TEXT,
                    text TEXT,
                    uploader_id INTEGER NOT NULL,
                    uploader_name TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    downloads INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    invite_link TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    added_at TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    downloaded_at TEXT NOT NULL,
                    FOREIGN KEY(code) REFERENCES contents(code) ON DELETE CASCADE
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS statistics (
                    stat_date TEXT PRIMARY KEY,
                    new_users INTEGER NOT NULL DEFAULT 0,
                    downloads INTEGER NOT NULL DEFAULT 0,
                    uploads INTEGER NOT NULL DEFAULT 0,
                    successful_joins INTEGER NOT NULL DEFAULT 0,
                    link_views INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            now = self._now()
            conn.execute(
                """
                INSERT OR IGNORE INTO admins (user_id, first_name, added_by, added_at)
                VALUES (?, ?, ?, ?)
                """,
                (OWNER_ID, "مالک ربات", OWNER_ID, now),
            )

            conn.execute(
                """
                INSERT OR IGNORE INTO settings (key, value)
                VALUES ('force_join_enabled', '0')
                """
            )

            self._ensure_today_stats_in_tx(conn)

    def _ensure_today_stats_in_tx(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO statistics
            (stat_date, new_users, downloads, uploads, successful_joins, link_views)
            VALUES (?, 0, 0, 0, 0, 0)
            """,
            (self._today(),),
        )

    def _increment_stat_in_tx(self, conn: sqlite3.Connection, column: str, amount: int = 1) -> None:
        allowed = {"new_users", "downloads", "uploads", "successful_joins", "link_views"}
        if column not in allowed:
            raise ValueError("ستون آماری نامعتبر است.")

        self._ensure_today_stats_in_tx(conn)
        conn.execute(
            f"UPDATE statistics SET {column} = {column} + ? WHERE stat_date = ?",
            (amount, self._today()),
        )

    def register_user(self, user_id: int, first_name: str, username: str) -> bool:
        now = self._now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")

            existing = conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE users
                    SET first_name = ?, username = ?, last_seen = ?
                    WHERE user_id = ?
                    """,
                    (first_name, username, now, user_id),
                )
                return False

            conn.execute(
                """
                INSERT INTO users (user_id, first_name, username, joined_at, last_seen)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, first_name, username, now, now),
            )
            self._increment_stat_in_tx(conn, "new_users", 1)
            return True

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_all_users(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY joined_at ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_user(self, user_id: int) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))

    def is_admin(self, user_id: int) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT user_id FROM admins WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return row is not None

    def add_admin(self, user_id: int, first_name: str, added_by: int) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR REPLACE INTO admins (user_id, first_name, added_by, added_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, first_name, added_by, self._now()),
            )

    def remove_admin(self, user_id: int) -> None:
        if user_id == OWNER_ID:
            return

        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))

    def get_admins(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM admins ORDER BY added_at ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    def create_code(self) -> str:
        alphabet = string.ascii_uppercase + string.digits
        while True:
            code = "".join(random.choices(alphabet, k=6))
            with self.connection() as conn:
                row = conn.execute(
                    "SELECT code FROM contents WHERE code = ?",
                    (code,),
                ).fetchone()
                if row is None:
                    return code

    def create_content(
        self,
        content_type: str,
        file_id: str | None,
        text: str | None,
        uploader_id: int,
        uploader_name: str,
    ) -> str:
        code = self.create_code()

        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO contents
                (code, content_type, file_id, text, uploader_id, uploader_name, uploaded_at, downloads)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    code,
                    content_type,
                    file_id,
                    text,
                    uploader_id,
                    uploader_name,
                    self._now(),
                ),
            )
            self._increment_stat_in_tx(conn, "uploads", 1)

        return code

    def get_content(self, code: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM contents WHERE code = ?",
                (code,),
            ).fetchone()
            return dict(row) if row else None

    def get_contents(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM contents ORDER BY uploaded_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_content(self, code: str) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM contents WHERE code = ?", (code,))

    def record_download(self, user_id: int, code: str) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO downloads (user_id, code, downloaded_at)
                VALUES (?, ?, ?)
                """,
                (user_id, code, self._now()),
            )
            conn.execute(
                """
                UPDATE contents
                SET downloads = downloads + 1
                WHERE code = ?
                """,
                (code,),
            )
            self._increment_stat_in_tx(conn, "downloads", 1)

    def get_downloads(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM downloads ORDER BY downloaded_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def add_channel(self, chat_id: str, title: str, invite_link: str) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO channels (chat_id, title, invite_link, active, added_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    invite_link = excluded.invite_link,
                    active = 1
                """,
                (chat_id, title, invite_link, self._now()),
            )

    def remove_channel(self, channel_id: int) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))

    def set_channel_active(self, channel_id: int, active: bool) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE channels SET active = ? WHERE id = ?",
                (1 if active else 0, channel_id),
            )

    def get_channels(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM channels ORDER BY id ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_active_channels(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM channels WHERE active = 1 ORDER BY id ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def is_force_join_enabled(self) -> bool:
        return self.get_setting("force_join_enabled", "0") == "1"

    def set_force_join_enabled(self, enabled: bool) -> None:
        self.set_setting("force_join_enabled", "1" if enabled else "0")

    def record_link_view(self) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._increment_stat_in_tx(conn, "link_views", 1)

    def record_successful_join(self) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._increment_stat_in_tx(conn, "successful_joins", 1)

    def get_statistics_summary(self) -> dict[str, int]:
        today = self._today()
        with self.connection() as conn:
            self._ensure_today_stats_in_tx(conn)

            total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            total_uploads = conn.execute("SELECT COUNT(*) AS c FROM contents").fetchone()["c"]
            total_downloads = conn.execute("SELECT COUNT(*) AS c FROM downloads").fetchone()["c"]
            total_admins = conn.execute("SELECT COUNT(*) AS c FROM admins").fetchone()["c"]
            total_channels = conn.execute("SELECT COUNT(*) AS c FROM channels").fetchone()["c"]

            daily = conn.execute(
                "SELECT * FROM statistics WHERE stat_date = ?",
                (today,),
            ).fetchone()

            return {
                "total_users": int(total_users),
                "total_uploads": int(total_uploads),
                "total_downloads": int(total_downloads),
                "total_admins": int(total_admins),
                "total_channels": int(total_channels),
                "daily_users": int(daily["new_users"] if daily else 0),
                "daily_downloads": int(daily["downloads"] if daily else 0),
            }

    def get_daily_report(self) -> dict[str, int]:
        today = self._today()
        with self.connection() as conn:
            self._ensure_today_stats_in_tx(conn)

            daily = conn.execute(
                "SELECT * FROM statistics WHERE stat_date = ?",
                (today,),
            ).fetchone()

            total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]

            return {
                "new_users": int(daily["new_users"] if daily else 0),
                "total_users": int(total_users),
                "downloads": int(daily["downloads"] if daily else 0),
                "uploads": int(daily["uploads"] if daily else 0),
                "successful_joins": int(daily["successful_joins"] if daily else 0),
                "link_views": int(daily["link_views"] if daily else 0),
            }

    def reset_statistics_for_date(self, stat_date: str) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR REPLACE INTO statistics
                (stat_date, new_users, downloads, uploads, successful_joins, link_views)
                VALUES (?, 0, 0, 0, 0, 0)
                """,
                (stat_date,),
            )

    def create_backup(self) -> str:
        backup_dir = Path(BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)

        filename = f"backup_{datetime.now().strftime('%Y_%m_%d')}.db"
        backup_path = str(backup_dir / filename)

        with db_lock:
            source = self._connect()
            destination = sqlite3.connect(
                backup_path,
                timeout=30,
                check_same_thread=False,
            )
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
                source.close()

        return backup_path


db = DatabaseManager(DATABASE_PATH)

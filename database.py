import os
import random
import sqlite3
import string
import threading
from datetime import datetime
from typing import Any, Callable

from config import BACKUP_DIR


class DatabaseManager:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        self.db_lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.database_path,
            timeout=30,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    def _write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        with self.db_lock:
            with self._connect() as conn:
                try:
                    result = operation(conn)
                    conn.commit()
                    return result
                except Exception:
                    conn.rollback()
                    raise

    def _read(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._connect() as conn:
            return operation(conn)

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y/%m/%d %H:%M")

    def initialize(self) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    is_blocked INTEGER NOT NULL DEFAULT 0
                );
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    added_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    content_type TEXT NOT NULL,
                    file_id TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    uploader_id INTEGER NOT NULL,
                    uploader_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    content_id INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (content_id) REFERENCES contents(id) ON DELETE CASCADE
                );
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
                    link_views INTEGER NOT NULL DEFAULT 0,
                    broadcasts INTEGER NOT NULL DEFAULT 0
                );
                """
            )

            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('force_join_enabled', '0');"
            )
            self._ensure_statistics_row(conn, self._today())

        self._write(op)

    def _ensure_statistics_row(self, conn: sqlite3.Connection, date_text: str) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO statistics(
                stat_date,
                new_users,
                downloads,
                uploads,
                successful_joins,
                link_views,
                broadcasts
            )
            VALUES(?, 0, 0, 0, 0, 0, 0);
            """,
            (date_text,),
        )

    def _increment_stat(self, conn: sqlite3.Connection, field: str, amount: int = 1) -> None:
        allowed = {
            "new_users",
            "downloads",
            "uploads",
            "successful_joins",
            "link_views",
            "broadcasts",
        }
        if field not in allowed:
            raise ValueError("نام آمار معتبر نیست.")

        today = self._today()
        self._ensure_statistics_row(conn, today)
        conn.execute(
            f"UPDATE statistics SET {field} = {field} + ? WHERE stat_date = ?;",
            (amount, today),
        )

    def register_user(
        self,
        user_id: int,
        first_name: str,
        last_name: str,
        username: str,
    ) -> bool:
        def op(conn: sqlite3.Connection) -> bool:
            now = self._now()
            row = conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?;",
                (user_id,),
            ).fetchone()

            is_new = row is None

            if is_new:
                conn.execute(
                    """
                    INSERT INTO users(
                        user_id,
                        first_name,
                        last_name,
                        username,
                        created_at,
                        last_seen,
                        is_blocked
                    )
                    VALUES(?, ?, ?, ?, ?, ?, 0);
                    """,
                    (user_id, first_name, last_name, username, now, now),
                )
                self._increment_stat(conn, "new_users", 1)
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET first_name = ?,
                        last_name = ?,
                        username = ?,
                        last_seen = ?,
                        is_blocked = 0
                    WHERE user_id = ?;
                    """,
                    (first_name, last_name, username, now, user_id),
                )

            return is_new

        return bool(self._write(op))

    def mark_user_blocked(self, user_id: int, blocked: bool) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE users SET is_blocked = ? WHERE user_id = ?;",
                (1 if blocked else 0, user_id),
            )

        self._write(op)

    def get_all_user_ids(self) -> list[int]:
        def op(conn: sqlite3.Connection) -> list[int]:
            rows = conn.execute(
                "SELECT user_id FROM users WHERE is_blocked = 0 ORDER BY user_id ASC;"
            ).fetchall()
            return [int(row["user_id"]) for row in rows]

        return self._read(op)

    def add_admin(self, user_id: int, name: str, added_by: int) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO admins(user_id, name, added_by, created_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    name = excluded.name,
                    added_by = excluded.added_by;
                """,
                (user_id, name, added_by, self._now()),
            )

        self._write(op)

    def remove_admin(self, user_id: int) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM admins WHERE user_id = ?;", (user_id,))

        self._write(op)

    def is_admin(self, user_id: int) -> bool:
        def op(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT user_id FROM admins WHERE user_id = ?;",
                (user_id,),
            ).fetchone()
            return row is not None

        return bool(self._read(op))

    def get_admins(self) -> list[dict[str, Any]]:
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT user_id, name, added_by, created_at FROM admins ORDER BY created_at ASC;"
            ).fetchall()
            return [dict(row) for row in rows]

        return self._read(op)

    def _generate_code(self, length: int = 6) -> str:
        chars = string.ascii_uppercase + string.digits
        return "".join(random.choice(chars) for _ in range(length))

    def create_content(
        self,
        content_type: str,
        file_id: str,
        text: str,
        uploader_id: int,
        uploader_name: str,
    ) -> str:
        def op(conn: sqlite3.Connection) -> str:
            code = self._generate_code()
            while conn.execute("SELECT id FROM contents WHERE code = ?;", (code,)).fetchone():
                code = self._generate_code()

            conn.execute(
                """
                INSERT INTO contents(
                    code,
                    content_type,
                    file_id,
                    text,
                    uploader_id,
                    uploader_name,
                    created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?);
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
            self._increment_stat(conn, "uploads", 1)
            return code

        return str(self._write(op))

    def get_content_by_code(self, code: str) -> dict[str, Any] | None:
        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                """
                SELECT
                    id,
                    code,
                    content_type,
                    file_id,
                    text,
                    uploader_id,
                    uploader_name,
                    created_at
                FROM contents
                WHERE code = ?;
                """,
                (code,),
            ).fetchone()
            return dict(row) if row else None

        return self._read(op)

    def delete_content(self, content_id: int) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM contents WHERE id = ?;", (content_id,))

        self._write(op)

    def get_contents(self) -> list[dict[str, Any]]:
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT
                    id,
                    code,
                    content_type,
                    file_id,
                    text,
                    uploader_id,
                    uploader_name,
                    created_at
                FROM contents
                ORDER BY id DESC;
                """
            ).fetchall()
            return [dict(row) for row in rows]

        return self._read(op)

    def add_channel(self, chat_id: str, title: str, url: str) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO channels(chat_id, title, url, created_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url;
                """,
                (chat_id, title, url, self._now()),
            )

        self._write(op)

    def remove_channel(self, channel_id: int) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM channels WHERE id = ?;", (channel_id,))

        self._write(op)

    def get_channel_by_id(self, channel_id: int) -> dict[str, Any] | None:
        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT id, chat_id, title, url, created_at FROM channels WHERE id = ?;",
                (channel_id,),
            ).fetchone()
            return dict(row) if row else None

        return self._read(op)

    def get_channels(self) -> list[dict[str, Any]]:
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT id, chat_id, title, url, created_at FROM channels ORDER BY id ASC;"
            ).fetchall()
            return [dict(row) for row in rows]

        return self._read(op)

    def set_force_join_enabled(self, enabled: bool) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO settings(key, value)
                VALUES('force_join_enabled', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                """,
                ("1" if enabled else "0",),
            )

        self._write(op)

    def get_force_join_enabled(self) -> bool:
        def op(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'force_join_enabled';"
            ).fetchone()
            return bool(row and row["value"] == "1")

        return bool(self._read(op))

    def set_setting(self, key: str, value: str) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO settings(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                """,
                (key, value),
            )

        self._write(op)

    def get_setting(self, key: str, default: str = "") -> str:
        def op(conn: sqlite3.Connection) -> str:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?;",
                (key,),
            ).fetchone()
            return str(row["value"]) if row else default

        return self._read(op)

    def record_download(self, user_id: int, content_id: int, code: str) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO downloads(user_id, content_id, code, created_at)
                VALUES(?, ?, ?, ?);
                """,
                (user_id, content_id, code, self._now()),
            )
            self._increment_stat(conn, "downloads", 1)

        self._write(op)

    def increment_link_view(self) -> None:
        def op(conn: sqlite3.Connection) -> None:
            self._increment_stat(conn, "link_views", 1)

        self._write(op)

    def increment_successful_join(self) -> None:
        def op(conn: sqlite3.Connection) -> None:
            self._increment_stat(conn, "successful_joins", 1)

        self._write(op)

    def increment_broadcast(self) -> None:
        def op(conn: sqlite3.Connection) -> None:
            self._increment_stat(conn, "broadcasts", 1)

        self._write(op)

    def get_statistics_summary(self) -> dict[str, int]:
        def op(conn: sqlite3.Connection) -> dict[str, int]:
            today = self._today()
            self._ensure_statistics_row(conn, today)

            daily = conn.execute(
                "SELECT * FROM statistics WHERE stat_date = ?;",
                (today,),
            ).fetchone()

            total_users = conn.execute("SELECT COUNT(*) AS c FROM users;").fetchone()["c"]
            total_uploads = conn.execute("SELECT COUNT(*) AS c FROM contents;").fetchone()["c"]
            total_downloads = conn.execute("SELECT COUNT(*) AS c FROM downloads;").fetchone()["c"]
            total_admins = conn.execute("SELECT COUNT(*) AS c FROM admins;").fetchone()["c"]
            total_channels = conn.execute("SELECT COUNT(*) AS c FROM channels;").fetchone()["c"]

            return {
                "total_users": int(total_users),
                "total_uploads": int(total_uploads),
                "total_downloads": int(total_downloads),
                "total_admins": int(total_admins),
                "total_channels": int(total_channels),
                "daily_users": int(daily["new_users"]),
                "daily_downloads": int(daily["downloads"]),
            }

        return self._read(op)

    def get_daily_report(self) -> dict[str, int]:
        def op(conn: sqlite3.Connection) -> dict[str, int]:
            today = self._today()
            self._ensure_statistics_row(conn, today)

            row = conn.execute(
                "SELECT * FROM statistics WHERE stat_date = ?;",
                (today,),
            ).fetchone()

            total_users = conn.execute("SELECT COUNT(*) AS c FROM users;").fetchone()["c"]

            return {
                "new_users": int(row["new_users"]),
                "total_users": int(total_users),
                "downloads": int(row["downloads"]),
                "uploads": int(row["uploads"]),
                "successful_joins": int(row["successful_joins"]),
                "link_views": int(row["link_views"]),
                "broadcasts": int(row["broadcasts"]),
            }

        return self._read(op)

    def get_downloads_by_user(self, user_id: int) -> list[dict[str, Any]]:
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT id, user_id, content_id, code, created_at
                FROM downloads
                WHERE user_id = ?
                ORDER BY id DESC;
                """,
                (user_id,),
            ).fetchall()
            return [dict(row) for row in rows]

        return self._read(op)

    def create_backup(self) -> str:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        filename = f"backup_{datetime.now().strftime('%Y_%m_%d')}.db"
        backup_path = os.path.join(BACKUP_DIR, filename)

        with self.db_lock:
            with self._connect() as source:
                with sqlite3.connect(
                    backup_path,
                    timeout=30,
                    check_same_thread=False,
                ) as destination:
                    source.backup(destination)
                    destination.commit()

        return backup_path

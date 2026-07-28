from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from storage.sqlcipher_driver import dbapi as sqlite3

if TYPE_CHECKING:  # pragma: no cover
    from sqlite3 import Connection as SQLiteConnection


MAX_TELEGRAM_ACCOUNTS = 5
ACCOUNT_BOUND_TASK_TYPES = (
    "sync_channels",
    "link_channels",
    "auto_comment",
    "auto_comment_slot",
    "direct_message",
    "comment",
    "sync_saved_dialogs",
    "join_saved_slot",
    "openai_test",
)
ACCOUNT_SETTING_PREFIXES = (
    "telegram.",
    "automation.",
    "commenting.",
    "openai.",
    "scheduler.",
)
SECRET_ACCOUNT_SETTING_KEYS = frozenset(
    {
        "telegram.api_hash",
        "telegram.phone",
        "telegram.proxy_username",
        "telegram.proxy_password",
        "telegram.proxy_secret",
        "openai.api_key",
    }
)


def _tables(conn: SQLiteConnection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _columns(conn: SQLiteConnection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _setting(conn: SQLiteConnection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return default if row is None else str(row[0] or default)


def _positive_int(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed > 0 else 0


def _task_account(payload: object) -> int:
    try:
        decoded = json.loads(str(payload or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0
    if not isinstance(decoded, dict):
        return 0
    return _positive_int(decoded.get("account_id"))


def migrate_multiaccount_v31(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Add durable account registry, account settings and task ownership.

    Existing domain tables are already account-scoped by migrations v18-v30.
    This migration replaces the remaining single-account control plane while
    retaining the global settings keys as a selected-account compatibility view.
    """

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")

        conn.execute(
            """CREATE TABLE IF NOT EXISTS telegram_accounts(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   telegram_account_id INTEGER NOT NULL UNIQUE
                       CHECK(telegram_account_id > 0),
                   session_name TEXT NOT NULL UNIQUE,
                   display_name TEXT NOT NULL DEFAULT 'Telegram Account',
                   username TEXT,
                   phone_masked TEXT,
                   authorized INTEGER NOT NULL DEFAULT 1
                       CHECK(authorized IN (0,1)),
                   runtime_state TEXT NOT NULL DEFAULT 'connected'
                       CHECK(runtime_state IN (
                           'disconnected','connecting','connected','running',
                           'paused','stopping','stopped','network_wait',
                           'flood_wait','restricted','authorization_required','error'
                       )),
                   stopped INTEGER NOT NULL DEFAULT 0 CHECK(stopped IN (0,1)),
                   last_error TEXT,
                   last_activity_at DATETIME,
                   created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS account_settings(
                   account_id INTEGER NOT NULL,
                   key TEXT NOT NULL,
                   value TEXT,
                   updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   PRIMARY KEY(account_id, key),
                   FOREIGN KEY(account_id)
                       REFERENCES telegram_accounts(telegram_account_id)
                       ON DELETE CASCADE)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_telegram_accounts_runtime
               ON telegram_accounts(stopped, runtime_state, updated_at)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_account_settings_prefix
               ON account_settings(account_id, key)"""
        )

        tables = _tables(conn)
        if "tasks" in tables and "account_id" not in _columns(conn, "tasks"):
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0"
            )
        if "tasks" in tables:
            rows = conn.execute(
                "SELECT id, type, payload, account_id FROM tasks"
            ).fetchall()
            for row in rows:
                owner = _positive_int(row["account_id"]) or _task_account(row["payload"])
                conn.execute(
                    "UPDATE tasks SET account_id=? WHERE id=?",
                    (owner, int(row["id"])),
                )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_tasks_account_status_due
                   ON tasks(account_id, status, not_before, created_at, id)"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_tasks_status_due_account
                   ON tasks(status, not_before, account_id, created_at, id)"""
            )

        conn.execute("DROP TRIGGER IF EXISTS telegram_accounts_limit_insert")
        conn.execute(
            f"""CREATE TRIGGER telegram_accounts_limit_insert
                BEFORE INSERT ON telegram_accounts
                WHEN (SELECT COUNT(*) FROM telegram_accounts) >= {MAX_TELEGRAM_ACCOUNTS}
                 AND NOT EXISTS(
                     SELECT 1 FROM telegram_accounts
                      WHERE telegram_account_id=NEW.telegram_account_id
                 )
                BEGIN
                    SELECT RAISE(ABORT, 'telegram account limit reached');
                END"""
        )

        current_account_id = _positive_int(
            _setting(conn, "telegram.account_id", "0")
        )
        if current_account_id > 0:
            display_name = (
                _setting(conn, "telegram.account_name", "Telegram Account").strip()
                or "Telegram Account"
            )
            username = _setting(conn, "telegram.account_username", "").strip() or None
            authorized = 1 if _setting(conn, "telegram.authorized", "1") == "1" else 0
            conn.execute(
                """INSERT INTO telegram_accounts(
                       telegram_account_id, session_name, display_name, username,
                       authorized, runtime_state, stopped, updated_at)
                   VALUES(?, 'main', ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
                   ON CONFLICT(telegram_account_id) DO UPDATE SET
                       display_name=excluded.display_name,
                       username=excluded.username,
                       authorized=excluded.authorized,
                       updated_at=CURRENT_TIMESTAMP""",
                (
                    current_account_id,
                    display_name,
                    username,
                    authorized,
                    "connected" if authorized else "authorization_required",
                ),
            )
            conn.execute(
                """INSERT INTO settings(key, value, updated_at)
                   VALUES('ui.selected_account_id', ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                (str(current_account_id),),
            )
            conn.execute(
                """INSERT OR IGNORE INTO settings(
                       key, value, updated_at)
                   VALUES('ui.previous_selected_account_id', '', CURRENT_TIMESTAMP)"""
            )
            settings_rows = conn.execute(
                "SELECT key, value FROM settings"
            ).fetchall()
            for row in settings_rows:
                key = str(row["key"])
                if key in {
                    "telegram.account_id",
                    "telegram.account_name",
                    "telegram.account_username",
                    "telegram.authorized",
                }:
                    continue
                if (
                    key in SECRET_ACCOUNT_SETTING_KEYS
                    or not key.startswith(ACCOUNT_SETTING_PREFIXES)
                ):
                    continue
                conn.execute(
                    """INSERT INTO account_settings(
                           account_id, key, value, updated_at)
                       VALUES(?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(account_id, key) DO NOTHING""",
                    (current_account_id, key, row["value"]),
                )

        conn.execute(
            "INSERT OR IGNORE INTO migrations(version) VALUES(31)"
        )
        conn.execute("PRAGMA user_version = 31")
        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

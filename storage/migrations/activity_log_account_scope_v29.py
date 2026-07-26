from __future__ import annotations

from typing import TYPE_CHECKING

from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path

if TYPE_CHECKING:  # pragma: no cover - typing only
    # ``sqlite3`` is bound to the SQLCipher DBAPI proxy object, not to a
    # module, so its DBAPI classes are imported from the standard library
    # for annotations. The two drivers are DBAPI-compatible.
    from sqlite3 import Connection as SQLiteConnection



def _column_names(conn: SQLiteConnection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate_activity_log_account_scope_v29(
    path: str | Path,
    *,
    sqlite_timeout_seconds: float = 30.0,
    busy_timeout_ms: int = 30_000,
) -> None:
    """Make the user-facing activity journal account-scoped.

    Older releases persisted all activity rows in one global stream. Their owner
    cannot be reconstructed safely after an account was switched, so legacy rows
    are deliberately assigned to account 0. Authenticated accounts only read rows
    carrying their exact Telegram account id.
    """

    conn = sqlite3.connect(str(path), timeout=float(sqlite_timeout_seconds))
    try:
        conn.execute(f"PRAGMA busy_timeout = {max(100, int(busy_timeout_ms))}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")

        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='logs'"
        ).fetchone()
        if table is None:
            conn.execute(
                """CREATE TABLE logs(
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       account_id INTEGER NOT NULL DEFAULT 0,
                       level TEXT NOT NULL,
                       message TEXT NOT NULL,
                       created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                   )"""
            )
        elif "account_id" not in _column_names(conn, "logs"):
            conn.execute(
                "ALTER TABLE logs ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0"
            )

        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_logs_account_id_id
               ON logs(account_id, id DESC)"""
        )
        migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrations'"
        ).fetchone()
        if migrations is not None:
            conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(29)")
        conn.execute("PRAGMA user_version = 29")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

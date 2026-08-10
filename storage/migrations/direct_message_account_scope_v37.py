from __future__ import annotations

from pathlib import Path

from storage.sqlcipher_driver import dbapi as sqlite3


def migrate_direct_message_account_scope_v37(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Scope direct-group delivery reservations to the owning Telegram account."""

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")

        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "direct_message_deliveries" in tables:
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(direct_message_deliveries)"
                ).fetchall()
            }
            if "account_id" not in columns:
                conn.execute(
                    "ALTER TABLE direct_message_deliveries "
                    "ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0"
                )
            if "tasks" in tables:
                conn.execute(
                    """UPDATE direct_message_deliveries
                       SET account_id=COALESCE(
                           (SELECT t.account_id FROM tasks t
                            WHERE t.id=direct_message_deliveries.task_id),
                           0
                       )
                       WHERE account_id IS NULL OR account_id=0"""
                )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS
                       idx_direct_delivery_account_chat_status
                       ON direct_message_deliveries(
                           account_id, chat_id, status, updated_at
                       )"""
            )

        if "migrations" in tables:
            conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(37)")
        conn.execute("PRAGMA user_version = 37")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

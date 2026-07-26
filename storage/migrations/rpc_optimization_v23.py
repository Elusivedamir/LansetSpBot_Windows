from __future__ import annotations

from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path


def migrate_rpc_optimization_v23(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Persist peer references, comment routes, and negative target cache."""

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")

        channel_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(channels)")
        }
        additions = {
            "access_hash": "INTEGER",
            "peer_type": "TEXT",
            "negative_status": "TEXT",
            "negative_until": "DATETIME",
        }
        for name, declaration in additions.items():
            if name not in channel_columns:
                conn.execute(f"ALTER TABLE channels ADD COLUMN {name} {declaration}")

        table_names = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "comment_schedule" in table_names:
            schedule_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(comment_schedule)")
            }
            schedule_additions = {
                "linked_chat_id": "INTEGER",
                "discussion_message_id": "INTEGER",
                "route_cached_at": "DATETIME",
            }
            for name, declaration in schedule_additions.items():
                if name not in schedule_columns:
                    conn.execute(
                        f"ALTER TABLE comment_schedule ADD COLUMN {name} {declaration}"
                    )

        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_channels_negative_cache
               ON channels(account_id, negative_until, comment_mode, linked_chat_id)"""
        )
        if "comment_schedule" in table_names:
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_comment_schedule_route
                   ON comment_schedule(id, task_id, channel_id, post_id)"""
            )
        conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(23)")
        conn.execute("PRAGMA user_version = 23")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

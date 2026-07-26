from __future__ import annotations

from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path


def migrate_comment_targets_v16(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Add safe channel/group target classification to a v15 database."""
    conn = sqlite3.connect(
        str(path),
        timeout=sqlite_timeout_seconds,
        isolation_level=None,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")

        channel_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(channels)")
        }
        if "target_kind" not in channel_columns:
            conn.execute(
                "ALTER TABLE channels ADD COLUMN target_kind TEXT NOT NULL DEFAULT 'channel'"
            )
        if "comment_mode" not in channel_columns:
            conn.execute(
                "ALTER TABLE channels ADD COLUMN comment_mode TEXT NOT NULL DEFAULT 'channel_post'"
            )

        # Every pre-v16 row came from the old broadcast-only synchronizer.
        conn.execute(
            """UPDATE channels
               SET target_kind='channel', comment_mode='channel_post'
               WHERE target_kind IS NULL OR target_kind NOT IN ('channel','group')
                  OR comment_mode IS NULL
                  OR comment_mode NOT IN (
                      'channel_post','pending','direct_group','linked_discussion'
                  )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_channels_comment_targets
               ON channels(comment_mode, linked_chat_id, last_comment_check_at)"""
        )
        conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(16)")
        conn.execute("PRAGMA user_version = 16")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

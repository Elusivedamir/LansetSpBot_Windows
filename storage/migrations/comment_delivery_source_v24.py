from __future__ import annotations

from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path


def migrate_comment_delivery_source_v24(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Make the immutable source post the delivery identity.

    A linked discussion is mutable Telegram routing metadata.  It must not be
    part of the uniqueness boundary, otherwise changing ``linked_chat_id`` can
    authorize a second comment to the same source post in one campaign.
    """

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")

        conn.execute(
            """CREATE TABLE comment_deliveries_v24(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   account_id INTEGER NOT NULL DEFAULT 0,
                   campaign_id INTEGER NOT NULL DEFAULT 0,
                   action_type TEXT NOT NULL DEFAULT 'comment',
                   channel_id INTEGER NOT NULL,
                   post_id INTEGER NOT NULL,
                   linked_chat_id INTEGER NOT NULL DEFAULT 0,
                   comment_message_id INTEGER,
                   text TEXT,
                   status TEXT NOT NULL DEFAULT 'sending',
                   error TEXT,
                   reserved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                   updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE(account_id, campaign_id, channel_id, post_id)
               )"""
        )

        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='comment_deliveries'"
        ).fetchone()
        if table is not None:
            # Legacy v21 can contain several route-context rows for one source.
            # Keep the safest/latest row: uncertain > sent > sending, then the
            # most recently updated row.  No source is made eligible again.
            conn.execute(
                """WITH ranked AS (
                       SELECT *,
                              ROW_NUMBER() OVER (
                                  PARTITION BY account_id, campaign_id, channel_id, post_id
                                  ORDER BY
                                      CASE status
                                          WHEN 'uncertain' THEN 3
                                          WHEN 'sent' THEN 2
                                          ELSE 1
                                      END DESC,
                                      COALESCE(updated_at, reserved_at, '') DESC,
                                      id DESC
                              ) AS rn
                       FROM comment_deliveries
                   )
                   INSERT INTO comment_deliveries_v24(
                       account_id, campaign_id, action_type, channel_id, post_id,
                       linked_chat_id, comment_message_id, text, status, error,
                       reserved_at, updated_at
                   )
                   SELECT account_id, campaign_id, action_type, channel_id, post_id,
                          COALESCE(linked_chat_id, 0), comment_message_id, text,
                          status, error, reserved_at, updated_at
                   FROM ranked
                   WHERE rn=1"""
            )
            conn.execute("DROP TABLE comment_deliveries")

        conn.execute("ALTER TABLE comment_deliveries_v24 RENAME TO comment_deliveries")
        conn.execute(
            """CREATE INDEX idx_delivery_lookup
               ON comment_deliveries(
                   account_id, campaign_id, channel_id, post_id, status
               )"""
        )
        conn.execute(
            """CREATE INDEX idx_delivery_recovery
               ON comment_deliveries(status, reserved_at)"""
        )
        conn.execute(
            """CREATE INDEX idx_delivery_campaign
               ON comment_deliveries(account_id, campaign_id, status)"""
        )

        migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrations'"
        ).fetchone()
        if migrations is not None:
            conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(24)")
        conn.execute("PRAGMA user_version = 24")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

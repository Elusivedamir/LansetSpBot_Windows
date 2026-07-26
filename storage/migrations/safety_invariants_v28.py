from __future__ import annotations

from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path


def migrate_safety_invariants_v28(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Enforce permanent peer bans and cross-campaign comment idempotency."""

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
            """CREATE TABLE IF NOT EXISTS local_ban_targets(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   account_id INTEGER NOT NULL,
                   peer_id INTEGER NOT NULL,
                   reason TEXT NOT NULL,
                   source_channel_id INTEGER,
                   related_peer_id INTEGER,
                   banned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE(account_id, peer_id)
               )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_local_ban_targets_account
               ON local_ban_targets(account_id, banned_at, peer_id)"""
        )

        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        channels_table = "channels" in tables
        if channels_table:
            conn.execute(
                """INSERT INTO local_ban_targets(
                       account_id, peer_id, reason, source_channel_id,
                       related_peer_id, banned_at)
                   SELECT account_id, channel_id,
                          COALESCE(local_ban_reason, 'Локальная блокировка'),
                          channel_id, local_ban_peer_id,
                          COALESCE(local_banned_at, CURRENT_TIMESTAMP)
                   FROM channels
                   WHERE local_banned_at IS NOT NULL
                   ON CONFLICT(account_id, peer_id) DO UPDATE SET
                       reason=excluded.reason,
                       source_channel_id=COALESCE(
                           local_ban_targets.source_channel_id,
                           excluded.source_channel_id
                       ),
                       related_peer_id=COALESCE(
                           local_ban_targets.related_peer_id,
                           excluded.related_peer_id
                       ),
                       banned_at=MIN(local_ban_targets.banned_at, excluded.banned_at)"""
            )
            conn.execute(
                """INSERT INTO local_ban_targets(
                       account_id, peer_id, reason, source_channel_id,
                       related_peer_id, banned_at)
                   SELECT account_id, local_ban_peer_id,
                          COALESCE(local_ban_reason, 'Локальная блокировка'),
                          channel_id, local_ban_peer_id,
                          COALESCE(local_banned_at, CURRENT_TIMESTAMP)
                   FROM channels
                   WHERE local_banned_at IS NOT NULL
                     AND local_ban_peer_id IS NOT NULL
                     AND local_ban_peer_id<>0
                   ON CONFLICT(account_id, peer_id) DO UPDATE SET
                       reason=excluded.reason,
                       source_channel_id=COALESCE(
                           local_ban_targets.source_channel_id,
                           excluded.source_channel_id
                       ),
                       related_peer_id=COALESCE(
                           local_ban_targets.related_peer_id,
                           excluded.related_peer_id
                       ),
                       banned_at=MIN(local_ban_targets.banned_at, excluded.banned_at)"""
            )

        deliveries_table = "comment_deliveries" in tables
        if deliveries_table:
            conn.execute(
                """CREATE TABLE comment_deliveries_v28(
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
                       UNIQUE(account_id, channel_id, post_id)
                   )"""
            )
            conn.execute(
                """WITH ranked AS (
                       SELECT *,
                              ROW_NUMBER() OVER (
                                  PARTITION BY account_id, channel_id, post_id
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
                   INSERT INTO comment_deliveries_v28(
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
            conn.execute(
                "ALTER TABLE comment_deliveries_v28 RENAME TO comment_deliveries"
            )
            conn.execute(
                """CREATE INDEX idx_delivery_lookup
                   ON comment_deliveries(account_id, channel_id, post_id, status)"""
            )
            conn.execute(
                """CREATE INDEX idx_delivery_recovery
                   ON comment_deliveries(status, reserved_at)"""
            )
            conn.execute(
                """CREATE INDEX idx_delivery_campaign
                   ON comment_deliveries(account_id, campaign_id, status)"""
            )

        # Existing queued work must not survive a migrated permanent ban.
        if {
            "channels",
            "comment_schedule",
            "comment_campaigns",
        }.issubset(tables):
            conn.execute(
                """UPDATE comment_schedule
                   SET status='cancelled',
                       result='Канал локально заблокирован',
                       executed_at=CURRENT_TIMESTAMP
                   WHERE status IN ('pending','queued')
                     AND EXISTS(
                         SELECT 1
                         FROM comment_campaigns c
                         JOIN local_ban_targets b
                           ON b.account_id=c.account_id
                          AND b.peer_id=comment_schedule.channel_id
                         WHERE c.id=comment_schedule.campaign_id
                     )"""
            )

        if {
            "saved_dialogs",
            "join_schedule",
            "join_campaigns",
        }.issubset(tables):
            conn.execute(
                """UPDATE join_schedule
                   SET status='cancelled', task_id=NULL,
                       result='Цель локально заблокирована',
                       executed_at=CURRENT_TIMESTAMP
                   WHERE status IN ('pending','queued')
                     AND EXISTS(
                         SELECT 1
                         FROM join_campaigns c
                         JOIN saved_dialogs d
                           ON d.id=join_schedule.saved_dialog_id
                         JOIN local_ban_targets b
                           ON b.account_id=c.account_id
                          AND b.peer_id=d.peer_id
                         WHERE c.id=join_schedule.campaign_id
                     )"""
            )

        migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrations'"
        ).fetchone()
        if migrations is not None:
            conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(28)")
        conn.execute("PRAGMA user_version = 28")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

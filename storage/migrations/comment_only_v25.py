from __future__ import annotations

from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path


def migrate_comment_only_v25(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Disable legacy plain-message delivery to ordinary Telegram groups.

    Marlen comments only below broadcast-channel posts through their linked
    discussion. Historical receipts remain for audit/idempotency, while pending
    direct-group work is made terminal before the queue worker can start.
    """

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "channels" in tables:
            conn.execute(
                """CREATE TEMP TABLE marlen_disabled_direct_groups(
                       account_id INTEGER NOT NULL,
                       channel_id INTEGER NOT NULL,
                       PRIMARY KEY(account_id, channel_id)
                   ) WITHOUT ROWID"""
            )
            channel_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(channels)")
            }
            if {
                "account_id",
                "channel_id",
                "target_kind",
                "comment_mode",
            } <= channel_columns:
                conn.execute(
                    """INSERT OR IGNORE INTO marlen_disabled_direct_groups(account_id, channel_id)
                       SELECT account_id, channel_id FROM channels
                       WHERE target_kind='group' AND comment_mode='direct_group'"""
                )

                # A queued direct-group slot must not be reinterpreted as a
                # channel-post route after the target mode changes.
                if {"tasks", "comment_schedule", "comment_campaigns"} <= tables:
                    conn.execute(
                        """UPDATE tasks
                           SET status=CASE
                                   WHEN status IN ('running','processing') THEN 'failed'
                                   ELSE 'cancelled'
                               END,
                               progress=100,
                               status_text=NULL,
                               error=CASE
                                   WHEN status IN ('running','processing') THEN
                                       'Legacy direct-group operation interrupted; result requires manual review'
                                   ELSE
                                       'Прямая отправка в обычные группы отключена'
                               END,
                               not_before=NULL,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id IN (
                               SELECT s.task_id
                               FROM comment_schedule s
                               JOIN comment_campaigns c ON c.id=s.campaign_id
                               JOIN marlen_disabled_direct_groups d
                                 ON d.account_id=c.account_id AND d.channel_id=s.channel_id
                               WHERE s.task_id IS NOT NULL
                           )
                             AND status IN ('pending','paused','running','processing')"""
                    )
                    conn.execute(
                        """UPDATE comment_schedule
                           SET status='cancelled',
                               result='Прямая отправка в обычные группы отключена',
                               executed_at=CURRENT_TIMESTAMP
                           WHERE id IN (
                               SELECT s.id
                               FROM comment_schedule s
                               JOIN comment_campaigns c ON c.id=s.campaign_id
                               JOIN marlen_disabled_direct_groups d
                                 ON d.account_id=c.account_id AND d.channel_id=s.channel_id
                           )
                             AND status IN ('pending','queued','running')"""
                    )

                conn.execute(
                    """UPDATE channels
                       SET comment_mode='pending',
                           linked_chat_id=NULL,
                           linked_chat_title=NULL,
                           link_status='Обычная группа · прямая отправка отключена',
                           last_sync_at=CURRENT_TIMESTAMP
                       WHERE target_kind='group' AND comment_mode='direct_group'"""
                )
            conn.execute("DROP TABLE marlen_disabled_direct_groups")

        if "tasks" in tables:
            task_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")
            }
            required_task_columns = {
                "type",
                "status",
                "progress",
                "status_text",
                "error",
                "not_before",
                "updated_at",
            }
            if required_task_columns <= task_columns:
                conn.execute(
                    """UPDATE tasks
                       SET status=CASE
                               WHEN status IN ('running','processing') THEN 'failed'
                               ELSE 'cancelled'
                           END,
                           progress=100,
                           status_text=NULL,
                           error=CASE
                               WHEN status IN ('running','processing') THEN
                                   'Legacy direct-message operation interrupted; result requires manual review'
                               ELSE
                                   'Прямая отправка в обычные группы отключена'
                           END,
                           not_before=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE type='direct_message'
                         AND status IN ('pending','paused','running','processing')"""
                )
        conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(25)")
        conn.execute("PRAGMA user_version = 25")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

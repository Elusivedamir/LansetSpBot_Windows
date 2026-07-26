from __future__ import annotations

from typing import TYPE_CHECKING

import logging

from storage.db_common import DatabaseError

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class LegacySchemaMigrationMixin(_MixinHost):
    def _upgrade_legacy_to_v13(self):
        """One-time compatibility upgrade for databases created before v13."""
        current_version = self.get_version()
        if current_version > self.SCHEMA_VERSION:
            raise DatabaseError(
                f"Database schema v{current_version} is newer than supported v{self.SCHEMA_VERSION}"
            )

        with self.get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS migrations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version INTEGER NOT NULL UNIQUE,
                    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS comment_deliveries(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL,
                    post_id INTEGER NOT NULL,
                    linked_chat_id INTEGER,
                    comment_message_id INTEGER,
                    text TEXT,
                    status TEXT NOT NULL DEFAULT 'sending',
                    error TEXT,
                    reserved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(channel_id, post_id)
                );
                CREATE TABLE IF NOT EXISTS saved_dialogs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_id INTEGER,
                    username TEXT,
                    title TEXT,
                    kind TEXT NOT NULL DEFAULT 'channel',
                    invite_link TEXT,
                    source_account_id INTEGER,
                    source_phone TEXT,
                    saved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(peer_id)
                );
                CREATE TABLE IF NOT EXISTS saved_dialog_memberships(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    saved_dialog_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(saved_dialog_id, account_id),
                    FOREIGN KEY(saved_dialog_id) REFERENCES saved_dialogs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS join_campaigns(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    started_at DATETIME NOT NULL,
                    ends_at DATETIME NOT NULL,
                    max_per_hour INTEGER NOT NULL DEFAULT 40,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    attempted_count INTEGER NOT NULL DEFAULT 0,
                    joined_count INTEGER NOT NULL DEFAULT 0,
                    pause_reason TEXT,
                    network_failure_count INTEGER NOT NULL DEFAULT 0,
                    network_retry_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS join_schedule(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    slot_index INTEGER NOT NULL,
                    scheduled_at DATETIME NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    task_id INTEGER,
                    saved_dialog_id INTEGER NOT NULL,
                    executed_at DATETIME,
                    result TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(campaign_id, slot_index),
                    FOREIGN KEY(campaign_id) REFERENCES join_campaigns(id) ON DELETE CASCADE,
                    FOREIGN KEY(saved_dialog_id) REFERENCES saved_dialogs(id) ON DELETE CASCADE
                );
            """)

            table_columns = {
                "tasks": {
                    "type": "TEXT NOT NULL DEFAULT 'noop'",
                    "payload": "TEXT",
                    "status": "TEXT NOT NULL DEFAULT 'pending'",
                    "progress": "INTEGER NOT NULL DEFAULT 0",
                    "status_text": "TEXT",
                    "error": "TEXT",
                    "retry_count": "INTEGER NOT NULL DEFAULT 0",
                    "max_retries": "INTEGER NOT NULL DEFAULT 3",
                    "created_at": "DATETIME",
                    "updated_at": "DATETIME",
                    "not_before": "DATETIME",
                },
                "channels": {
                    "channel_id": "INTEGER",
                    "username": "TEXT",
                    "title": "TEXT",
                    "linked_chat_id": "INTEGER",
                    "linked_chat_title": "TEXT",
                    "link_status": "TEXT",
                    "last_sync_at": "DATETIME",
                    "last_comment_check_at": "DATETIME",
                    "created_at": "DATETIME",
                },
                "messages": {
                    "channel_id": "INTEGER",
                    "message_id": "INTEGER",
                    "text": "TEXT",
                    "date": "TEXT",
                    "author_id": "INTEGER",
                    "created_at": "DATETIME",
                },
                "comments": {
                    "channel_id": "INTEGER",
                    "linked_chat_id": "INTEGER",
                    "post_message_id": "INTEGER",
                    "comment_message_id": "INTEGER",
                    "reply_to": "INTEGER",
                    "author_id": "INTEGER",
                    "text": "TEXT",
                    "date": "TEXT",
                },
                "comment_templates": {
                    "name": "TEXT",
                    "text_1": "TEXT",
                    "text_2": "TEXT",
                    "text_3": "TEXT",
                    "text_4": "TEXT",
                    "text_5": "TEXT",
                    "created_at": "DATETIME",
                    "updated_at": "DATETIME",
                },
                "comment_history": {
                    "task_id": "INTEGER",
                    "campaign_id": "INTEGER",
                    "slot_id": "INTEGER",
                    "channel_id": "INTEGER",
                    "post_id": "INTEGER",
                    "comment_text": "TEXT",
                    "sent_at": "DATETIME",
                    "status": "TEXT",
                },
                "comment_campaigns": {
                    "network_failure_count": "INTEGER NOT NULL DEFAULT 0",
                    "network_retry_at": "DATETIME",
                },
                "join_events": {
                    "campaign_id": "INTEGER",
                    "saved_dialog_id": "INTEGER",
                    "account_id": "INTEGER",
                },
                "logs": {"level": "TEXT", "message": "TEXT", "created_at": "DATETIME"},
                "settings": {"key": "TEXT", "value": "TEXT", "updated_at": "DATETIME"},
            }
            for table, additions in table_columns.items():
                existing = {
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                }
                for name, definition in additions.items():
                    if name not in existing:
                        conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                        )

            # A hard crash can leave a delivery in ``sending`` after Telegram
            # accepted the comment but before SQLite was finalized. Never release
            # that reservation automatically: mark it uncertain so duplicate
            # protection remains active and the GUI/audit can explain why.
            conn.execute(
                """UPDATE comment_deliveries
                   SET status='uncertain',
                       error=COALESCE(error, 'Recovered after unclean shutdown'),
                       updated_at=CURRENT_TIMESTAMP
                   WHERE status='sending'
                     AND reserved_at < datetime('now', '-5 minutes')"""
            )

            conn.execute(
                "UPDATE tasks SET created_at=COALESCE(created_at, CURRENT_TIMESTAMP)"
            )
            conn.execute(
                "UPDATE tasks SET updated_at=COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)"
            )
            conn.execute("""UPDATE tasks SET retry_count=0
                            WHERE retry_count IS NULL OR typeof(retry_count) != 'integer' OR retry_count < 0""")
            conn.execute("""UPDATE tasks SET max_retries=3
                            WHERE max_retries IS NULL OR typeof(max_retries) != 'integer' OR max_retries < 0""")
            conn.execute("""UPDATE tasks SET progress=CASE
                                WHEN progress IS NULL OR typeof(progress) != 'integer' THEN 0
                                WHEN progress < 0 THEN 0
                                WHEN progress > 100 THEN 100
                                ELSE progress END""")
            conn.execute("UPDATE tasks SET status='running' WHERE status='processing'")
            conn.execute(
                "UPDATE tasks SET status='completed' WHERE status IN ('done', 'complete')"
            )
            conn.execute("""UPDATE tasks SET status='failed'
                            WHERE status NOT IN ('pending','running','paused','completed','failed','cancelled')
                               OR status IS NULL""")

            # Older schemas did not always enforce logical uniqueness. Keep the
            # newest row before adding unique indexes used by UPSERT operations.
            conn.execute("""DELETE FROM channels WHERE channel_id IS NOT NULL AND id NOT IN
                            (SELECT MAX(id) FROM channels WHERE channel_id IS NOT NULL GROUP BY channel_id)""")
            conn.execute("""DELETE FROM messages WHERE id NOT IN
                            (SELECT MAX(id) FROM messages GROUP BY channel_id, message_id)""")
            conn.execute("""DELETE FROM comments WHERE comment_message_id IS NOT NULL AND id NOT IN
                            (SELECT MAX(id) FROM comments WHERE comment_message_id IS NOT NULL
                             GROUP BY channel_id, comment_message_id)""")
            conn.execute("""DELETE FROM comment_templates WHERE name IS NOT NULL AND id NOT IN
                            (SELECT MAX(id) FROM comment_templates WHERE name IS NOT NULL GROUP BY name)""")
            conn.execute("""DELETE FROM settings WHERE key IS NOT NULL AND id NOT IN
                            (SELECT MAX(id) FROM settings WHERE key IS NOT NULL GROUP BY key)""")

            indexes = (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_channels_channel_id ON channels(channel_id)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_channel_message ON messages(channel_id, message_id)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_comments_channel_message ON comments(channel_id, comment_message_id)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_templates_name ON comment_templates(name)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_settings_key ON settings(key)",
                "CREATE INDEX IF NOT EXISTS idx_channels_id ON channels(channel_id)",
                "CREATE INDEX IF NOT EXISTS idx_channels_comment_rotation ON channels(linked_chat_id, last_comment_check_at)",
                "CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id)",
                "CREATE INDEX IF NOT EXISTS idx_comments_channel ON comments(channel_id)",
                "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
                "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at)",
                "CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(status, created_at, id)",
                "CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)",
                "CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at)",
                "CREATE INDEX IF NOT EXISTS idx_campaign_status_end ON comment_campaigns(status, ends_at)",
                "CREATE INDEX IF NOT EXISTS idx_campaign_network_retry ON comment_campaigns(status, network_retry_at)",
                "CREATE INDEX IF NOT EXISTS idx_schedule_due ON comment_schedule(status, scheduled_at)",
                "CREATE INDEX IF NOT EXISTS idx_schedule_campaign ON comment_schedule(campaign_id, slot_index)",
                "CREATE INDEX IF NOT EXISTS idx_join_events_time ON join_events(joined_at)",
                "CREATE INDEX IF NOT EXISTS idx_history_campaign ON comment_history(campaign_id, id)",
                "CREATE INDEX IF NOT EXISTS idx_tasks_type_status ON tasks(type, status, created_at)",
                "CREATE INDEX IF NOT EXISTS idx_delivery_lookup ON comment_deliveries(channel_id, post_id, status)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_dialog_username ON saved_dialogs(username) WHERE username IS NOT NULL AND username<>''",
                "CREATE INDEX IF NOT EXISTS idx_saved_dialog_source ON saved_dialogs(source_account_id, last_seen_at)",
                "CREATE INDEX IF NOT EXISTS idx_saved_membership_account ON saved_dialog_memberships(account_id, status)",
                "CREATE INDEX IF NOT EXISTS idx_join_campaign_status ON join_campaigns(status, network_retry_at)",
                "CREATE INDEX IF NOT EXISTS idx_join_schedule_due ON join_schedule(status, scheduled_at)",
                "CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, not_before, created_at)",
                "CREATE INDEX IF NOT EXISTS idx_join_schedule_campaign ON join_schedule(campaign_id, slot_index)",
            )
            for statement in indexes:
                conn.execute(statement)

            for version in range(1, self.LEGACY_SCHEMA_VERSION + 1):
                conn.execute(
                    "INSERT OR IGNORE INTO migrations(version) VALUES(?)", (version,)
                )
            if current_version < self.LEGACY_SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {self.LEGACY_SCHEMA_VERSION}")

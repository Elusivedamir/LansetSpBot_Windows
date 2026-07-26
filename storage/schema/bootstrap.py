from __future__ import annotations

from typing import TYPE_CHECKING

import logging

from core.config import DEFAULT_MAX_CHANNELS_PER_RUN
from storage.db_common import DatabaseError

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class SchemaBootstrapMixin(_MixinHost):
    def init(self):
        """Initialize database with all required tables."""
        try:
            with self.get_connection() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(f"""
                    CREATE TABLE IF NOT EXISTS comment_templates(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        text_1 TEXT, text_2 TEXT, text_3 TEXT,
                        text_4 TEXT, text_5 TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS account_comment_templates(
                        account_id INTEGER PRIMARY KEY,
                        visible_count INTEGER NOT NULL DEFAULT 10
                            CHECK(visible_count BETWEEN 1 AND 10),
                        text_1 TEXT, text_2 TEXT, text_3 TEXT, text_4 TEXT, text_5 TEXT,
                        text_6 TEXT, text_7 TEXT, text_8 TEXT, text_9 TEXT, text_10 TEXT,
                        bag_fingerprint TEXT NOT NULL DEFAULT '',
                        bag_order_json TEXT NOT NULL DEFAULT '[]',
                        bag_position INTEGER NOT NULL DEFAULT 0 CHECK(bag_position >= 0),
                        last_variant_index INTEGER,
                        last_used_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_comment_templates_last_used
                        ON account_comment_templates(last_used_at DESC, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS comment_history(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id INTEGER,
                        campaign_id INTEGER,
                        slot_id INTEGER,
                        channel_id INTEGER,
                        post_id INTEGER,
                        comment_text TEXT,
                        sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        status TEXT
                    );

                    CREATE TABLE IF NOT EXISTS comment_limits(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        date TEXT NOT NULL UNIQUE,
                        sent_count INTEGER DEFAULT 0,
                        daily_limit INTEGER DEFAULT {DEFAULT_MAX_CHANNELS_PER_RUN}
                    );

                    CREATE TABLE IF NOT EXISTS comment_campaigns(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        status TEXT NOT NULL DEFAULT 'running',
                        started_at DATETIME NOT NULL,
                        ends_at DATETIME NOT NULL,
                        daily_limit INTEGER NOT NULL DEFAULT 40,
                        cadence_seconds REAL NOT NULL DEFAULT 2160.0,
                        continuous INTEGER NOT NULL DEFAULT 1,
                        comments_json TEXT NOT NULL,
                        attempted_count INTEGER NOT NULL DEFAULT 0,
                        sent_count INTEGER NOT NULL DEFAULT 0,
                        last_comment_text TEXT,
                        pause_reason TEXT,
                        network_failure_count INTEGER NOT NULL DEFAULT 0,
                        network_retry_at DATETIME,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS comment_schedule(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        campaign_id INTEGER NOT NULL,
                        slot_index INTEGER NOT NULL,
                        scheduled_at DATETIME NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        task_id INTEGER,
                        channel_id INTEGER,
                        post_id INTEGER,
                        linked_chat_id INTEGER,
                        discussion_message_id INTEGER,
                        route_cached_at DATETIME,
                        selected_text TEXT,
                        selected_variant_index INTEGER,
                        executed_at DATETIME,
                        result TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(campaign_id, slot_index),
                        FOREIGN KEY(campaign_id) REFERENCES comment_campaigns(id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS join_events(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        linked_chat_id INTEGER,
                        joined_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        result TEXT NOT NULL DEFAULT 'joined',
                        campaign_id INTEGER,
                        saved_dialog_id INTEGER,
                        account_id INTEGER
                    );

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
                        peer_id INTEGER UNIQUE,
                        username TEXT,
                        title TEXT,
                        kind TEXT NOT NULL DEFAULT 'channel',
                        invite_link TEXT,
                        source_account_id INTEGER,
                        source_phone TEXT,
                        saved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP
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

                    CREATE TABLE IF NOT EXISTS channels(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id INTEGER NOT NULL UNIQUE,
                        username TEXT,
                        title TEXT,
                        target_kind TEXT NOT NULL DEFAULT 'channel',
                        comment_mode TEXT NOT NULL DEFAULT 'channel_post',
                        linked_chat_id INTEGER,
                        linked_chat_title TEXT,
                        link_status TEXT,
                        link_checked_at DATETIME,
                        last_sync_at DATETIME,
                        last_comment_check_at DATETIME,
                        access_hash INTEGER,
                        peer_type TEXT,
                        negative_status TEXT,
                        negative_until DATETIME,
                        local_ban_reason TEXT,
                        local_ban_peer_id INTEGER,
                        local_banned_at DATETIME,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS messages(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        text TEXT,
                        date TEXT,
                        author_id INTEGER,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(channel_id, message_id)
                    );

                    CREATE TABLE IF NOT EXISTS comments(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id INTEGER NOT NULL,
                        linked_chat_id INTEGER NOT NULL,
                        post_message_id INTEGER NOT NULL,
                        comment_message_id INTEGER,
                        reply_to INTEGER,
                        author_id INTEGER,
                        text TEXT,
                        date TEXT,
                        UNIQUE(channel_id, comment_message_id)
                    );

                    CREATE TABLE IF NOT EXISTS settings(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        key TEXT NOT NULL UNIQUE,
                        value TEXT,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS tasks(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        type TEXT NOT NULL,
                        payload TEXT,
                        status TEXT DEFAULT 'pending',
                        progress INTEGER DEFAULT 0,
                        status_text TEXT,
                        error TEXT,
                        retry_count INTEGER DEFAULT 0,
                        max_retries INTEGER DEFAULT 3,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        not_before DATETIME
                    );

                    CREATE TABLE IF NOT EXISTS logs(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_id INTEGER NOT NULL DEFAULT 0,
                        level TEXT NOT NULL,
                        message TEXT NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_logs_account_id_id
                        ON logs(account_id, id DESC);

                    CREATE TABLE IF NOT EXISTS account_rpc_cooldowns(
                        account_id INTEGER PRIMARY KEY,
                        next_allowed_at DATETIME NOT NULL,
                        code TEXT NOT NULL DEFAULT 'flood_wait_deferred',
                        source_task_id INTEGER,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_account_rpc_cooldowns_due
                        ON account_rpc_cooldowns(next_allowed_at);

                    CREATE TABLE IF NOT EXISTS account_restrictions(
                        account_id INTEGER PRIMARY KEY,
                        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                        code TEXT NOT NULL,
                        message TEXT,
                        detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        checked_at DATETIME,
                        details_json TEXT NOT NULL DEFAULT '{{}}',
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_account_restrictions_active
                        ON account_restrictions(active, updated_at);

                """)
            log.info("Database initialized successfully")
        except DatabaseError as e:
            log.error("Failed to initialize database: %s", e)
            raise
        except Exception as e:
            log.error("Unexpected error during database initialization: %s", e)
            raise DatabaseError(f"Database initialization failed: {e}") from e

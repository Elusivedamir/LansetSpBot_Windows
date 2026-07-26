from __future__ import annotations

from typing import TYPE_CHECKING

import logging
from storage.sqlcipher_driver import dbapi as sqlite3


log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class SchemaV14MigrationMixin(_MixinHost):
    def _migrate_to_v14(self):
        """Apply the v14 hardening migration exactly once and atomically.

        SQLite cannot add foreign-key clauses to existing columns.  To avoid a
        destructive table rebuild on user databases, v14 enforces the missing
        relationships with equivalent triggers and repairs existing orphaned
        rows before the triggers are installed.
        """
        conn = sqlite3.connect(
            str(self.path),
            timeout=self.sqlite_timeout_seconds,
            isolation_level=None,
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("BEGIN IMMEDIATE")

            task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            additions = {
                "defer_count": "INTEGER NOT NULL DEFAULT 0",
                "first_deferred_at": "DATETIME",
                "last_deferred_at": "DATETIME",
            }
            for name, definition in additions.items():
                if name not in task_columns:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")

            conn.execute(
                """UPDATE tasks SET defer_count=0
                   WHERE defer_count IS NULL
                      OR typeof(defer_count)!='integer'
                      OR defer_count<0"""
            )

            # Repair legacy orphan rows before enforcing future writes.
            for statement in (
                "UPDATE comment_schedule SET task_id=NULL WHERE task_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM tasks WHERE tasks.id=comment_schedule.task_id)",
                "UPDATE join_schedule SET task_id=NULL WHERE task_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM tasks WHERE tasks.id=join_schedule.task_id)",
                "UPDATE comment_history SET task_id=NULL WHERE task_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM tasks WHERE tasks.id=comment_history.task_id)",
                "UPDATE comment_history SET campaign_id=NULL WHERE campaign_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM comment_campaigns WHERE comment_campaigns.id=comment_history.campaign_id)",
                "UPDATE comment_history SET slot_id=NULL WHERE slot_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM comment_schedule WHERE comment_schedule.id=comment_history.slot_id)",
                "DELETE FROM messages WHERE NOT EXISTS(SELECT 1 FROM channels WHERE channels.channel_id=messages.channel_id)",
                "DELETE FROM comments WHERE NOT EXISTS(SELECT 1 FROM channels WHERE channels.channel_id=comments.channel_id)",
                "UPDATE join_events SET campaign_id=NULL WHERE campaign_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM join_campaigns WHERE join_campaigns.id=join_events.campaign_id)",
                "UPDATE join_events SET saved_dialog_id=NULL WHERE saved_dialog_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM saved_dialogs WHERE saved_dialogs.id=join_events.saved_dialog_id)",
            ):
                conn.execute(statement)

            # Query-path indexes found missing during the 4.7.4 audit.
            for statement in (
                "CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(channel_id, post_message_id)",
                "CREATE INDEX IF NOT EXISTS idx_comment_history_task ON comment_history(task_id, id)",
                "CREATE INDEX IF NOT EXISTS idx_comment_schedule_campaign_status_due ON comment_schedule(campaign_id, status, scheduled_at)",
                "CREATE INDEX IF NOT EXISTS idx_comment_schedule_active_task ON comment_schedule(status, task_id)",
                "CREATE INDEX IF NOT EXISTS idx_join_schedule_campaign_status_due ON join_schedule(campaign_id, status, scheduled_at)",
                "CREATE INDEX IF NOT EXISTS idx_join_schedule_active_task ON join_schedule(status, task_id)",
                "CREATE INDEX IF NOT EXISTS idx_tasks_retention ON tasks(status, updated_at)",
                "CREATE INDEX IF NOT EXISTS idx_delivery_recovery ON comment_deliveries(status, reserved_at)",
            ):
                conn.execute(statement)

            # Remove redundant indexes only when another index already guarantees
            # the same key. This preserves uniqueness on very old databases where
            # the explicit uq_* index may be the only constraint.
            def index_columns(index_name):
                return tuple(
                    row[2]
                    for row in conn.execute(
                        f"PRAGMA index_info({index_name})"
                    ).fetchall()
                )

            duplicate_unique_indexes = (
                ("channels", "uq_channels_channel_id"),
                ("messages", "uq_messages_channel_message"),
                ("comments", "uq_comments_channel_message"),
                ("comment_templates", "uq_templates_name"),
                ("settings", "uq_settings_key"),
            )
            for table, candidate in duplicate_unique_indexes:
                candidate_columns = index_columns(candidate)
                if not candidate_columns:
                    continue
                alternatives = conn.execute(f"PRAGMA index_list({table})").fetchall()
                if any(
                    str(row[1]) != candidate
                    and int(row[2]) == 1
                    and index_columns(str(row[1])) == candidate_columns
                    for row in alternatives
                ):
                    conn.execute(f"DROP INDEX IF EXISTS {candidate}")

            for name in (
                "idx_channels_id",
                "idx_messages_channel",
                "idx_comments_channel",
                "idx_schedule_campaign",
                "idx_join_schedule_campaign",
            ):
                conn.execute(f"DROP INDEX IF EXISTS {name}")

            trigger_statements = (
                "DROP TRIGGER IF EXISTS fk_comment_schedule_task_insert",
                "DROP TRIGGER IF EXISTS fk_comment_schedule_task_update",
                "DROP TRIGGER IF EXISTS fk_join_schedule_task_insert",
                "DROP TRIGGER IF EXISTS fk_join_schedule_task_update",
                "DROP TRIGGER IF EXISTS fk_comment_history_insert",
                "DROP TRIGGER IF EXISTS fk_comment_history_update",
                "DROP TRIGGER IF EXISTS fk_messages_channel_insert",
                "DROP TRIGGER IF EXISTS fk_messages_channel_update",
                "DROP TRIGGER IF EXISTS fk_comments_channel_insert",
                "DROP TRIGGER IF EXISTS fk_comments_channel_update",
                "DROP TRIGGER IF EXISTS fk_join_events_insert",
                "DROP TRIGGER IF EXISTS fk_join_events_update",
                "DROP TRIGGER IF EXISTS cascade_channel_children",
                "DROP TRIGGER IF EXISTS null_task_references",
                "DROP TRIGGER IF EXISTS null_comment_campaign_history",
                "DROP TRIGGER IF EXISTS null_comment_slot_history",
                "DROP TRIGGER IF EXISTS null_join_campaign_events",
                "DROP TRIGGER IF EXISTS null_saved_dialog_events",
                "DROP TRIGGER IF EXISTS validate_task_insert",
                "DROP TRIGGER IF EXISTS validate_task_update",
                """CREATE TRIGGER fk_comment_schedule_task_insert
                   BEFORE INSERT ON comment_schedule
                   WHEN NEW.task_id IS NOT NULL
                    AND NOT EXISTS(SELECT 1 FROM tasks WHERE id=NEW.task_id)
                   BEGIN SELECT RAISE(ABORT, 'comment_schedule.task_id references missing task'); END""",
                """CREATE TRIGGER fk_comment_schedule_task_update
                   BEFORE UPDATE OF task_id ON comment_schedule
                   WHEN NEW.task_id IS NOT NULL
                    AND NOT EXISTS(SELECT 1 FROM tasks WHERE id=NEW.task_id)
                   BEGIN SELECT RAISE(ABORT, 'comment_schedule.task_id references missing task'); END""",
                """CREATE TRIGGER fk_join_schedule_task_insert
                   BEFORE INSERT ON join_schedule
                   WHEN NEW.task_id IS NOT NULL
                    AND NOT EXISTS(SELECT 1 FROM tasks WHERE id=NEW.task_id)
                   BEGIN SELECT RAISE(ABORT, 'join_schedule.task_id references missing task'); END""",
                """CREATE TRIGGER fk_join_schedule_task_update
                   BEFORE UPDATE OF task_id ON join_schedule
                   WHEN NEW.task_id IS NOT NULL
                    AND NOT EXISTS(SELECT 1 FROM tasks WHERE id=NEW.task_id)
                   BEGIN SELECT RAISE(ABORT, 'join_schedule.task_id references missing task'); END""",
                """CREATE TRIGGER fk_comment_history_insert
                   BEFORE INSERT ON comment_history
                   WHEN (NEW.task_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM tasks WHERE id=NEW.task_id))
                     OR (NEW.campaign_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM comment_campaigns WHERE id=NEW.campaign_id))
                     OR (NEW.slot_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM comment_schedule WHERE id=NEW.slot_id))
                   BEGIN SELECT RAISE(ABORT, 'comment_history contains missing reference'); END""",
                """CREATE TRIGGER fk_comment_history_update
                   BEFORE UPDATE OF task_id, campaign_id, slot_id ON comment_history
                   WHEN (NEW.task_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM tasks WHERE id=NEW.task_id))
                     OR (NEW.campaign_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM comment_campaigns WHERE id=NEW.campaign_id))
                     OR (NEW.slot_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM comment_schedule WHERE id=NEW.slot_id))
                   BEGIN SELECT RAISE(ABORT, 'comment_history contains missing reference'); END""",
                """CREATE TRIGGER fk_messages_channel_insert
                   BEFORE INSERT ON messages
                   WHEN NOT EXISTS(SELECT 1 FROM channels WHERE channel_id=NEW.channel_id)
                   BEGIN SELECT RAISE(ABORT, 'messages.channel_id references missing channel'); END""",
                """CREATE TRIGGER fk_messages_channel_update
                   BEFORE UPDATE OF channel_id ON messages
                   WHEN NOT EXISTS(SELECT 1 FROM channels WHERE channel_id=NEW.channel_id)
                   BEGIN SELECT RAISE(ABORT, 'messages.channel_id references missing channel'); END""",
                """CREATE TRIGGER fk_comments_channel_insert
                   BEFORE INSERT ON comments
                   WHEN NOT EXISTS(SELECT 1 FROM channels WHERE channel_id=NEW.channel_id)
                   BEGIN SELECT RAISE(ABORT, 'comments.channel_id references missing channel'); END""",
                """CREATE TRIGGER fk_comments_channel_update
                   BEFORE UPDATE OF channel_id ON comments
                   WHEN NOT EXISTS(SELECT 1 FROM channels WHERE channel_id=NEW.channel_id)
                   BEGIN SELECT RAISE(ABORT, 'comments.channel_id references missing channel'); END""",
                """CREATE TRIGGER fk_join_events_insert
                   BEFORE INSERT ON join_events
                   WHEN (NEW.campaign_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM join_campaigns WHERE id=NEW.campaign_id))
                     OR (NEW.saved_dialog_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM saved_dialogs WHERE id=NEW.saved_dialog_id))
                   BEGIN SELECT RAISE(ABORT, 'join_events contains missing reference'); END""",
                """CREATE TRIGGER fk_join_events_update
                   BEFORE UPDATE OF campaign_id, saved_dialog_id ON join_events
                   WHEN (NEW.campaign_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM join_campaigns WHERE id=NEW.campaign_id))
                     OR (NEW.saved_dialog_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM saved_dialogs WHERE id=NEW.saved_dialog_id))
                   BEGIN SELECT RAISE(ABORT, 'join_events contains missing reference'); END""",
                """CREATE TRIGGER cascade_channel_children AFTER DELETE ON channels
                   BEGIN DELETE FROM messages WHERE channel_id=OLD.channel_id;
                         DELETE FROM comments WHERE channel_id=OLD.channel_id; END""",
                """CREATE TRIGGER null_task_references AFTER DELETE ON tasks
                   BEGIN UPDATE comment_schedule SET task_id=NULL WHERE task_id=OLD.id;
                         UPDATE join_schedule SET task_id=NULL WHERE task_id=OLD.id;
                         UPDATE comment_history SET task_id=NULL WHERE task_id=OLD.id; END""",
                """CREATE TRIGGER null_comment_campaign_history AFTER DELETE ON comment_campaigns
                   BEGIN UPDATE comment_history SET campaign_id=NULL WHERE campaign_id=OLD.id; END""",
                """CREATE TRIGGER null_comment_slot_history AFTER DELETE ON comment_schedule
                   BEGIN UPDATE comment_history SET slot_id=NULL WHERE slot_id=OLD.id; END""",
                """CREATE TRIGGER null_join_campaign_events AFTER DELETE ON join_campaigns
                   BEGIN UPDATE join_events SET campaign_id=NULL WHERE campaign_id=OLD.id; END""",
                """CREATE TRIGGER null_saved_dialog_events AFTER DELETE ON saved_dialogs
                   BEGIN UPDATE join_events SET saved_dialog_id=NULL WHERE saved_dialog_id=OLD.id; END""",
                """CREATE TRIGGER validate_task_insert BEFORE INSERT ON tasks
                   WHEN NEW.status IS NULL
                     OR NEW.status NOT IN ('pending','running','paused','completed','failed','cancelled')
                     OR NEW.progress IS NULL OR NEW.progress NOT BETWEEN 0 AND 100
                     OR NEW.retry_count IS NULL OR NEW.retry_count < 0
                     OR NEW.max_retries IS NULL OR NEW.max_retries < 0
                     OR NEW.defer_count IS NULL OR NEW.defer_count < 0
                   BEGIN SELECT RAISE(ABORT, 'invalid task state'); END""",
                """CREATE TRIGGER validate_task_update
                   BEFORE UPDATE OF status, progress, retry_count, max_retries, defer_count ON tasks
                   WHEN NEW.status IS NULL
                     OR NEW.status NOT IN ('pending','running','paused','completed','failed','cancelled')
                     OR NEW.progress IS NULL OR NEW.progress NOT BETWEEN 0 AND 100
                     OR NEW.retry_count IS NULL OR NEW.retry_count < 0
                     OR NEW.max_retries IS NULL OR NEW.max_retries < 0
                     OR NEW.defer_count IS NULL OR NEW.defer_count < 0
                   BEGIN SELECT RAISE(ABORT, 'invalid task state'); END""",
            )
            for statement in trigger_statements:
                conn.execute(statement)

            conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(14)")
            conn.execute("PRAGMA user_version = 14")
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

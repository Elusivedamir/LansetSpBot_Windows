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


class SchemaV15MigrationMixin(_MixinHost):
    def _migrate_to_v15(self):
        """Harden join identity/state and add direct-message delivery receipts."""
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

            conn.execute(
                """CREATE TABLE IF NOT EXISTS direct_message_deliveries(
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       task_id INTEGER NOT NULL UNIQUE,
                       chat_id TEXT NOT NULL,
                       text TEXT NOT NULL,
                       message_id INTEGER,
                       status TEXT NOT NULL DEFAULT 'sending'
                           CHECK(status IN ('sending','sent','uncertain','failed')),
                       error TEXT,
                       reserved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                       updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                   )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_direct_delivery_status "
                "ON direct_message_deliveries(status, updated_at)"
            )

            # A username is mutable Telegram metadata, never the durable identity.
            # Keep the newest owner and clear stale duplicates before installing a
            # case-insensitive uniqueness constraint. Historical peer rows remain
            # intact, so memberships and join events are never reassigned.
            duplicate_names = conn.execute(
                """SELECT lower(username) AS normalized
                   FROM saved_dialogs
                   WHERE username IS NOT NULL AND trim(username)<>''
                   GROUP BY lower(username) HAVING COUNT(*)>1"""
            ).fetchall()
            for duplicate in duplicate_names:
                rows = conn.execute(
                    """SELECT id FROM saved_dialogs
                       WHERE lower(username)=?
                       ORDER BY COALESCE(last_seen_at, saved_at) DESC, id DESC""",
                    (duplicate["normalized"],),
                ).fetchall()
                for row in rows[1:]:
                    conn.execute(
                        "UPDATE saved_dialogs SET username=NULL WHERE id=?",
                        (int(row["id"]),),
                    )
            conn.execute("DROP INDEX IF EXISTS uq_saved_dialog_username")
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_dialog_username_ci
                   ON saved_dialogs(lower(username))
                   WHERE username IS NOT NULL AND trim(username)<>''"""
            )

            # Repair duplicate active campaigns from older versions, then enforce
            # the invariant at the schema level for every process/thread.
            active_rows = conn.execute(
                """SELECT id FROM join_campaigns
                   WHERE status IN ('running','paused','network_wait')
                   ORDER BY id DESC"""
            ).fetchall()
            for row in active_rows[1:]:
                campaign_id = int(row["id"])
                conn.execute(
                    """UPDATE join_campaigns
                       SET status='stopped',
                           pause_reason='Остановлено миграцией: дублирующая активная кампания',
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (campaign_id,),
                )
                conn.execute(
                    """UPDATE join_schedule
                       SET status='cancelled', task_id=NULL,
                           result='Остановлено миграцией: дублирующая активная кампания',
                           executed_at=CURRENT_TIMESTAMP
                       WHERE campaign_id=? AND status IN ('pending','queued','running')""",
                    (campaign_id,),
                )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS uq_join_campaign_active
                   ON join_campaigns((1))
                   WHERE status IN ('running','paused','network_wait')"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_join_events_account_time "
                "ON join_events(account_id, joined_at)"
            )

            for name in (
                "validate_join_schedule_insert",
                "validate_join_schedule_update",
                "validate_join_campaign_insert",
                "validate_join_campaign_update",
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute(
                """CREATE TRIGGER validate_join_schedule_insert
                   BEFORE INSERT ON join_schedule
                   WHEN NEW.status NOT IN ('pending','queued','running','joined',
                       'already_member','skipped','failed','uncertain','cancelled')
                   BEGIN SELECT RAISE(ABORT, 'invalid join schedule status'); END"""
            )
            conn.execute(
                """CREATE TRIGGER validate_join_schedule_update
                   BEFORE UPDATE OF status ON join_schedule
                   WHEN NEW.status NOT IN ('pending','queued','running','joined',
                       'already_member','skipped','failed','uncertain','cancelled')
                   BEGIN SELECT RAISE(ABORT, 'invalid join schedule status'); END"""
            )
            conn.execute(
                """CREATE TRIGGER validate_join_campaign_insert
                   BEFORE INSERT ON join_campaigns
                   WHEN NEW.status NOT IN ('running','paused','network_wait','completed','stopped')
                   BEGIN SELECT RAISE(ABORT, 'invalid join campaign status'); END"""
            )
            conn.execute(
                """CREATE TRIGGER validate_join_campaign_update
                   BEFORE UPDATE OF status ON join_campaigns
                   WHEN NEW.status NOT IN ('running','paused','network_wait','completed','stopped')
                   BEGIN SELECT RAISE(ABORT, 'invalid join campaign status'); END"""
            )

            conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(15)")
            conn.execute("PRAGMA user_version = 15")
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

from __future__ import annotations

from typing import TYPE_CHECKING

import json
from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path

if TYPE_CHECKING:  # pragma: no cover - typing only
    # ``sqlite3`` is bound to the SQLCipher DBAPI proxy object, not to a
    # module, so its DBAPI classes are imported from the standard library
    # for annotations. The two drivers are DBAPI-compatible.
    from sqlite3 import Connection as SQLiteConnection



def _current_account_id(conn: SQLiteConnection) -> int:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
    ).fetchone()
    if table is None:
        return 0
    row = conn.execute(
        "SELECT value FROM settings WHERE key='telegram.account_id'"
    ).fetchone()
    if row is None:
        return 0
    try:
        return max(0, int(row[0] or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _execute_script(conn: SQLiteConnection, script: str) -> None:
    """Execute a SQL script without sqlite3.executescript's implicit COMMIT."""
    statement = ""
    for line in script.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                conn.execute(sql)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("Incomplete migration SQL statement")


def migrate_account_isolation_v18(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Scope comment campaigns, targets and delivery receipts to one account.

    Pre-v18 databases had a single global comment target list and a delivery key
    of ``(channel_id, post_id)``.  A campaign/task therefore lost its owner when
    the locally selected Telegram account changed.  This migration snapshots the
    currently authorized account onto legacy rows and rebuilds the affected
    unique keys as ``account_id + Telegram identity``.

    When no account is currently recorded, legacy rows receive account ``0``.
    Such campaigns are paused and workers fail closed until the user authorizes
    an account and starts a new campaign; no Telegram action is guessed/replayed.
    """

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   key TEXT NOT NULL UNIQUE,
                   value TEXT,
                   updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        account_id = _current_account_id(conn)

        existing_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required_full_tables = {
            "comment_campaigns",
            "comment_schedule",
            "comment_history",
            "channels",
            "messages",
            "comments",
            "comment_deliveries",
            "tasks",
            "join_campaigns",
            "join_schedule",
            "join_events",
        }
        if not required_full_tables.issubset(existing_tables):
            # Defensive compatibility for deliberately minimal historical test or
            # diagnostic databases. Scope every available target table, but do not
            # invent missing domain rows. A normal Marlen database always follows
            # the full migration path below.
            if "channels" in existing_tables:
                columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(channels)")
                }
                if "account_id" not in columns:
                    conn.execute(
                        "ALTER TABLE channels ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0"
                    )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_channels_account_peer "
                    "ON channels(account_id, channel_id)"
                )
            if "comment_deliveries" in existing_tables:
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(comment_deliveries)")
                }
                if "account_id" not in columns:
                    conn.execute(
                        "ALTER TABLE comment_deliveries "
                        "ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0"
                    )
            conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(18)")
            conn.execute("PRAGMA user_version = 18")
            conn.execute("COMMIT")
            return

        campaign_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(comment_campaigns)")
        }
        already_scoped = "account_id" in campaign_columns and all(
            "account_id"
            in {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
            for table in (
                "channels",
                "messages",
                "comments",
                "comment_deliveries",
                "comment_history",
            )
        )
        if already_scoped:
            if account_id > 0:
                conn.execute(
                    "UPDATE join_events SET account_id=? WHERE account_id IS NULL",
                    (account_id,),
                )
            conn.execute("DROP TRIGGER IF EXISTS validate_join_schedule_insert")
            conn.execute("DROP TRIGGER IF EXISTS validate_join_schedule_update")
            _execute_script(
                conn,
                """
                CREATE TRIGGER validate_join_schedule_insert
                BEFORE INSERT ON join_schedule
                WHEN NEW.status NOT IN (
                    'pending','queued','running','joined','already_member',
                    'join_requested','skipped','failed','uncertain','cancelled')
                BEGIN SELECT RAISE(ABORT, 'invalid join schedule status'); END;
                CREATE TRIGGER validate_join_schedule_update
                BEFORE UPDATE OF status ON join_schedule
                WHEN NEW.status NOT IN (
                    'pending','queued','running','joined','already_member',
                    'join_requested','skipped','failed','uncertain','cancelled')
                BEGIN SELECT RAISE(ABORT, 'invalid join schedule status'); END;
                """,
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_comment_campaign_account_status "
                "ON comment_campaigns(account_id, status, ends_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_comment_history_account_campaign "
                "ON comment_history(account_id, campaign_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_channels_account_peer "
                "ON channels(account_id, channel_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_comments_account_post "
                "ON comments(account_id, channel_id, post_message_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_delivery_lookup "
                "ON comment_deliveries(account_id, channel_id, post_id, status)"
            )
            conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(18)")
            conn.execute("PRAGMA user_version = 18")
            conn.execute("COMMIT")
            return

        if "account_id" not in campaign_columns:
            conn.execute(
                "ALTER TABLE comment_campaigns "
                "ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            "UPDATE comment_campaigns SET account_id=? WHERE account_id IS NULL OR account_id=0",
            (account_id,),
        )
        if account_id <= 0:
            conn.execute(
                """UPDATE comment_campaigns
                   SET status='paused',
                       pause_reason='Кампания приостановлена миграцией: владелец Telegram-аккаунта не определён',
                       network_retry_at=NULL,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE status IN ('running','network_wait','cycle_wait')"""
            )

        history_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(comment_history)")
        }
        if "account_id" not in history_columns:
            conn.execute(
                "ALTER TABLE comment_history "
                "ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """UPDATE comment_history
               SET account_id=COALESCE(
                   (SELECT account_id FROM comment_campaigns c
                    WHERE c.id=comment_history.campaign_id), ?)
               WHERE account_id IS NULL OR account_id=0""",
            (account_id,),
        )

        for trigger in (
            "cascade_channel_children",
            "fk_messages_channel_insert",
            "fk_messages_channel_update",
            "fk_comments_channel_insert",
            "fk_comments_channel_update",
            "validate_comment_history_account_insert",
            "validate_comment_history_account_update",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")

        _execute_script(
            conn,
            """
            CREATE TABLE channels_v18(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL DEFAULT 0,
                channel_id INTEGER NOT NULL,
                username TEXT,
                title TEXT,
                target_kind TEXT NOT NULL DEFAULT 'channel',
                comment_mode TEXT NOT NULL DEFAULT 'channel_post',
                linked_chat_id INTEGER,
                linked_chat_title TEXT,
                link_status TEXT,
                last_sync_at DATETIME,
                last_comment_check_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_id, channel_id)
            );

            CREATE TABLE messages_v18(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL DEFAULT 0,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                text TEXT,
                date TEXT,
                author_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_id, channel_id, message_id)
            );

            CREATE TABLE comments_v18(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL DEFAULT 0,
                channel_id INTEGER NOT NULL,
                linked_chat_id INTEGER NOT NULL,
                post_message_id INTEGER NOT NULL,
                comment_message_id INTEGER,
                reply_to INTEGER,
                author_id INTEGER,
                text TEXT,
                date TEXT,
                UNIQUE(account_id, channel_id, comment_message_id)
            );

            CREATE TABLE comment_deliveries_v18(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL DEFAULT 0,
                channel_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                linked_chat_id INTEGER,
                comment_message_id INTEGER,
                text TEXT,
                status TEXT NOT NULL DEFAULT 'sending',
                error TEXT,
                reserved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_id, channel_id, post_id)
            );
            """,
        )
        conn.execute(
            """INSERT INTO channels_v18(
                   id, account_id, channel_id, username, title, target_kind,
                   comment_mode, linked_chat_id, linked_chat_title, link_status,
                   last_sync_at, last_comment_check_at, created_at)
               SELECT id, ?, channel_id, username, title, target_kind,
                      comment_mode, linked_chat_id, linked_chat_title, link_status,
                      last_sync_at, last_comment_check_at, created_at
               FROM channels""",
            (account_id,),
        )
        conn.execute(
            """INSERT INTO messages_v18(
                   id, account_id, channel_id, message_id, text, date, author_id, created_at)
               SELECT id, ?, channel_id, message_id, text, date, author_id, created_at
               FROM messages""",
            (account_id,),
        )
        conn.execute(
            """INSERT INTO comments_v18(
                   id, account_id, channel_id, linked_chat_id, post_message_id,
                   comment_message_id, reply_to, author_id, text, date)
               SELECT id, ?, channel_id, linked_chat_id, post_message_id,
                      comment_message_id, reply_to, author_id, text, date
               FROM comments""",
            (account_id,),
        )
        conn.execute(
            """INSERT INTO comment_deliveries_v18(
                   id, account_id, channel_id, post_id, linked_chat_id,
                   comment_message_id, text, status, error, reserved_at, updated_at)
               SELECT id, ?, channel_id, post_id, linked_chat_id,
                      comment_message_id, text, status, error, reserved_at, updated_at
               FROM comment_deliveries""",
            (account_id,),
        )

        _execute_script(
            conn,
            """
            DROP TABLE messages;
            DROP TABLE comments;
            DROP TABLE comment_deliveries;
            DROP TABLE channels;
            ALTER TABLE channels_v18 RENAME TO channels;
            ALTER TABLE messages_v18 RENAME TO messages;
            ALTER TABLE comments_v18 RENAME TO comments;
            ALTER TABLE comment_deliveries_v18 RENAME TO comment_deliveries;

            CREATE INDEX idx_channels_comment_rotation
                ON channels(account_id, linked_chat_id, last_comment_check_at);
            CREATE INDEX idx_channels_comment_targets
                ON channels(account_id, comment_mode, linked_chat_id, last_comment_check_at);
            CREATE INDEX idx_messages_account_channel
                ON messages(account_id, channel_id, message_id);
            CREATE INDEX idx_comments_post
                ON comments(channel_id, post_message_id);
            CREATE INDEX idx_comments_account_post
                ON comments(account_id, channel_id, post_message_id);
            CREATE INDEX idx_delivery_lookup
                ON comment_deliveries(account_id, channel_id, post_id, status);
            CREATE INDEX idx_delivery_recovery
                ON comment_deliveries(status, reserved_at);

            CREATE TRIGGER fk_messages_channel_insert
               BEFORE INSERT ON messages
               WHEN NOT EXISTS(
                   SELECT 1 FROM channels
                   WHERE account_id=NEW.account_id AND channel_id=NEW.channel_id)
               BEGIN SELECT RAISE(ABORT, 'messages target references missing channel for account'); END;
            CREATE TRIGGER fk_messages_channel_update
               BEFORE UPDATE OF account_id, channel_id ON messages
               WHEN NOT EXISTS(
                   SELECT 1 FROM channels
                   WHERE account_id=NEW.account_id AND channel_id=NEW.channel_id)
               BEGIN SELECT RAISE(ABORT, 'messages target references missing channel for account'); END;
            CREATE TRIGGER fk_comments_channel_insert
               BEFORE INSERT ON comments
               WHEN NOT EXISTS(
                   SELECT 1 FROM channels
                   WHERE account_id=NEW.account_id AND channel_id=NEW.channel_id)
               BEGIN SELECT RAISE(ABORT, 'comments target references missing channel for account'); END;
            CREATE TRIGGER fk_comments_channel_update
               BEFORE UPDATE OF account_id, channel_id ON comments
               WHEN NOT EXISTS(
                   SELECT 1 FROM channels
                   WHERE account_id=NEW.account_id AND channel_id=NEW.channel_id)
               BEGIN SELECT RAISE(ABORT, 'comments target references missing channel for account'); END;
            CREATE TRIGGER cascade_channel_children
               AFTER DELETE ON channels
               BEGIN
                   DELETE FROM messages
                    WHERE account_id=OLD.account_id AND channel_id=OLD.channel_id;
                   DELETE FROM comments
                    WHERE account_id=OLD.account_id AND channel_id=OLD.channel_id;
               END;
            CREATE TRIGGER validate_comment_history_account_insert
               BEFORE INSERT ON comment_history
               WHEN NEW.campaign_id IS NOT NULL AND NOT EXISTS(
                   SELECT 1 FROM comment_campaigns c
                    WHERE c.id=NEW.campaign_id AND c.account_id=NEW.account_id)
               BEGIN SELECT RAISE(ABORT, 'comment history account does not match campaign'); END;
            CREATE TRIGGER validate_comment_history_account_update
               BEFORE UPDATE OF account_id, campaign_id ON comment_history
               WHEN NEW.campaign_id IS NOT NULL AND NOT EXISTS(
                   SELECT 1 FROM comment_campaigns c
                    WHERE c.id=NEW.campaign_id AND c.account_id=NEW.account_id)
               BEGIN SELECT RAISE(ABORT, 'comment history account does not match campaign'); END;
            """,
        )

        # Persist the owner on already queued tasks as well. Workers require an
        # exact task/campaign/session match before any Telegram call.
        for row in conn.execute(
            "SELECT id, type, payload FROM tasks "
            "WHERE type IN ('sync_channels','link_channels','auto_comment',"
            "'auto_comment_slot','direct_message','comment',"
            "'sync_saved_dialogs','join_saved_slot')"
        ).fetchall():
            try:
                payload = json.loads(str(row["payload"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            task_type = str(row["type"])
            if task_type not in {"auto_comment_slot", "join_saved_slot"}:
                payload["account_id"] = int(account_id or 0)
                conn.execute(
                    "UPDATE tasks SET payload=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), int(row["id"])),
                )
                continue
            slot_id = payload.get("slot_id")
            try:
                slot_id = int(slot_id)
            except (TypeError, ValueError, OverflowError):
                continue
            if task_type == "auto_comment_slot":
                owner = conn.execute(
                    """SELECT c.account_id FROM comment_schedule s
                       JOIN comment_campaigns c ON c.id=s.campaign_id
                       WHERE s.id=? AND s.task_id=?""",
                    (slot_id, int(row["id"])),
                ).fetchone()
            else:
                owner = conn.execute(
                    """SELECT c.account_id FROM join_schedule s
                       JOIN join_campaigns c ON c.id=s.campaign_id
                       WHERE s.id=? AND s.task_id=?""",
                    (slot_id, int(row["id"])),
                ).fetchone()
            if owner is None:
                continue
            payload["account_id"] = int(owner[0] or 0)
            conn.execute(
                "UPDATE tasks SET payload=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), int(row["id"])),
            )

        if account_id > 0:
            conn.execute(
                "UPDATE join_events SET account_id=? WHERE account_id IS NULL",
                (account_id,),
            )

        # A join request awaiting administrator approval is definitive, but is
        # neither membership nor an unknown result. Extend the persisted state
        # machine without weakening any existing terminal-state checks.
        conn.execute("DROP TRIGGER IF EXISTS validate_join_schedule_insert")
        conn.execute("DROP TRIGGER IF EXISTS validate_join_schedule_update")
        _execute_script(
            conn,
            """
            CREATE TRIGGER validate_join_schedule_insert
            BEFORE INSERT ON join_schedule
            WHEN NEW.status NOT IN (
                'pending','queued','running','joined','already_member',
                'join_requested','skipped','failed','uncertain','cancelled')
            BEGIN SELECT RAISE(ABORT, 'invalid join schedule status'); END;
            CREATE TRIGGER validate_join_schedule_update
            BEFORE UPDATE OF status ON join_schedule
            WHEN NEW.status NOT IN (
                'pending','queued','running','joined','already_member',
                'join_requested','skipped','failed','uncertain','cancelled')
            BEGIN SELECT RAISE(ABORT, 'invalid join schedule status'); END;
            """,
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comment_campaign_account_status "
            "ON comment_campaigns(account_id, status, ends_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comment_history_account_campaign "
            "ON comment_history(account_id, campaign_id, id)"
        )
        conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(18)")
        conn.execute("PRAGMA user_version = 18")
        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

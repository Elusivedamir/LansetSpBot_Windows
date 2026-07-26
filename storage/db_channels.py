from __future__ import annotations

import logging
from storage.sqlcipher_driver import dbapi as sqlite3

from storage.db_common import DatabaseError, resolve_account_id

log = logging.getLogger(__name__)


class ChannelRepositoryMixin:
    def upsert_channels_batch(self, rows, *, account_id=None):
        """Insert/update a bounded working-target batch in one transaction.

        A repeated dialog sync must not erase a previously resolved channel
        link or reset an already classified group back to ``pending``.
        """
        normalized = [row for row in rows if row.get("channel_id") is not None]
        owner_account_id = resolve_account_id(self, account_id)
        if not normalized:
            return 0
        try:
            with self.get_connection() as conn:
                conn.executemany(
                    """INSERT INTO channels(
                           account_id, channel_id, username, title, target_kind, comment_mode,
                           linked_chat_id, linked_chat_title, link_status,
                           access_hash, peer_type, created_at, last_sync_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                       ON CONFLICT(account_id, channel_id) DO UPDATE SET
                           username=excluded.username,
                           title=excluded.title,
                           target_kind=excluded.target_kind,
                           comment_mode=CASE
                               WHEN channels.local_banned_at IS NOT NULL
                                   THEN channels.comment_mode
                               WHEN excluded.comment_mode='linked_discussion'
                                   THEN 'linked_discussion'
                               WHEN channels.target_kind=excluded.target_kind
                                   THEN channels.comment_mode
                               ELSE excluded.comment_mode
                           END,
                           linked_chat_id=CASE
                               WHEN channels.local_banned_at IS NOT NULL THEN NULL
                               WHEN channels.target_kind=excluded.target_kind
                                   THEN COALESCE(channels.linked_chat_id, excluded.linked_chat_id)
                               ELSE excluded.linked_chat_id
                           END,
                           linked_chat_title=CASE
                               WHEN channels.local_banned_at IS NOT NULL THEN NULL
                               WHEN channels.target_kind=excluded.target_kind
                                   THEN COALESCE(channels.linked_chat_title, excluded.linked_chat_title)
                               ELSE excluded.linked_chat_title
                           END,
                           link_status=CASE
                               WHEN channels.local_banned_at IS NOT NULL
                                   THEN channels.link_status
                               WHEN excluded.comment_mode='linked_discussion'
                                   THEN excluded.link_status
                               WHEN channels.target_kind=excluded.target_kind
                                   THEN COALESCE(channels.link_status, excluded.link_status)
                               ELSE excluded.link_status
                           END,
                           access_hash=COALESCE(excluded.access_hash, channels.access_hash),
                           peer_type=COALESCE(excluded.peer_type, channels.peer_type),
                           last_sync_at=CURRENT_TIMESTAMP""",
                    [
                        (
                            owner_account_id,
                            row.get("channel_id"),
                            row.get("username"),
                            row.get("title"),
                            row.get("target_kind", "channel"),
                            row.get("comment_mode", "channel_post"),
                            row.get("linked_chat_id"),
                            row.get("linked_chat_title"),
                            row.get("link_status"),
                            row.get("access_hash"),
                            row.get("peer_type"),
                        )
                        for row in normalized
                    ],
                )
            return len(normalized)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to upsert channel batch: {exc}") from exc

    def insert_channel(self, data):
        """Backward-compatible single-row channel upsert."""
        return self.upsert_channels_batch([data], account_id=data.get("account_id"))

    def insert_message(self, data, *, account_id=None):
        """Insert message."""
        try:
            owner_account_id = resolve_account_id(
                self, account_id if account_id is not None else data.get("account_id")
            )
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO messages(account_id, channel_id, message_id, text, date, author_id)
                       VALUES(?, ?, ?, ?, ?, ?)""",
                    (
                        owner_account_id,
                        data.get("channel_id"),
                        data.get("message_id"),
                        data.get("text"),
                        data.get("date"),
                        data.get("author_id"),
                    ),
                )
            inserted = cursor.rowcount == 1
            if inserted:
                log.debug("Message %s inserted", data.get("message_id"))
            else:
                log.debug("Message %s already exists", data.get("message_id"))
            return inserted
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to insert message: {e}") from e

    def insert_comment(self, data, *, account_id=None):
        """Insert comment."""
        try:
            owner_account_id = resolve_account_id(
                self, account_id if account_id is not None else data.get("account_id")
            )
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO comments(account_id, channel_id, linked_chat_id, post_message_id,
                       comment_message_id, reply_to, author_id, text, date)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        owner_account_id,
                        data.get("channel_id"),
                        data.get("linked_chat_id"),
                        data.get("post_message_id"),
                        data.get("comment_message_id"),
                        data.get("reply_to"),
                        data.get("author_id"),
                        data.get("text"),
                        data.get("date"),
                    ),
                )
            inserted = cursor.rowcount == 1
            if inserted:
                log.debug("Comment %s inserted", data.get("comment_message_id"))
            else:
                log.debug("Comment %s already exists", data.get("comment_message_id"))
            return inserted
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to insert comment: {e}") from e

    def import_rows(self, kind, rows, *, batch_size=1000, account_id=None):
        """Import an iterable atomically without loading the whole file into RAM."""
        owner_account_id = resolve_account_id(self, account_id)
        statements = {
            "channels": (
                """INSERT INTO channels(account_id, channel_id, username, title, linked_chat_id, created_at)
                   VALUES(?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(account_id, channel_id) DO UPDATE SET
                       username=excluded.username,
                       title=excluded.title,
                       linked_chat_id=CASE
                           WHEN channels.local_banned_at IS NOT NULL THEN NULL
                           ELSE COALESCE(excluded.linked_chat_id, channels.linked_chat_id)
                       END""",
                lambda row: (
                    owner_account_id,
                    row.get("channel_id"),
                    row.get("username"),
                    row.get("title"),
                    row.get("linked_chat_id"),
                ),
            ),
            "messages": (
                """INSERT OR IGNORE INTO messages(account_id, channel_id, message_id, text, date, author_id)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                lambda row: (
                    owner_account_id,
                    row.get("channel_id"),
                    row.get("message_id"),
                    row.get("text"),
                    row.get("date"),
                    row.get("author_id"),
                ),
            ),
            "comments": (
                """INSERT OR IGNORE INTO comments(account_id, channel_id, linked_chat_id, post_message_id,
                   comment_message_id, reply_to, author_id, text, date)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                lambda row: (
                    owner_account_id,
                    row.get("channel_id"),
                    row.get("linked_chat_id"),
                    row.get("post_message_id"),
                    row.get("comment_message_id"),
                    row.get("reply_to"),
                    row.get("author_id"),
                    row.get("text"),
                    row.get("date"),
                ),
            ),
        }
        if kind not in statements:
            raise DatabaseError(f"Unsupported import kind: {kind}")
        sql, values = statements[kind]
        size = max(1, int(batch_size))
        batch = []
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                before_changes = conn.total_changes
                for row in rows:
                    batch.append(values(row))
                    if len(batch) < size:
                        continue
                    conn.executemany(sql, batch)
                    batch.clear()
                if batch:
                    conn.executemany(sql, batch)
                applied = conn.total_changes - before_changes
            return int(applied)
        except (DatabaseError, ValueError):
            raise
        except sqlite3.Error as exc:
            raise DatabaseError(f"Atomic import failed: {exc}") from exc
        except Exception as exc:
            raise DatabaseError(f"Atomic import preparation failed: {exc}") from exc

    def prune_channels_except(self, channel_ids, *, account_id=None):
        ids = sorted({int(value) for value in channel_ids if value is not None})
        owner_account_id = resolve_account_id(self, account_id)
        try:
            with self.get_connection() as conn:
                if not ids:
                    conn.execute(
                        "DELETE FROM channels WHERE account_id=?", (owner_account_id,)
                    )
                    return
                # A temporary table avoids SQLite bind-variable limits and giant
                # NOT IN statements when an account contains thousands of dialogs.
                conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS marlen_seen_channels("
                    "channel_id INTEGER PRIMARY KEY)"
                )
                conn.execute("DELETE FROM marlen_seen_channels")
                conn.executemany(
                    "INSERT OR IGNORE INTO marlen_seen_channels(channel_id) VALUES(?)",
                    ((value,) for value in ids),
                )
                conn.execute(
                    """DELETE FROM channels
                       WHERE account_id=? AND NOT EXISTS(
                           SELECT 1 FROM marlen_seen_channels seen
                           WHERE seen.channel_id=channels.channel_id
                       )""",
                    (owner_account_id,),
                )
                conn.execute("DELETE FROM marlen_seen_channels")
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to prune working channels: {exc}") from exc

    def delete_channels_transactional(self, channel_ids, *, account_id=None):
        """Remove selected targets and cancel their durable work in one transaction.

        Comment receipts/history are intentionally retained for duplicate safety.
        Working rows, parsed-message cache, saved-dialog links, schedules and queue
        tasks tied to the selected Telegram peers are removed or cancelled together.
        """
        ids = sorted({int(value) for value in channel_ids if int(value) != 0})
        owner_account_id = resolve_account_id(self, account_id)
        if not ids:
            return {
                "deleted_channel_ids": [],
                "cancelled_task_ids": [],
                "comment_slot_count": 0,
                "join_slot_count": 0,
                "saved_dialog_count": 0,
                "deleted_membership_count": 0,
                "deleted_join_schedule_count": 0,
            }
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS marlen_delete_channels("
                    "channel_id INTEGER PRIMARY KEY)"
                )
                conn.execute("DELETE FROM marlen_delete_channels")
                conn.executemany(
                    "INSERT OR IGNORE INTO marlen_delete_channels(channel_id) VALUES(?)",
                    ((value,) for value in ids),
                )

                comment_rows = conn.execute(
                    """SELECT s.id, s.task_id
                       FROM comment_schedule s
                       JOIN comment_campaigns c ON c.id=s.campaign_id
                       WHERE c.account_id=?
                         AND s.channel_id IN (
                             SELECT channel_id FROM marlen_delete_channels
                         )
                         AND s.status IN ('pending','queued','running')""",
                    (owner_account_id,),
                ).fetchall()
                saved_rows = conn.execute(
                    """SELECT id FROM saved_dialogs
                       WHERE peer_id IN (SELECT channel_id FROM marlen_delete_channels)"""
                ).fetchall()
                saved_ids = [int(row["id"]) for row in saved_rows]
                join_rows = []
                if saved_ids:
                    join_rows = conn.execute(
                        """SELECT s.id, s.task_id
                           FROM join_schedule s
                           JOIN join_campaigns c ON c.id=s.campaign_id
                           WHERE c.account_id=?
                             AND s.saved_dialog_id IN (
                               SELECT id FROM saved_dialogs
                               WHERE peer_id IN (SELECT channel_id FROM marlen_delete_channels)
                           )
                             AND s.status IN ('pending','queued','running')""",
                        (owner_account_id,),
                    ).fetchall()

                task_ids = sorted(
                    {
                        int(row["task_id"])
                        for row in (*comment_rows, *join_rows)
                        if row["task_id"] is not None
                    }
                )
                conn.execute(
                    """UPDATE comment_schedule
                       SET status='cancelled',
                           result='Канал удалён пользователем',
                           executed_at=CURRENT_TIMESTAMP
                       WHERE campaign_id IN (
                                 SELECT id FROM comment_campaigns WHERE account_id=?
                             )
                         AND channel_id IN (SELECT channel_id FROM marlen_delete_channels)
                         AND status IN ('pending','queued','running')""",
                    (owner_account_id,),
                )
                if task_ids:
                    placeholders = ",".join("?" for _ in task_ids)
                    conn.execute(
                        f"""UPDATE tasks
                            SET status='cancelled', progress=100, status_text=NULL,
                                error='Канал удалён пользователем', not_before=NULL,
                                updated_at=CURRENT_TIMESTAMP
                            WHERE id IN ({placeholders})
                              AND status IN ('pending','running','processing')""",
                        task_ids,
                    )

                deleted_messages = conn.execute(
                    """DELETE FROM messages
                       WHERE account_id=?
                         AND channel_id IN (SELECT channel_id FROM marlen_delete_channels)""",
                    (owner_account_id,),
                ).rowcount
                deleted_channels = conn.execute(
                    """DELETE FROM channels
                       WHERE account_id=?
                         AND channel_id IN (SELECT channel_id FROM marlen_delete_channels)""",
                    (owner_account_id,),
                ).rowcount
                deleted_join_slots = 0
                deleted_memberships = 0
                deleted_saved = 0
                if saved_ids:
                    # saved_dialogs is a global peer registry. Remove only the
                    # current account's dependent state, then garbage-collect a
                    # peer row only when no other account still owns membership
                    # or schedule history for it.
                    deleted_join_slots = conn.execute(
                        """DELETE FROM join_schedule
                           WHERE campaign_id IN (
                                     SELECT id FROM join_campaigns WHERE account_id=?
                                 )
                             AND saved_dialog_id IN (
                                 SELECT id FROM saved_dialogs
                                 WHERE peer_id IN (
                                     SELECT channel_id FROM marlen_delete_channels
                                 )
                             )""",
                        (owner_account_id,),
                    ).rowcount
                    deleted_memberships = conn.execute(
                        """DELETE FROM saved_dialog_memberships
                           WHERE account_id=?
                             AND saved_dialog_id IN (
                                 SELECT id FROM saved_dialogs
                                 WHERE peer_id IN (
                                     SELECT channel_id FROM marlen_delete_channels
                                 )
                             )""",
                        (owner_account_id,),
                    ).rowcount
                    deleted_saved = conn.execute(
                        """DELETE FROM saved_dialogs
                           WHERE peer_id IN (
                                     SELECT channel_id FROM marlen_delete_channels
                                 )
                             AND NOT EXISTS(
                                 SELECT 1 FROM saved_dialog_memberships m
                                 WHERE m.saved_dialog_id=saved_dialogs.id
                             )
                             AND NOT EXISTS(
                                 SELECT 1 FROM join_schedule s
                                 WHERE s.saved_dialog_id=saved_dialogs.id
                             )"""
                    ).rowcount
                conn.execute("DELETE FROM marlen_delete_channels")

                return {
                    "deleted_channel_ids": ids,
                    "deleted_channel_count": int(deleted_channels),
                    "saved_dialog_count": int(deleted_saved),
                    "deleted_membership_count": int(deleted_memberships),
                    "deleted_join_schedule_count": int(deleted_join_slots),
                    "comment_slot_count": len(comment_rows),
                    "join_slot_count": len(join_rows),
                    "cancelled_task_ids": task_ids,
                    "deleted_message_count": int(deleted_messages),
                }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to delete selected channels: {exc}") from exc

    def get_channels(self, *, account_id=None):
        """Get all channels."""
        try:
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """SELECT id, account_id, channel_id, username, title, target_kind,
                              comment_mode, linked_chat_id,
                              linked_chat_title, link_status, link_checked_at, last_sync_at,
                              last_comment_check_at, access_hash, peer_type,
                              negative_status, negative_until, local_ban_reason,
                              local_ban_peer_id, local_banned_at
                       FROM channels WHERE account_id=? ORDER BY lower(COALESCE(title, username, ''))""",
                    (owner_account_id,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to get channels: {e}") from e

    def get_channel_by_id(self, channel_id, *, account_id=None):
        """Get channel by channel_id."""
        try:
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """SELECT id, account_id, channel_id, username, title, target_kind,
                              comment_mode, linked_chat_id,
                              linked_chat_title, link_status, link_checked_at, last_sync_at,
                              last_comment_check_at, access_hash, peer_type,
                              negative_status, negative_until, local_ban_reason,
                              local_ban_peer_id, local_banned_at
                       FROM channels WHERE account_id=? AND channel_id = ?""",
                    (owner_account_id, channel_id),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to get channel: {e}") from e

    @staticmethod
    def _comment_cooldown_modifier(cooldown_hours) -> str | None:
        try:
            hours = float(cooldown_hours)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DatabaseError(
                f"Invalid commenting cooldown: {cooldown_hours!r}"
            ) from exc
        if hours <= 0:
            return None
        seconds = max(1, int(round(hours * 3600)))
        return f"-{seconds} seconds"

    def get_channels_for_commenting(self, limit, *, cooldown_hours=0, account_id=None):
        """Return a fair rotating batch of linked and currently eligible channels.

        ``cooldown_hours`` excludes channels already checked inside the requested
        rolling window. Production comment campaigns use 24 hours, which means a
        channel consumes at most one attempt during that window even when another
        campaign is started or the channel list is synchronized again.
        """
        try:
            value = int(limit)
            if value <= 0:
                return []
            modifier = self._comment_cooldown_modifier(cooldown_hours)
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                if modifier is None:
                    rows = conn.execute(
                        """SELECT id, channel_id, username, title, target_kind,
                                  comment_mode, linked_chat_id,
                                  linked_chat_title, link_status, last_sync_at,
                                  last_comment_check_at, access_hash, peer_type,
                                  negative_status, negative_until, local_ban_reason,
                                  local_ban_peer_id, local_banned_at
                           FROM channels
                           WHERE account_id=? AND comment_mode='channel_post' AND linked_chat_id IS NOT NULL
                             AND local_banned_at IS NULL
                             AND NOT EXISTS(
                                 SELECT 1 FROM local_ban_targets AS ban
                                 WHERE ban.account_id=channels.account_id
                                   AND ban.peer_id IN (channels.channel_id, channels.linked_chat_id)
                             )
                             AND (negative_until IS NULL OR negative_until <= CURRENT_TIMESTAMP)
                           ORDER BY
                               CASE WHEN last_comment_check_at IS NULL THEN 0 ELSE 1 END,
                               last_comment_check_at ASC,
                               lower(COALESCE(title, username, '')) ASC,
                               id ASC
                           LIMIT ?""",
                        (owner_account_id, value),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT id, channel_id, username, title, target_kind,
                                  comment_mode, linked_chat_id,
                                  linked_chat_title, link_status, last_sync_at,
                                  last_comment_check_at, access_hash, peer_type,
                                  negative_status, negative_until, local_ban_reason,
                                  local_ban_peer_id, local_banned_at
                           FROM channels
                           WHERE account_id=? AND comment_mode='channel_post' AND linked_chat_id IS NOT NULL
                             AND local_banned_at IS NULL
                             AND NOT EXISTS(
                                 SELECT 1 FROM local_ban_targets AS ban
                                 WHERE ban.account_id=channels.account_id
                                   AND ban.peer_id IN (channels.channel_id, channels.linked_chat_id)
                             )
                             AND (negative_until IS NULL OR negative_until <= CURRENT_TIMESTAMP)
                             AND (last_comment_check_at IS NULL
                                  OR last_comment_check_at < datetime('now', ?))
                           ORDER BY
                               CASE WHEN last_comment_check_at IS NULL THEN 0 ELSE 1 END,
                               last_comment_check_at ASC,
                               lower(COALESCE(title, username, '')) ASC,
                               id ASC
                           LIMIT ?""",
                        (owner_account_id, modifier, value),
                    ).fetchall()
                return [dict(row) for row in rows]
        except (TypeError, ValueError) as exc:
            raise DatabaseError(f"Invalid commenting batch limit: {limit!r}") from exc
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to get commenting channel batch: {exc}"
            ) from exc

    def count_channels_for_commenting(
        self, *, cooldown_hours=0, account_id=None
    ) -> int:
        """Count linked channels eligible for one attempt in the current window."""
        modifier = self._comment_cooldown_modifier(cooldown_hours)
        owner_account_id = resolve_account_id(self, account_id)
        try:
            with self.get_connection() as conn:
                if modifier is None:
                    row = conn.execute(
                        """SELECT COUNT(*) AS total FROM channels
                           WHERE account_id=? AND comment_mode='channel_post' AND linked_chat_id IS NOT NULL
                             AND local_banned_at IS NULL
                             AND NOT EXISTS(
                                 SELECT 1 FROM local_ban_targets AS ban
                                 WHERE ban.account_id=channels.account_id
                                   AND ban.peer_id IN (channels.channel_id, channels.linked_chat_id)
                             )
                             AND (negative_until IS NULL OR negative_until <= CURRENT_TIMESTAMP)""",
                        (owner_account_id,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """SELECT COUNT(*) AS total FROM channels
                           WHERE account_id=? AND comment_mode='channel_post' AND linked_chat_id IS NOT NULL
                             AND local_banned_at IS NULL
                             AND NOT EXISTS(
                                 SELECT 1 FROM local_ban_targets AS ban
                                 WHERE ban.account_id=channels.account_id
                                   AND ban.peer_id IN (channels.channel_id, channels.linked_chat_id)
                             )
                             AND (negative_until IS NULL OR negative_until <= CURRENT_TIMESTAMP)
                             AND (last_comment_check_at IS NULL
                                  OR last_comment_check_at < datetime('now', ?))""",
                        (owner_account_id, modifier),
                    ).fetchone()
                return int(row["total"] if row else 0)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to count commenting channels: {exc}") from exc

    def mark_channel_comment_checked(self, channel_id, *, account_id=None):
        """Move one channel to the back of the fair commenting rotation."""
        try:
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE channels
                       SET last_comment_check_at=CURRENT_TIMESTAMP
                       WHERE account_id=? AND channel_id=?""",
                    (owner_account_id, int(channel_id)),
                )
                return bool(cursor.rowcount == 1)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to update comment check time: {exc}") from exc

    def register_channel_peer(
        self, channel_id, *, access_hash=None, peer_type=None, account_id=None
    ) -> bool:
        """Persist a Telegram InputPeer reconstruction tuple."""
        try:
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE channels
                       SET access_hash=COALESCE(?, access_hash),
                           peer_type=COALESCE(?, peer_type)
                       WHERE account_id=? AND channel_id=?""",
                    (access_hash, peer_type, owner_account_id, int(channel_id)),
                )
                return bool(cursor.rowcount == 1)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to persist peer reference: {exc}") from exc

    def set_channel_negative_cache(
        self, channel_id, status, *, ttl_seconds, account_id=None
    ) -> bool:
        """Temporarily exclude a target after a stable negative Telegram result."""
        seconds = max(1, int(ttl_seconds))
        try:
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE channels
                       SET negative_status=?,
                           negative_until=datetime('now', ?)
                       WHERE account_id=? AND channel_id=?""",
                    (
                        str(status or "unknown"),
                        f"+{seconds} seconds",
                        owner_account_id,
                        int(channel_id),
                    ),
                )
                return bool(cursor.rowcount == 1)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to set negative cache: {exc}") from exc

    def clear_channel_negative_cache(self, channel_id, *, account_id=None) -> bool:
        try:
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE channels
                       SET negative_status=NULL, negative_until=NULL
                       WHERE account_id=? AND channel_id=?""",
                    (owner_account_id, int(channel_id)),
                )
                return bool(cursor.rowcount == 1)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to clear negative cache: {exc}") from exc

    @staticmethod
    def _upsert_local_ban_target(
        conn,
        *,
        account_id: int,
        peer_id: int,
        reason: str,
        source_channel_id: int | None = None,
        related_peer_id: int | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO local_ban_targets(
                   account_id, peer_id, reason, source_channel_id,
                   related_peer_id, banned_at)
               VALUES(?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(account_id, peer_id) DO UPDATE SET
                   reason=excluded.reason,
                   source_channel_id=COALESCE(
                       local_ban_targets.source_channel_id,
                       excluded.source_channel_id
                   ),
                   related_peer_id=COALESCE(
                       local_ban_targets.related_peer_id,
                       excluded.related_peer_id
                   )""",
            (
                int(account_id),
                int(peer_id),
                str(reason),
                int(source_channel_id) if source_channel_id is not None else None,
                int(related_peer_id) if related_peer_id is not None else None,
            ),
        )

    @staticmethod
    def _cancel_local_ban_work(
        conn,
        *,
        account_id: int,
        peer_ids: set[int],
        source_channel_ids: set[int],
        reason: str,
    ) -> set[int]:
        task_ids: set[int] = set()
        safe_peer_ids = sorted({int(value) for value in peer_ids if int(value) != 0})
        safe_source_ids = sorted(
            {int(value) for value in source_channel_ids if int(value) != 0}
        )
        message = str(reason or "Цель локально заблокирована")

        if safe_source_ids:
            placeholders = ",".join("?" for _ in safe_source_ids)
            rows = conn.execute(
                f"""SELECT s.task_id
                    FROM comment_schedule AS s
                    JOIN comment_campaigns AS c ON c.id=s.campaign_id
                    WHERE c.account_id=?
                      AND s.channel_id IN ({placeholders})
                      AND s.status IN ('pending','queued')
                      AND s.task_id IS NOT NULL""",
                (int(account_id), *safe_source_ids),
            ).fetchall()
            task_ids.update(int(row["task_id"]) for row in rows)
            conn.execute(
                f"""UPDATE comment_schedule
                    SET status='cancelled', result=?, executed_at=CURRENT_TIMESTAMP
                    WHERE campaign_id IN (
                            SELECT id FROM comment_campaigns WHERE account_id=?
                        )
                      AND channel_id IN ({placeholders})
                      AND status IN ('pending','queued')""",
                (message, int(account_id), *safe_source_ids),
            )

        affected_join_campaigns: set[int] = set()
        if safe_peer_ids:
            placeholders = ",".join("?" for _ in safe_peer_ids)
            rows = conn.execute(
                f"""SELECT s.task_id, s.campaign_id
                    FROM join_schedule AS s
                    JOIN join_campaigns AS c ON c.id=s.campaign_id
                    JOIN saved_dialogs AS d ON d.id=s.saved_dialog_id
                    WHERE c.account_id=?
                      AND d.peer_id IN ({placeholders})
                      AND s.status IN ('pending','queued')""",
                (int(account_id), *safe_peer_ids),
            ).fetchall()
            task_ids.update(
                int(row["task_id"]) for row in rows if row["task_id"] is not None
            )
            affected_join_campaigns.update(int(row["campaign_id"]) for row in rows)
            conn.execute(
                f"""UPDATE join_schedule
                    SET status='cancelled', task_id=NULL, result=?,
                        executed_at=CURRENT_TIMESTAMP
                    WHERE campaign_id IN (
                            SELECT id FROM join_campaigns WHERE account_id=?
                        )
                      AND saved_dialog_id IN (
                            SELECT id FROM saved_dialogs
                            WHERE peer_id IN ({placeholders})
                        )
                      AND status IN ('pending','queued')""",
                (message, int(account_id), *safe_peer_ids),
            )

        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(
                f"""UPDATE tasks
                    SET status='cancelled', progress=100, status_text=NULL,
                        error=?, not_before=NULL, updated_at=CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders}) AND status='pending'""",
                (message, *sorted(task_ids)),
            )

        for campaign_id in affected_join_campaigns:
            remaining = conn.execute(
                """SELECT COUNT(*) AS total FROM join_schedule
                   WHERE campaign_id=?
                     AND status IN ('pending','queued','running')""",
                (campaign_id,),
            ).fetchone()
            if int(remaining["total"] if remaining else 0) == 0:
                conn.execute(
                    """UPDATE join_campaigns
                       SET status='completed', pause_reason=?, network_retry_at=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('running','paused','network_wait')""",
                    ("Все незапущенные цели обработаны или заблокированы", campaign_id),
                )
        return task_ids

    def ban_channel_locally(
        self,
        channel_id,
        reason,
        *,
        related_peer_id=None,
        account_id=None,
    ) -> bool:
        """Permanently exclude one source channel after an ambiguous Join result.

        The update is account-scoped and atomic: any discovered discussion link is
        cleared, future link passes are marked complete, and comment selection can
        no longer return the row. No LeaveChannel request is attempted because the
        original Join result is unknown.
        """
        try:
            owner_account_id = resolve_account_id(self, account_id)
            safe_reason = str(reason or "Результат вступления неизвестен").strip()
            status = f"Заблокирован · {safe_reason.lower()}"
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    """SELECT linked_chat_id FROM channels
                       WHERE account_id=? AND channel_id=?""",
                    (owner_account_id, int(channel_id)),
                ).fetchone()
                if existing is None:
                    return False
                effective_related_peer_id = (
                    int(related_peer_id)
                    if related_peer_id is not None
                    else (
                        int(existing["linked_chat_id"])
                        if existing["linked_chat_id"] is not None
                        else None
                    )
                )
                cursor = conn.execute(
                    """UPDATE channels
                       SET linked_chat_id=NULL,
                           linked_chat_title=NULL,
                           link_status=?,
                           link_checked_at=COALESCE(link_checked_at, CURRENT_TIMESTAMP),
                           negative_status=NULL,
                           negative_until=NULL,
                           local_ban_reason=?,
                           local_ban_peer_id=?,
                           local_banned_at=COALESCE(local_banned_at, CURRENT_TIMESTAMP),
                           last_sync_at=CURRENT_TIMESTAMP
                       WHERE account_id=? AND channel_id=?""",
                    (
                        status,
                        safe_reason,
                        effective_related_peer_id,
                        owner_account_id,
                        int(channel_id),
                    ),
                )
                if cursor.rowcount != 1:
                    return False
                self._upsert_local_ban_target(
                    conn,
                    account_id=owner_account_id,
                    peer_id=int(channel_id),
                    reason=safe_reason,
                    source_channel_id=int(channel_id),
                    related_peer_id=effective_related_peer_id,
                )
                peer_ids = {int(channel_id)}
                if effective_related_peer_id is not None:
                    peer_ids.add(int(effective_related_peer_id))
                    self._upsert_local_ban_target(
                        conn,
                        account_id=owner_account_id,
                        peer_id=effective_related_peer_id,
                        reason=safe_reason,
                        source_channel_id=int(channel_id),
                        related_peer_id=effective_related_peer_id,
                    )
                self._cancel_local_ban_work(
                    conn,
                    account_id=owner_account_id,
                    peer_ids=peer_ids,
                    source_channel_ids={int(channel_id)},
                    reason="Канал локально заблокирован после неизвестного Join",
                )
                return True
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to locally ban channel: {exc}") from exc

    def ban_peer_locally(
        self,
        peer_id,
        reason,
        *,
        account_id=None,
        source_channel_id=None,
    ) -> bool:
        """Permanently block a stable Telegram peer and its cached routes."""
        try:
            owner_account_id = resolve_account_id(self, account_id)
            numeric_peer_id = int(peer_id)
            if numeric_peer_id == 0:
                raise DatabaseError("Local ban requires a non-zero peer id")
            safe_reason = str(reason or "Результат вступления неизвестен").strip()
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                related_rows = conn.execute(
                    """SELECT channel_id FROM channels
                       WHERE account_id=?
                         AND (channel_id=? OR linked_chat_id=? OR local_ban_peer_id=?)""",
                    (
                        owner_account_id,
                        numeric_peer_id,
                        numeric_peer_id,
                        numeric_peer_id,
                    ),
                ).fetchall()
                source_ids = {int(row["channel_id"]) for row in related_rows}
                if source_channel_id is not None:
                    source_ids.add(int(source_channel_id))
                if source_ids:
                    placeholders = ",".join("?" for _ in source_ids)
                    conn.execute(
                        f"""UPDATE channels
                            SET linked_chat_id=NULL, linked_chat_title=NULL,
                                link_status='Заблокирован · результат вступления неизвестен',
                                link_checked_at=COALESCE(link_checked_at, CURRENT_TIMESTAMP),
                                negative_status=NULL, negative_until=NULL,
                                local_ban_reason=?, local_ban_peer_id=?,
                                local_banned_at=COALESCE(local_banned_at, CURRENT_TIMESTAMP),
                                last_sync_at=CURRENT_TIMESTAMP
                            WHERE account_id=? AND channel_id IN ({placeholders})""",
                        (
                            safe_reason,
                            numeric_peer_id,
                            owner_account_id,
                            *sorted(source_ids),
                        ),
                    )
                self._upsert_local_ban_target(
                    conn,
                    account_id=owner_account_id,
                    peer_id=numeric_peer_id,
                    reason=safe_reason,
                    source_channel_id=(
                        int(source_channel_id)
                        if source_channel_id is not None
                        else None
                    ),
                    related_peer_id=numeric_peer_id,
                )
                for source_id in source_ids:
                    self._upsert_local_ban_target(
                        conn,
                        account_id=owner_account_id,
                        peer_id=source_id,
                        reason=safe_reason,
                        source_channel_id=source_id,
                        related_peer_id=numeric_peer_id,
                    )
                self._cancel_local_ban_work(
                    conn,
                    account_id=owner_account_id,
                    peer_ids={numeric_peer_id, *source_ids},
                    source_channel_ids=source_ids,
                    reason="Цель локально заблокирована после неизвестного Join",
                )
                return True
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to locally ban peer: {exc}") from exc

    def is_channel_locally_banned(self, channel_id, *, account_id=None) -> bool:
        """Return whether a source or related Telegram peer is permanently blocked."""
        try:
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT 1 FROM local_ban_targets
                       WHERE account_id=? AND peer_id=?
                       UNION ALL
                       SELECT 1 FROM channels
                       WHERE account_id=? AND local_banned_at IS NOT NULL
                         AND (channel_id=? OR local_ban_peer_id=?)
                       LIMIT 1""",
                    (
                        owner_account_id,
                        int(channel_id),
                        owner_account_id,
                        int(channel_id),
                        int(channel_id),
                    ),
                ).fetchone()
                return row is not None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read local channel ban: {exc}") from exc

    def mark_link_checked(self, channel_id, *, account_id=None) -> bool:
        """Permanently exclude one target from future link-discovery passes."""
        try:
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE channels
                       SET link_checked_at=COALESCE(link_checked_at, CURRENT_TIMESTAMP)
                       WHERE account_id=? AND channel_id=?""",
                    (owner_account_id, int(channel_id)),
                )
                return bool(cursor.rowcount == 1)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to mark link target checked: {exc}") from exc

    def count_unchecked_link_targets(self, *, account_id=None) -> int:
        """Return targets that have never completed link inspection."""
        try:
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT COUNT(*) AS total FROM channels
                       WHERE account_id=? AND link_checked_at IS NULL
                         AND local_banned_at IS NULL
                         AND NOT EXISTS(
                             SELECT 1 FROM local_ban_targets AS ban
                             WHERE ban.account_id=channels.account_id
                               AND ban.peer_id=channels.channel_id
                         )""",
                    (owner_account_id,),
                ).fetchone()
                return int(row["total"] if row else 0)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to count unchecked link targets: {exc}"
            ) from exc

    def update_channel_link(
        self,
        channel_id,
        linked_chat_id=None,
        linked_chat_title=None,
        status=None,
        *,
        account_id=None,
    ):
        """Persist linked discussion information for one channel."""
        try:
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE channels
                       SET linked_chat_id=?, linked_chat_title=?, link_status=?,
                           last_sync_at=CURRENT_TIMESTAMP
                       WHERE account_id=? AND channel_id=?
                         AND local_banned_at IS NULL
                         AND NOT EXISTS(
                             SELECT 1 FROM local_ban_targets AS ban
                             WHERE ban.account_id=channels.account_id
                               AND ban.peer_id IN (channels.channel_id, ?)
                         )""",
                    (
                        linked_chat_id,
                        linked_chat_title,
                        status,
                        owner_account_id,
                        channel_id,
                        linked_chat_id,
                    ),
                )
                return bool(cursor.rowcount == 1)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to update channel link: {exc}") from exc

    def update_group_link_classification(
        self, group_id, *, is_linked: bool | None, status: str, account_id=None
    ) -> bool:
        """Persist one explicit group-side link inspection result.

        ``None`` records an inspection error without promoting an unverified
        group to a direct campaign target.
        """
        try:
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                if is_linked is None:
                    cursor = conn.execute(
                        """UPDATE channels
                           SET comment_mode=CASE
                                   WHEN comment_mode='linked_discussion'
                                       THEN 'linked_discussion'
                                   ELSE 'pending'
                               END,
                               linked_chat_id=channel_id,
                               linked_chat_title=COALESCE(linked_chat_title, title),
                               link_status=?,
                               last_sync_at=CURRENT_TIMESTAMP
                           WHERE account_id=? AND channel_id=? AND target_kind='group'
                             AND local_banned_at IS NULL""",
                        (str(status or ""), owner_account_id, int(group_id)),
                    )
                else:
                    mode = "linked_discussion" if is_linked else "pending"
                    cursor = conn.execute(
                        """UPDATE channels
                           SET comment_mode=?,
                               linked_chat_id=channel_id,
                               linked_chat_title=COALESCE(linked_chat_title, title),
                               link_status=?,
                               last_sync_at=CURRENT_TIMESTAMP
                           WHERE account_id=? AND channel_id=? AND target_kind='group'
                             AND local_banned_at IS NULL""",
                        (mode, str(status or ""), owner_account_id, int(group_id)),
                    )
                return bool(cursor.rowcount == 1)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to update group link classification: {exc}"
            ) from exc

    def refresh_group_comment_modes(self, *, account_id=None) -> dict[str, int]:
        """Keep only linked discussions eligible for comment campaigns.

        Ordinary groups remain visible in the channel list but never become
        plain-message targets. This makes the legacy ``direct_group`` delivery
        path unreachable from synchronization and scheduling.
        """
        try:
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                linked = conn.execute(
                    """UPDATE channels AS group_target
                       SET comment_mode='linked_discussion',
                           linked_chat_id=group_target.channel_id,
                           linked_chat_title=COALESCE(group_target.linked_chat_title, group_target.title),
                           link_status='Связанное обсуждение · только комментарии к постам',
                           last_sync_at=CURRENT_TIMESTAMP
                       WHERE group_target.account_id=? AND group_target.target_kind='group'
                         AND group_target.local_banned_at IS NULL
                         AND (group_target.comment_mode='linked_discussion'
                              OR EXISTS(
                                  SELECT 1 FROM channels AS channel_target
                                  WHERE channel_target.account_id=group_target.account_id
                                    AND channel_target.target_kind='channel'
                                    AND channel_target.linked_chat_id=group_target.channel_id
                              ))""",
                    (owner_account_id,),
                ).rowcount
                conn.execute(
                    """UPDATE channels AS group_target
                       SET comment_mode='pending',
                           linked_chat_id=NULL,
                           linked_chat_title=NULL,
                           link_status='Обычная группа · прямая отправка отключена',
                           last_sync_at=CURRENT_TIMESTAMP
                       WHERE group_target.account_id=?
                         AND group_target.target_kind='group'
                         AND group_target.local_banned_at IS NULL
                         AND group_target.comment_mode!='linked_discussion'
                         AND NOT EXISTS(
                             SELECT 1 FROM channels AS channel_target
                             WHERE channel_target.account_id=group_target.account_id
                               AND channel_target.target_kind='channel'
                               AND channel_target.linked_chat_id=group_target.channel_id
                         )""",
                    (owner_account_id,),
                )
            return {
                "linked_discussion": max(0, int(linked or 0)),
                "direct_group": 0,
            }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to classify group targets: {exc}") from exc

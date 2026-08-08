from __future__ import annotations
import logging
from storage.sqlcipher_driver import dbapi as sqlite3
from storage.db_common import DatabaseError, resolve_account_id
log = logging.getLogger(__name__)

class ChannelQueryRepositoryMixin:
    _CONFIRMED_COMMENT_LINK_STATUSES = (
        "Связано · обсуждение уже в диалогах",
        "Связано · вступление выполнено",
        "Связано · участие уже было",
        "Связано · участие подтверждено",
    )

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

    def is_comment_link_membership_confirmed(
        self, channel_id, linked_chat_id, *, account_id=None
    ) -> bool:
        """Return whether link preparation durably confirmed discussion membership."""
        try:
            owner_account_id = resolve_account_id(self, account_id)
            expected_linked_id = int(linked_chat_id)
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT linked_chat_id, link_status
                       FROM channels
                       WHERE account_id=? AND channel_id=?
                         AND comment_mode='channel_post'""",
                    (owner_account_id, int(channel_id)),
                ).fetchone()
            if row is None or row["linked_chat_id"] is None:
                return False
            link_status = str(row["link_status"] or "").strip()
            return (
                int(row["linked_chat_id"]) == expected_linked_id
                and (
                    not link_status
                    or link_status in self._CONFIRMED_COMMENT_LINK_STATUSES
                )
            )
        except (TypeError, ValueError, OverflowError):
            return False
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to validate prepared comment membership: {exc}"
            ) from exc

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
            confirmed_statuses = self._CONFIRMED_COMMENT_LINK_STATUSES
            status_marks = ",".join("?" for _ in confirmed_statuses)
            with self.get_connection() as conn:
                if modifier is None:
                    rows = conn.execute(
                        f"""SELECT id, channel_id, username, title, target_kind,
                                  comment_mode, linked_chat_id,
                                  linked_chat_title, link_status, last_sync_at,
                                  last_comment_check_at, access_hash, peer_type,
                                  negative_status, negative_until, local_ban_reason,
                                  local_ban_peer_id, local_banned_at
                           FROM channels
                           WHERE account_id=? AND (
                                  (comment_mode='channel_post'
                                   AND linked_chat_id IS NOT NULL
                                   AND (
                                       link_status IS NULL
                                       OR TRIM(link_status)=''
                                       OR link_status IN ({status_marks})
                                   ))
                                  OR (comment_mode='direct_group' AND target_kind='group')
                              )
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
                        (owner_account_id, *confirmed_statuses, value),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"""SELECT id, channel_id, username, title, target_kind,
                                  comment_mode, linked_chat_id,
                                  linked_chat_title, link_status, last_sync_at,
                                  last_comment_check_at, access_hash, peer_type,
                                  negative_status, negative_until, local_ban_reason,
                                  local_ban_peer_id, local_banned_at
                           FROM channels
                           WHERE account_id=? AND (
                                  (comment_mode='channel_post'
                                   AND linked_chat_id IS NOT NULL
                                   AND (
                                       link_status IS NULL
                                       OR TRIM(link_status)=''
                                       OR link_status IN ({status_marks})
                                   ))
                                  OR (comment_mode='direct_group' AND target_kind='group')
                              )
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
                        (owner_account_id, *confirmed_statuses, modifier, value),
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
        """Count post-comment and standalone-group targets in the current window."""
        modifier = self._comment_cooldown_modifier(cooldown_hours)
        owner_account_id = resolve_account_id(self, account_id)
        confirmed_statuses = self._CONFIRMED_COMMENT_LINK_STATUSES
        status_marks = ",".join("?" for _ in confirmed_statuses)
        try:
            with self.get_connection() as conn:
                if modifier is None:
                    row = conn.execute(
                        f"""SELECT COUNT(*) AS total FROM channels
                           WHERE account_id=? AND (
                                  (comment_mode='channel_post'
                                   AND linked_chat_id IS NOT NULL
                                   AND (
                                       link_status IS NULL
                                       OR TRIM(link_status)=''
                                       OR link_status IN ({status_marks})
                                   ))
                                  OR (comment_mode='direct_group' AND target_kind='group')
                              )
                             AND local_banned_at IS NULL
                             AND NOT EXISTS(
                                 SELECT 1 FROM local_ban_targets AS ban
                                 WHERE ban.account_id=channels.account_id
                                   AND ban.peer_id IN (channels.channel_id, channels.linked_chat_id)
                             )
                             AND (negative_until IS NULL OR negative_until <= CURRENT_TIMESTAMP)""",
                        (owner_account_id, *confirmed_statuses),
                    ).fetchone()
                else:
                    row = conn.execute(
                        f"""SELECT COUNT(*) AS total FROM channels
                           WHERE account_id=? AND (
                                  (comment_mode='channel_post'
                                   AND linked_chat_id IS NOT NULL
                                   AND (
                                       link_status IS NULL
                                       OR TRIM(link_status)=''
                                       OR link_status IN ({status_marks})
                                   ))
                                  OR (comment_mode='direct_group' AND target_kind='group')
                              )
                             AND local_banned_at IS NULL
                             AND NOT EXISTS(
                                 SELECT 1 FROM local_ban_targets AS ban
                                 WHERE ban.account_id=channels.account_id
                                   AND ban.peer_id IN (channels.channel_id, channels.linked_chat_id)
                             )
                             AND (negative_until IS NULL OR negative_until <= CURRENT_TIMESTAMP)
                             AND (last_comment_check_at IS NULL
                                  OR last_comment_check_at < datetime('now', ?))""",
                        (owner_account_id, *confirmed_statuses, modifier),
                    ).fetchone()
                return int(row["total"] if row else 0)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to count commenting channels: {exc}") from exc
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

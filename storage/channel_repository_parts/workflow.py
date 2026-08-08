from __future__ import annotations
import logging
from storage.sqlcipher_driver import dbapi as sqlite3
from storage.db_common import DatabaseError, resolve_account_id
log = logging.getLogger(__name__)

class ChannelWorkflowRepositoryMixin:
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
    def refresh_group_comment_modes(self, *, account_id=None) -> dict[str, int]:
        """Classify linked discussions and standalone ordinary-group targets.

        Linked discussions remain post-comment routes. Ordinary writable groups
        become ``direct_group`` targets and receive a standalone message without
        a post id or reply target.
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
                direct = conn.execute(
                    """UPDATE channels AS group_target
                       SET comment_mode='direct_group',
                           linked_chat_id=group_target.channel_id,
                           linked_chat_title=COALESCE(
                               group_target.linked_chat_title,
                               group_target.title
                           ),
                           link_status='Обычная группа · сообщение без привязки к посту',
                           last_sync_at=CURRENT_TIMESTAMP
                       WHERE group_target.account_id=?
                         AND group_target.target_kind='group'
                         AND group_target.local_banned_at IS NULL
                         AND group_target.comment_mode='direct_group'
                         AND NOT EXISTS(
                             SELECT 1 FROM channels AS channel_target
                             WHERE channel_target.account_id=group_target.account_id
                               AND channel_target.target_kind='channel'
                               AND channel_target.linked_chat_id=group_target.channel_id
                         )""",
                    (owner_account_id,),
                ).rowcount
            return {
                "linked_discussion": max(0, int(linked or 0)),
                "direct_group": max(0, int(direct or 0)),
            }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to classify group targets: {exc}") from exc

from __future__ import annotations

from typing import TYPE_CHECKING

import logging

from storage.db_common import DatabaseError, resolve_account_id

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class CommentHistoryMixin(_MixinHost):
    """Persistent comment history operations."""

    def has_commented(
        self,
        channel_id,
        post_id,
        *,
        account_id=None,
        linked_chat_id=None,
        campaign_id=None,
        action_type="comment",
    ):
        """Return whether this account already touched the immutable source post.

        ``linked_chat_id`` and ``action_type`` are route metadata. Telegram may
        replace a channel's linked discussion, but that must never make the same
        source post eligible for a second campaign delivery.
        """
        del linked_chat_id, action_type
        try:
            owner_account_id = resolve_account_id(self, account_id)
            del campaign_id
            source_channel_id = int(channel_id)
            source_post_id = int(post_id)
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT 1 FROM comments
                       WHERE account_id=? AND channel_id=? AND post_message_id=?
                         AND comment_message_id IS NOT NULL
                       UNION ALL
                       SELECT 1 FROM comment_deliveries
                       WHERE account_id=? AND channel_id=? AND post_id=?
                         AND status IN ('sending','sent','uncertain')
                       LIMIT 1""",
                    (
                        owner_account_id,
                        source_channel_id,
                        source_post_id,
                        owner_account_id,
                        source_channel_id,
                        source_post_id,
                    ),
                ).fetchone()
                return row is not None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to check comment history: {exc}") from exc

    def add_comment_history(
        self,
        task_id,
        channel_id,
        post_id,
        text,
        status,
        *,
        campaign_id=None,
        slot_id=None,
        account_id=None,
    ):
        try:
            owner_account_id = resolve_account_id(self, account_id)
            with self.get_connection() as conn:
                conn.execute(
                    """INSERT INTO comment_history(account_id, task_id, campaign_id, slot_id,
                           channel_id, post_id, comment_text, sent_at, status)
                       VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)""",
                    (
                        owner_account_id,
                        task_id,
                        campaign_id,
                        slot_id,
                        channel_id,
                        post_id,
                        text,
                        status,
                    ),
                )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to write comment history: {exc}") from exc

    def get_comment_history(
        self, task_id=None, limit=100, campaign_id=None, *, account_id=None
    ):
        try:
            owner_account_id = resolve_account_id(self, account_id)
            history_limit = max(0, int(limit))
            if history_limit <= 0:
                return []
            with self.get_connection() as conn:
                if campaign_id is not None:
                    campaign_key = int(campaign_id)
                    rows = conn.execute(
                        """SELECT id, account_id, task_id, campaign_id, slot_id, channel_id, post_id,
                                  comment_text, sent_at, status
                           FROM comment_history
                           WHERE account_id=? AND campaign_id=? ORDER BY id ASC LIMIT ?""",
                        (owner_account_id, campaign_key, history_limit),
                    ).fetchall()
                    history = [dict(row) for row in rows]
                    seen_slots = {
                        int(item["slot_id"])
                        for item in history
                        if item.get("slot_id") is not None
                    }
                    if len(history) < history_limit:
                        # comment_schedule is the authoritative durable slot
                        # ledger. Reconstruct only rows that are missing from the
                        # presentation history; never duplicate an existing
                        # comment_history record.
                        finalized = conn.execute(
                            """SELECT s.id AS slot_id, s.task_id, s.channel_id, s.post_id,
                                      s.selected_text AS comment_text,
                                      s.executed_at AS sent_at, s.result,
                                      s.status AS slot_status
                               FROM comment_schedule s
                               JOIN comment_campaigns c ON c.id=s.campaign_id
                               WHERE c.account_id=? AND s.campaign_id=?
                                 AND s.executed_at IS NOT NULL
                                 AND s.status IN (
                                     'sent','skipped','failed','uncertain',
                                     'missed','cancelled'
                                 )
                               ORDER BY s.slot_index ASC
                               LIMIT ?""",
                            (owner_account_id, campaign_key, history_limit),
                        ).fetchall()
                        for row in finalized:
                            slot_id = int(row["slot_id"])
                            if slot_id in seen_slots:
                                continue
                            history.append(
                                {
                                    "id": None,
                                    "account_id": owner_account_id,
                                    "task_id": row["task_id"],
                                    "campaign_id": campaign_key,
                                    "slot_id": slot_id,
                                    "channel_id": row["channel_id"],
                                    "post_id": row["post_id"],
                                    "comment_text": row["comment_text"],
                                    "sent_at": row["sent_at"],
                                    "status": str(
                                        row["result"] or row["slot_status"] or ""
                                    ),
                                }
                            )
                            seen_slots.add(slot_id)
                            if len(history) >= history_limit:
                                break
                    history.sort(
                        key=lambda item: (
                            int(item.get("slot_id") or 9_223_372_036_854_775_807),
                            int(item.get("id") or 9_223_372_036_854_775_807),
                        )
                    )
                    return history[:history_limit]
                elif task_id is None:
                    rows = conn.execute(
                        """SELECT id, account_id, task_id, campaign_id, slot_id, channel_id, post_id,
                                  comment_text, sent_at, status
                           FROM comment_history
                           WHERE account_id=? ORDER BY id DESC LIMIT ?""",
                        (owner_account_id, history_limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT id, account_id, task_id, campaign_id, slot_id, channel_id, post_id,
                                  comment_text, sent_at, status
                           FROM comment_history
                           WHERE account_id=? AND task_id=? ORDER BY id ASC LIMIT ?""",
                        (owner_account_id, int(task_id), history_limit),
                    ).fetchall()
                return [dict(row) for row in rows]
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read comment history: {exc}") from exc

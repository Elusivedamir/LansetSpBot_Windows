from __future__ import annotations

from typing import TYPE_CHECKING

import logging

from core.redaction import sanitize_text

from storage.db_common import DatabaseError, resolve_account_id

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class CommentReconciliationMixin(_MixinHost):
    """Crash recovery and durable comment delivery ledger."""

    def reconcile_comment_schedule(self):
        """Resolve slots whose worker task ended without finalizing the slot."""
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                affected_campaign_ids = set()

                orphan_rows = conn.execute(
                    """SELECT s.id, s.campaign_id, c.status AS campaign_status
                       FROM comment_schedule s
                       JOIN comment_campaigns c ON c.id=s.campaign_id
                       LEFT JOIN tasks t ON t.id=s.task_id
                       WHERE s.status IN ('queued','running')
                         AND (s.task_id IS NULL OR t.id IS NULL)"""
                ).fetchall()
                for row in orphan_rows:
                    if row["campaign_status"] in {
                        "running",
                        "paused",
                        "network_wait",
                        "cycle_wait",
                    }:
                        conn.execute(
                            """UPDATE comment_schedule
                               SET status='pending', task_id=NULL,
                                   result='Восстановлено после потери задачи',
                                   executed_at=NULL
                               WHERE id=? AND status IN ('queued','running')""",
                            (int(row["id"]),),
                        )
                    else:
                        conn.execute(
                            """UPDATE comment_schedule
                               SET status='cancelled', task_id=NULL,
                                   result='Кампания уже завершена',
                                   executed_at=CURRENT_TIMESTAMP
                               WHERE id=? AND status IN ('queued','running')""",
                            (int(row["id"]),),
                        )
                    affected_campaign_ids.add(int(row["campaign_id"]))

                rows = conn.execute(
                    """SELECT s.id, s.campaign_id, c.account_id, s.task_id, s.channel_id, s.post_id,
                              s.selected_text AS slot_selected_text,
                              t.status, t.error,
                              d.status AS direct_delivery_status,
                              d.chat_id AS direct_chat_id, d.text AS direct_text,
                              cd.status AS comment_delivery_status,
                              cd.text AS comment_text
                       FROM comment_schedule s
                       JOIN comment_campaigns c ON c.id=s.campaign_id
                       JOIN tasks t ON t.id=s.task_id
                       LEFT JOIN direct_message_deliveries d ON d.task_id=s.task_id
                       LEFT JOIN comment_deliveries cd
                         ON cd.account_id=c.account_id
                        AND cd.campaign_id=s.campaign_id
                        AND cd.action_type='campaign_comment'
                        AND cd.channel_id=s.channel_id AND cd.post_id=s.post_id
                       WHERE s.status IN ('queued','running')
                         AND t.status IN ('failed','cancelled','completed')"""
                ).fetchall()
                for row in rows:
                    if row["status"] == "cancelled":
                        message = str(row["error"] or "Задача отменена пользователем")
                        # Cancellation is a campaign pause, not a consumed target.
                        # Detach the cancelled queue task so Resume can lay this
                        # slot out again with the selected cadence.
                        conn.execute(
                            """UPDATE comment_schedule
                               SET status='pending', task_id=NULL, result=?,
                                   executed_at=NULL
                               WHERE id=? AND status IN ('queued','running')""",
                            (message, int(row["id"])),
                        )
                        conn.execute(
                            """UPDATE comment_campaigns
                               SET status='paused', pause_reason=?,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id=? AND status IN ('running','network_wait')""",
                            (
                                "Задача кампании отменена; продолжите или остановите кампанию вручную",
                                int(row["campaign_id"]),
                            ),
                        )
                        affected_campaign_ids.add(int(row["campaign_id"]))
                        continue

                    direct_sent = str(row["direct_delivery_status"] or "") == "sent"
                    comment_sent = str(row["comment_delivery_status"] or "") == "sent"
                    if direct_sent or comment_sent:
                        raw_channel_id = (
                            row["direct_chat_id"] if direct_sent else row["channel_id"]
                        )
                        channel_id = int(raw_channel_id)
                        post_id = None if direct_sent else int(row["post_id"])
                        selected_text = (
                            row["direct_text"] if direct_sent else row["comment_text"]
                        ) or row["slot_selected_text"]
                        message = (
                            "Сообщение отправлено в группу; локальное состояние восстановлено"
                            if direct_sent
                            else "Комментарий отправлен; локальное состояние восстановлено"
                        )
                        conn.execute(
                            """INSERT INTO comment_history(
                                   account_id, task_id, campaign_id, slot_id, channel_id, post_id,
                                   comment_text, sent_at, status)
                               SELECT ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?
                               WHERE NOT EXISTS(
                                   SELECT 1 FROM comment_history
                                    WHERE account_id=? AND slot_id=?
                               )""",
                            (
                                int(row["account_id"]),
                                int(row["task_id"]),
                                int(row["campaign_id"]),
                                int(row["id"]),
                                channel_id,
                                post_id,
                                selected_text,
                                message,
                                int(row["account_id"]),
                                int(row["id"]),
                            ),
                        )
                        conn.execute(
                            """UPDATE channels SET last_comment_check_at=CURRENT_TIMESTAMP
                               WHERE account_id=? AND channel_id=?""",
                            (int(row["account_id"]), channel_id),
                        )
                        conn.execute(
                            """UPDATE comment_schedule
                               SET status='sent', channel_id=?, post_id=?, result=?,
                                   executed_at=CURRENT_TIMESTAMP
                               WHERE id=? AND status IN ('queued','running')""",
                            (channel_id, post_id, message, int(row["id"])),
                        )
                        conn.execute(
                            """UPDATE comment_campaigns
                               SET attempted_count=attempted_count+1,
                                   sent_count=sent_count+1,
                                   last_comment_text=COALESCE(?, last_comment_text),
                                   network_failure_count=0, network_retry_at=NULL,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (selected_text, int(row["campaign_id"])),
                        )
                        conn.execute(
                            """UPDATE tasks
                               SET status='completed', progress=100, status_text=NULL,
                                   error=NULL, not_before=NULL, updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (int(row["task_id"]),),
                        )
                        affected_campaign_ids.add(int(row["campaign_id"]))
                        continue

                    if row["status"] == "completed":
                        slot_status = "uncertain"
                        message = "Задача завершилась без результата слота; требуется проверка"
                    else:
                        slot_status = (
                            "uncertain"
                            if "uncertain" in str(row["error"] or "")
                            else "failed"
                        )
                        message = str(row["error"] or "Задача остановлена")
                    conn.execute(
                        """UPDATE comment_schedule SET status=?, result=?, executed_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status IN ('queued','running')""",
                        (slot_status, message, int(row["id"])),
                    )
                    conn.execute(
                        """UPDATE comment_campaigns SET attempted_count=attempted_count+1,
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (int(row["campaign_id"]),),
                    )
                    if slot_status == "uncertain":
                        target_channel_id = (
                            row["direct_chat_id"]
                            if row["direct_chat_id"] is not None
                            else row["channel_id"]
                        )
                        if target_channel_id is not None:
                            conn.execute(
                                """UPDATE channels
                                   SET last_comment_check_at=CURRENT_TIMESTAMP
                                   WHERE account_id=? AND channel_id=?""",
                                (int(row["account_id"]), target_channel_id),
                            )
                        conn.execute(
                            """UPDATE direct_message_deliveries
                               SET status='uncertain', error=?,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE task_id=? AND status='sending'""",
                            (message, int(row["task_id"])),
                        )
                        if row["channel_id"] is not None and row["post_id"] is not None:
                            conn.execute(
                                """UPDATE comment_deliveries
                                   SET status='uncertain', error=?,
                                       updated_at=CURRENT_TIMESTAMP
                                   WHERE account_id=? AND campaign_id=?
                                     AND action_type='campaign_comment'
                                     AND channel_id=? AND post_id=?
                                     AND status='sending'""",
                                (
                                    message,
                                    int(row["account_id"]),
                                    int(row["campaign_id"]),
                                    int(row["channel_id"]),
                                    int(row["post_id"]),
                                ),
                            )
                        conn.execute(
                            """UPDATE comment_campaigns
                               SET status='paused', pause_reason=?,
                                   network_retry_at=NULL,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id=? AND status IN ('running','network_wait')""",
                            (
                                "Кампания приостановлена: результат отправки неизвестен; требуется ручная проверка",
                                int(row["campaign_id"]),
                            ),
                        )
                    affected_campaign_ids.add(int(row["campaign_id"]))

                # Match finish_comment_slot(): finite campaigns finish as soon
                # as their last slot is resolved, while continuous campaigns
                # enter an explicit wait state until the 24-hour boundary.
                for campaign_id in affected_campaign_ids:
                    remaining = conn.execute(
                        """SELECT COUNT(*) AS total FROM comment_schedule
                           WHERE campaign_id=?
                             AND status IN ('pending','queued','running')""",
                        (campaign_id,),
                    ).fetchone()
                    if int(remaining["total"] if remaining else 0) != 0:
                        continue
                    campaign = conn.execute(
                        "SELECT continuous FROM comment_campaigns WHERE id=?",
                        (campaign_id,),
                    ).fetchone()
                    continuous = bool(campaign["continuous"]) if campaign else False
                    conn.execute(
                        """UPDATE comment_campaigns
                           SET status=?, pause_reason=?, updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status='running'""",
                        (
                            "cycle_wait" if continuous else "completed",
                            (
                                "Все запланированные каналы обработаны; "
                                "ожидание следующего 24-часового цикла"
                                if continuous
                                else "Все запланированные каналы обработаны"
                            ),
                            campaign_id,
                        ),
                    )
                return len(rows) + len(orphan_rows)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to reconcile campaign schedule: {exc}"
            ) from exc

    @staticmethod
    def _delivery_context(
        *,
        campaign_id=None,
        action_type=None,
        linked_chat_id=None,
    ) -> tuple[int, str, int]:
        return (
            max(0, int(campaign_id or 0)),
            str(action_type or "comment").strip() or "comment",
            int(linked_chat_id or 0),
        )

    def reserve_comment_delivery(
        self,
        channel_id,
        post_id,
        *,
        linked_chat_id=None,
        text=None,
        account_id=None,
        campaign_id=None,
        action_type="comment",
    ):
        """Durably reserve one fully scoped delivery before Telegram send."""
        try:
            owner_account_id = resolve_account_id(self, account_id)
            campaign, action, discussion = self._delivery_context(
                campaign_id=campaign_id,
                action_type=action_type,
                linked_chat_id=linked_chat_id,
            )
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO comment_deliveries(
                           account_id, campaign_id, action_type, channel_id, post_id,
                           linked_chat_id, text, status, reserved_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, 'sending',
                              CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                    (
                        owner_account_id,
                        campaign,
                        action,
                        int(channel_id),
                        int(post_id),
                        discussion,
                        text,
                    ),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to reserve comment delivery: {exc}") from exc

    def release_comment_delivery(
        self,
        channel_id,
        post_id,
        *,
        error=None,
        account_id=None,
        linked_chat_id=None,
        campaign_id=None,
        action_type="comment",
    ):
        del error, linked_chat_id, action_type
        try:
            owner_account_id = resolve_account_id(self, account_id)
            campaign = max(0, int(campaign_id or 0))
            with self.get_connection() as conn:
                conn.execute(
                    """DELETE FROM comment_deliveries
                       WHERE account_id=? AND channel_id=? AND post_id=?
                         AND campaign_id=? AND status='sending'""",
                    (
                        owner_account_id,
                        int(channel_id),
                        int(post_id),
                        campaign,
                    ),
                )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to release comment delivery: {exc}") from exc

    def mark_comment_delivery_uncertain(
        self,
        channel_id,
        post_id,
        error,
        *,
        account_id=None,
        linked_chat_id=None,
        campaign_id=None,
        action_type="comment",
    ):
        try:
            owner_account_id = resolve_account_id(self, account_id)
            campaign, action, discussion = self._delivery_context(
                campaign_id=campaign_id,
                action_type=action_type,
                linked_chat_id=linked_chat_id,
            )
            with self.get_connection() as conn:
                conn.execute(
                    """UPDATE comment_deliveries
                       SET status='uncertain', error=?, action_type=?, linked_chat_id=?,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE account_id=? AND channel_id=? AND post_id=?
                         AND campaign_id=?
                         AND status IN ('sending','uncertain')""",
                    (
                        sanitize_text(error),
                        action,
                        discussion,
                        owner_account_id,
                        int(channel_id),
                        int(post_id),
                        campaign,
                    ),
                )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to mark uncertain delivery: {exc}") from exc

    def finalize_comment_delivery(self, data):
        """Persist comment and delivery receipt in one SQLite transaction."""
        try:
            owner_account_id = resolve_account_id(self, data.get("account_id"))
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
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
                campaign, action, discussion = self._delivery_context(
                    campaign_id=data.get("campaign_id"),
                    action_type=data.get("action_type"),
                    linked_chat_id=data.get("linked_chat_id"),
                )
                cursor = conn.execute(
                    """UPDATE comment_deliveries
                       SET status='sent', comment_message_id=?, text=?, error=NULL,
                           action_type=?, linked_chat_id=?, updated_at=CURRENT_TIMESTAMP
                       WHERE account_id=? AND channel_id=? AND post_id=?
                         AND campaign_id=?
                         AND status='sending'""",
                    (
                        data.get("comment_message_id"),
                        data.get("text"),
                        action,
                        discussion,
                        owner_account_id,
                        data.get("channel_id"),
                        data.get("post_message_id"),
                        campaign,
                    ),
                )
                if cursor.rowcount != 1:
                    # The reservation is the only state a confirmed send may be
                    # written from.  A row that already moved to sent/failed/
                    # uncertain must never be promoted to sent without a new
                    # reservation, otherwise a lost-response retry could turn an
                    # unproven outcome into a reported success.
                    raise DatabaseError(
                        "Delivery reservation disappeared before finalization"
                    )
                return True
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to finalize comment delivery: {exc}") from exc

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


class CommentFinalizationMixin(_MixinHost):
    """Atomic slot and campaign finalization."""

    def finish_comment_slot(
        self,
        slot_id,
        *,
        status,
        result,
        channel_id=None,
        post_id=None,
        sent=False,
        selected_text=None,
    ):
        final_status = str(status)
        if final_status not in {"sent", "skipped", "failed", "uncertain", "missed"}:
            raise DatabaseError(f"Invalid final slot status: {status}")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """SELECT s.campaign_id, s.status, c.account_id
                       FROM comment_schedule s
                       JOIN comment_campaigns c ON c.id=s.campaign_id
                       WHERE s.id=?""",
                    (int(slot_id),),
                ).fetchone()
                if row is None or row["status"] not in {"queued", "running"}:
                    return False
                conn.execute(
                    """UPDATE comment_schedule
                       SET status=?, channel_id=?, post_id=?, result=?,
                           executed_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (final_status, channel_id, post_id, str(result), int(slot_id)),
                )
                campaign_id = int(row["campaign_id"])
                conn.execute(
                    """UPDATE comment_campaigns
                       SET attempted_count=attempted_count+1,
                           sent_count=sent_count+?,
                           last_comment_text=CASE WHEN ? THEN ? ELSE last_comment_text END,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (
                        1 if sent else 0,
                        1 if sent else 0,
                        selected_text,
                        campaign_id,
                    ),
                )
                remaining = conn.execute(
                    """SELECT COUNT(*) AS total FROM comment_schedule
                       WHERE campaign_id=? AND status IN ('pending','queued','running')""",
                    (campaign_id,),
                ).fetchone()
                campaign = conn.execute(
                    "SELECT status, continuous FROM comment_campaigns WHERE id=?",
                    (campaign_id,),
                ).fetchone()
                if (
                    int(remaining["total"] if remaining else 0) == 0
                    and campaign is not None
                    and campaign["status"] == "running"
                ):
                    continuous = bool(campaign["continuous"])
                    conn.execute(
                        """UPDATE comment_campaigns
                           SET status=?, pause_reason=?,
                               updated_at=CURRENT_TIMESTAMP
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
                return True
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to finish campaign slot: {exc}") from exc

    def finalize_comment_slot_outcome(
        self,
        task_id,
        slot_id,
        *,
        status,
        result,
        channel_id=None,
        post_id=None,
        selected_text=None,
        sent=False,
        consume_channel=False,
        campaign_pause_reason=None,
        task_failed=False,
        task_error=None,
        expected_campaign_id=None,
        expected_account_id=None,
    ):
        """Atomically finalize one handled comment slot and its queue task.

        The external Telegram result is already protected by the delivery ledger.
        This transaction keeps the local history, rotation cooldown, slot status,
        campaign counters and queue task marker consistent with each other.
        """

        final_status = str(status)
        if final_status not in {"sent", "skipped", "failed", "uncertain", "missed"}:
            raise DatabaseError(f"Invalid final slot status: {status}")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """SELECT s.campaign_id, s.status, s.task_id, c.account_id,
                              t.type AS task_type, t.status AS task_status
                       FROM comment_schedule s
                       JOIN comment_campaigns c ON c.id=s.campaign_id
                       LEFT JOIN tasks t ON t.id=s.task_id
                       WHERE s.id=?""",
                    (int(slot_id),),
                ).fetchone()
                if row is None or row["status"] not in {"queued", "running"}:
                    return False

                if row["task_id"] is None or int(row["task_id"]) != int(task_id):
                    raise DatabaseError("Comment slot is bound to another queue task")
                if str(row["task_type"] or "") != "auto_comment_slot":
                    raise DatabaseError("Comment slot task has an invalid task type")
                if str(row["task_status"] or "") != "running":
                    raise DatabaseError(
                        "Comment slot task is not running during finalization"
                    )
                if expected_campaign_id is not None and int(row["campaign_id"]) != int(
                    expected_campaign_id
                ):
                    raise DatabaseError("Comment slot belongs to another campaign")
                if expected_account_id is not None and int(
                    row["account_id"] or 0
                ) != int(expected_account_id):
                    raise DatabaseError(
                        "Comment slot belongs to another Telegram account"
                    )

                campaign_id = int(row["campaign_id"])
                conn.execute(
                    """INSERT INTO comment_history(account_id, task_id, campaign_id, slot_id,
                           channel_id, post_id, comment_text, sent_at, status)
                       VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)""",
                    (
                        int(row["account_id"]),
                        int(task_id),
                        campaign_id,
                        int(slot_id),
                        channel_id,
                        post_id,
                        selected_text,
                        str(result),
                    ),
                )

                if consume_channel and channel_id is not None:
                    conn.execute(
                        """UPDATE channels
                           SET last_comment_check_at=CURRENT_TIMESTAMP
                           WHERE account_id=? AND channel_id=?""",
                        (int(row["account_id"]), int(channel_id)),
                    )

                updated = conn.execute(
                    """UPDATE comment_schedule
                       SET status=?, channel_id=?, post_id=?, result=?,
                           executed_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('queued','running')""",
                    (
                        final_status,
                        channel_id,
                        post_id,
                        str(result),
                        int(slot_id),
                    ),
                )
                if updated.rowcount != 1:
                    raise DatabaseError("Comment slot changed during finalization")

                conn.execute(
                    """UPDATE comment_campaigns
                       SET attempted_count=attempted_count+1,
                           sent_count=sent_count+?,
                           last_comment_text=CASE WHEN ? THEN ? ELSE last_comment_text END,
                           network_failure_count=0,
                           network_retry_at=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (
                        1 if sent else 0,
                        1 if sent else 0,
                        selected_text,
                        campaign_id,
                    ),
                )
                if campaign_pause_reason:
                    conn.execute(
                        """UPDATE comment_campaigns
                           SET status='paused', pause_reason=?, network_retry_at=NULL,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status IN ('running','network_wait')""",
                        (str(campaign_pause_reason), campaign_id),
                    )

                remaining = conn.execute(
                    """SELECT COUNT(*) AS total FROM comment_schedule
                       WHERE campaign_id=? AND status IN ('pending','queued','running')""",
                    (campaign_id,),
                ).fetchone()
                campaign = conn.execute(
                    "SELECT status, continuous FROM comment_campaigns WHERE id=?",
                    (campaign_id,),
                ).fetchone()
                if (
                    int(remaining["total"] if remaining else 0) == 0
                    and campaign is not None
                    and campaign["status"] == "running"
                    and not campaign_pause_reason
                ):
                    continuous = bool(campaign["continuous"])
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

                task_status = "failed" if task_failed else "completed"
                task_message = str(task_error or result) if task_failed else None
                task_update = conn.execute(
                    """UPDATE tasks
                       SET status=?, progress=100, status_text=NULL, error=?,
                           not_before=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='running'""",
                    (task_status, task_message, int(task_id)),
                )
                if task_update.rowcount != 1:
                    raise DatabaseError(
                        "Queue task changed during comment finalization"
                    )
                return True
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to atomically finalize comment slot: {exc}"
            ) from exc

    def finalize_comment_slot_outcome_with_restriction(
        self,
        task_id,
        slot_id,
        *,
        restriction_kwargs,
        **outcome_kwargs,
    ):
        """Finalize a critical comment outcome and RESTRICTED in one transaction."""

        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                finalized = self.finalize_comment_slot_outcome(
                    task_id,
                    slot_id,
                    **outcome_kwargs,
                )
                if not finalized:
                    raise DatabaseError(
                        "Comment slot changed before restricted finalization"
                    )
                state = self.activate_account_restriction_atomic(
                    **dict(restriction_kwargs)
                )
                result = dict(state or {})
                result["finalized"] = True
                return result
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to atomically finalize restricted comment slot: {exc}"
            ) from exc

    def pause_campaign_for_safety(self, campaign_id, reason):
        return self.pause_comment_campaign(campaign_id, reason=reason)

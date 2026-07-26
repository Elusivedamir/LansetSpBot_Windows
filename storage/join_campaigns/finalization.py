from __future__ import annotations

from typing import TYPE_CHECKING

import logging

from core.campaign_schedule import to_db_time_precise, utc_now
from storage.db_common import DatabaseError

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class JoinFinalizationMixin(_MixinHost):
    """Atomic join slot finalization."""

    def finalize_join_slot_outcome(
        self,
        task_id,
        slot_id,
        *,
        status,
        result,
        joined=False,
        saved_dialog_id,
        account_id,
        membership_status=None,
        membership_error=None,
        join_event_peer_id=None,
        campaign_pause_reason=None,
        task_failed=False,
        task_error=None,
    ):
        """Commit membership, join event, slot, campaign and task atomically."""

        final_status = str(status)
        allowed = {
            "joined",
            "already_member",
            "join_requested",
            "skipped",
            "failed",
            "uncertain",
            "cancelled",
        }
        if final_status not in allowed:
            raise DatabaseError(f"Invalid final join slot status: {status}")
        if joined and final_status != "joined":
            raise DatabaseError("joined=True requires status='joined'")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT campaign_id, status FROM join_schedule WHERE id=?",
                    (int(slot_id),),
                ).fetchone()
                if row is None or row["status"] not in {"queued", "running"}:
                    return False
                campaign_id = int(row["campaign_id"])

                if membership_status is not None:
                    conn.execute(
                        """INSERT INTO saved_dialog_memberships(
                               saved_dialog_id, account_id, status, last_error, updated_at)
                           VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
                           ON CONFLICT(saved_dialog_id, account_id) DO UPDATE SET
                               status=excluded.status,
                               last_error=excluded.last_error,
                               updated_at=CURRENT_TIMESTAMP""",
                        (
                            int(saved_dialog_id),
                            int(account_id),
                            str(membership_status),
                            None if membership_error is None else str(membership_error),
                        ),
                    )

                if join_event_peer_id is not None:
                    conn.execute(
                        """INSERT INTO join_events(
                               linked_chat_id, joined_at, result, campaign_id,
                               saved_dialog_id, account_id)
                           VALUES(?, ?, 'joined', ?, ?, ?)""",
                        (
                            int(join_event_peer_id or 0),
                            to_db_time_precise(utc_now()),
                            campaign_id,
                            int(saved_dialog_id),
                            int(account_id),
                        ),
                    )

                changed = conn.execute(
                    """UPDATE join_schedule
                       SET status=?, result=?, executed_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('queued','running')""",
                    (final_status, str(result), int(slot_id)),
                )
                if changed.rowcount != 1:
                    raise DatabaseError("Join slot changed during finalization")

                conn.execute(
                    """UPDATE join_campaigns
                       SET attempted_count=attempted_count+1,
                           joined_count=joined_count+?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (1 if joined else 0, campaign_id),
                )
                if campaign_pause_reason:
                    conn.execute(
                        """UPDATE join_campaigns
                           SET status='paused', pause_reason=?, network_retry_at=NULL,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status IN ('running','network_wait')""",
                        (str(campaign_pause_reason), campaign_id),
                    )

                remaining = conn.execute(
                    """SELECT COUNT(*) AS total FROM join_schedule
                       WHERE campaign_id=? AND status IN ('pending','queued','running')""",
                    (campaign_id,),
                ).fetchone()
                if (
                    int(remaining["total"] if remaining else 0) == 0
                    and not campaign_pause_reason
                ):
                    conn.execute(
                        """UPDATE join_campaigns
                           SET status='completed',
                               pause_reason='Все запланированные вступления обработаны',
                               network_retry_at=NULL, updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status IN ('running','paused','network_wait')""",
                        (campaign_id,),
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
                    raise DatabaseError("Queue task changed during join finalization")
                return True
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to atomically finalize join slot: {exc}"
            ) from exc

    def finalize_join_slot_outcome_with_restriction(
        self,
        task_id,
        slot_id,
        *,
        restriction_kwargs,
        **outcome_kwargs,
    ):
        """Finalize a critical Join outcome and RESTRICTED in one transaction."""

        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                finalized = self.finalize_join_slot_outcome(
                    task_id,
                    slot_id,
                    **outcome_kwargs,
                )
                if not finalized:
                    raise DatabaseError(
                        "Join slot changed before restricted finalization"
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
                f"Failed to atomically finalize restricted Join slot: {exc}"
            ) from exc

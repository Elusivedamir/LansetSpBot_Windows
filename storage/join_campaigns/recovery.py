from __future__ import annotations

from typing import TYPE_CHECKING

import logging
from datetime import timedelta

from core.campaign_schedule import (
    generate_join_slots,
    to_db_time,
    utc_now,
)
from storage.db_common import DatabaseError, resolve_account_id

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class JoinRecoveryMixin(_MixinHost):
    """Join campaign lifecycle recovery and reconciliation."""

    def pause_join_campaign(self, campaign_id, reason):
        try:
            with self.get_connection() as conn:
                cur = conn.execute(
                    """UPDATE join_campaigns SET status='paused',pause_reason=?,updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('running','network_wait')""",
                    (str(reason), int(campaign_id)),
                )
                return cur.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to pause join campaign: {exc}") from exc

    def stop_join_campaign(self, campaign_id, reason="Остановлено пользователем"):
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """UPDATE join_campaigns SET status='stopped',pause_reason=?,updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('running','paused','network_wait')""",
                    (str(reason), int(campaign_id)),
                )
                if cur.rowcount:
                    conn.execute(
                        "UPDATE join_schedule SET status='cancelled',result=?,executed_at=CURRENT_TIMESTAMP WHERE campaign_id=? AND status='pending'",
                        (str(reason), int(campaign_id)),
                    )
                return cur.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to stop join campaign: {exc}") from exc

    def complete_join_campaign(
        self, campaign_id, reason="Кампания вступлений завершена"
    ):
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """UPDATE join_campaigns
                       SET status='completed', pause_reason=?, network_retry_at=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('running','paused','network_wait')""",
                    (str(reason), int(campaign_id)),
                )
                if cur.rowcount:
                    conn.execute(
                        """UPDATE join_schedule
                           SET status='cancelled', result=?, executed_at=CURRENT_TIMESTAMP
                           WHERE campaign_id=? AND status='pending'""",
                        (str(reason), int(campaign_id)),
                    )
                return cur.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to complete join campaign: {exc}") from exc

    def resume_join_campaign(self, campaign_id):
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                campaign = conn.execute(
                    """SELECT status, max_per_hour FROM join_campaigns
                       WHERE id=?""",
                    (int(campaign_id),),
                ).fetchone()
                if not campaign or campaign["status"] not in {"paused", "network_wait"}:
                    return False
                pending = conn.execute(
                    """SELECT id FROM join_schedule
                       WHERE campaign_id=? AND status='pending'
                       ORDER BY slot_index""",
                    (int(campaign_id),),
                ).fetchall()
                now = utc_now()
                moments = generate_join_slots(
                    now,
                    len(pending),
                    max_per_hour=int(campaign["max_per_hour"] or 40),
                )
                for row, moment in zip(pending, moments):
                    conn.execute(
                        "UPDATE join_schedule SET scheduled_at=?, result=NULL WHERE id=?",
                        (to_db_time(moment), int(row["id"])),
                    )
                ends_at = moments[-1] + timedelta(minutes=5) if moments else now
                cur = conn.execute(
                    """UPDATE join_campaigns
                       SET status='running', pause_reason=NULL, network_retry_at=NULL,
                           ends_at=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('paused','network_wait')""",
                    (to_db_time(ends_at), int(campaign_id)),
                )
                return cur.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to resume join campaign: {exc}") from exc

    def defer_join_slot_and_set_network_wait(
        self,
        task_id,
        slot_id,
        campaign_id,
        *,
        scheduled_at,
        slot_result,
        reason,
    ):
        """Atomically defer one join slot and pause its campaign for network retry."""
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """SELECT s.status AS slot_status, s.task_id,
                              c.status AS campaign_status
                       FROM join_schedule s
                       JOIN join_campaigns c ON c.id=s.campaign_id
                       WHERE s.id=? AND s.campaign_id=?""",
                    (int(slot_id), int(campaign_id)),
                ).fetchone()
                if (
                    row is None
                    or row["slot_status"] not in {"queued", "running"}
                    or int(row["task_id"] or 0) != int(task_id)
                    or row["campaign_status"]
                    not in {"running", "paused", "network_wait"}
                ):
                    return False

                slot_cursor = conn.execute(
                    """UPDATE join_schedule
                       SET status='pending', task_id=NULL, scheduled_at=?, result=?,
                           executed_at=NULL
                       WHERE id=? AND campaign_id=? AND task_id=?
                         AND status IN ('queued','running')""",
                    (
                        to_db_time(scheduled_at),
                        str(slot_result),
                        int(slot_id),
                        int(campaign_id),
                        int(task_id),
                    ),
                )
                if slot_cursor.rowcount != 1:
                    raise DatabaseError("Join slot changed while entering network wait")

                if row["campaign_status"] == "running":
                    campaign_cursor = conn.execute(
                        """UPDATE join_campaigns
                           SET status='network_wait', pause_reason=?, network_retry_at=?,
                               network_failure_count=network_failure_count+1,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status='running'""",
                        (str(reason), to_db_time(scheduled_at), int(campaign_id)),
                    )
                    if campaign_cursor.rowcount != 1:
                        raise DatabaseError(
                            "Join campaign changed while entering network wait"
                        )
                return True
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to atomically defer join slot for network wait: {exc}"
            ) from exc

    def set_join_campaign_network_wait(self, campaign_id, retry_at, reason):
        try:
            with self.get_connection() as conn:
                cur = conn.execute(
                    """UPDATE join_campaigns SET status='network_wait', pause_reason=?, network_retry_at=?,
                           network_failure_count=network_failure_count+1, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='running'""",
                    (str(reason), to_db_time(retry_at), int(campaign_id)),
                )
                return cur.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to pause join campaign for network: {exc}"
            ) from exc

    def reconcile_join_schedule(self, account_id=None):
        owner_account_id = (
            resolve_account_id(self, account_id)
            if account_id is not None
            else 0
        )
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                repaired = 0
                affected_campaign_ids = set()

                orphan_query = """SELECT s.id, s.campaign_id,
                                         c.status AS campaign_status
                                  FROM join_schedule s
                                  JOIN join_campaigns c
                                    ON c.id=s.campaign_id
                                  LEFT JOIN tasks t ON t.id=s.task_id
                                  WHERE s.status IN ('queued','running')
                                    AND (
                                        s.task_id IS NULL
                                        OR t.id IS NULL
                                    )"""
                orphan_params: tuple[object, ...] = ()
                if owner_account_id > 0:
                    orphan_query += " AND c.account_id=?"
                    orphan_params = (owner_account_id,)

                orphan_rows = conn.execute(
                    orphan_query,
                    orphan_params,
                ).fetchall()
                for row in orphan_rows:
                    campaign_id = int(row["campaign_id"])
                    if row["campaign_status"] in {"running", "paused", "network_wait"}:
                        conn.execute(
                            """UPDATE join_schedule
                               SET status='pending', task_id=NULL,
                                   result='Восстановлено после потери задачи',
                                   executed_at=NULL
                               WHERE id=? AND status IN ('queued','running')""",
                            (int(row["id"]),),
                        )
                    else:
                        conn.execute(
                            """UPDATE join_schedule
                               SET status='cancelled', task_id=NULL,
                                   result='Кампания уже завершена',
                                   executed_at=CURRENT_TIMESTAMP
                               WHERE id=? AND status IN ('queued','running')""",
                            (int(row["id"]),),
                        )
                    repaired += 1
                    affected_campaign_ids.add(campaign_id)

                completed_query = """SELECT
                                             s.id,
                                             s.campaign_id,
                                             s.saved_dialog_id,
                                             c.account_id,
                                             t.status,
                                             t.error
                                        FROM join_schedule s
                                        JOIN join_campaigns c
                                          ON c.id=s.campaign_id
                                        JOIN tasks t
                                          ON t.id=s.task_id
                                        WHERE s.status IN ('queued','running')
                                          AND t.status IN (
                                              'failed',
                                              'cancelled',
                                              'completed'
                                          )"""
                completed_params: tuple[object, ...] = ()
                if owner_account_id > 0:
                    completed_query += " AND c.account_id=?"
                    completed_params = (owner_account_id,)

                rows = conn.execute(
                    completed_query,
                    completed_params,
                ).fetchall()
                for row in rows:
                    if row["status"] == "cancelled":
                        msg = str(row["error"] or "Задача отменена пользователем")
                        # A user cancellation pauses the campaign without spending
                        # the join target. Detach the cancelled task and keep the
                        # slot resumable.
                        changed = conn.execute(
                            """UPDATE join_schedule
                               SET status='pending', task_id=NULL, result=?,
                                   executed_at=NULL
                               WHERE id=? AND status IN ('queued','running')""",
                            (msg, int(row["id"])),
                        ).rowcount
                        if changed:
                            conn.execute(
                                """UPDATE join_campaigns
                                   SET status='paused', pause_reason=?,
                                       network_retry_at=NULL,
                                       updated_at=CURRENT_TIMESTAMP
                                   WHERE id=?
                                     AND status IN ('running','network_wait')""",
                                (
                                    "Задача кампании отменена; продолжите или остановите кампанию вручную",
                                    int(row["campaign_id"]),
                                ),
                            )
                            repaired += 1
                            affected_campaign_ids.add(int(row["campaign_id"]))
                        continue

                    ambiguous = (
                        row["status"] == "completed"
                        or "uncertain" in str(row["error"] or "").lower()
                    )
                    status = "uncertain" if ambiguous else "failed"
                    msg = str(row["error"] or "Задача завершилась без результата")
                    changed = conn.execute(
                        """UPDATE join_schedule
                           SET status=?, result=?, executed_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status IN ('queued','running')""",
                        (status, msg, int(row["id"])),
                    ).rowcount
                    if changed:
                        conn.execute(
                            """UPDATE join_campaigns
                               SET attempted_count=attempted_count+1,
                                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                            (int(row["campaign_id"]),),
                        )
                        if ambiguous:
                            conn.execute(
                                """INSERT INTO saved_dialog_memberships(
                                       saved_dialog_id, account_id, status,
                                       last_error, updated_at)
                                   VALUES(?, ?, 'uncertain', ?, CURRENT_TIMESTAMP)
                                   ON CONFLICT(saved_dialog_id, account_id) DO UPDATE SET
                                       status='uncertain', last_error=excluded.last_error,
                                       updated_at=CURRENT_TIMESTAMP""",
                                (
                                    int(row["saved_dialog_id"]),
                                    int(row["account_id"]),
                                    msg,
                                ),
                            )
                            conn.execute(
                                """UPDATE join_campaigns
                                   SET status='paused', pause_reason=?,
                                       network_retry_at=NULL,
                                       updated_at=CURRENT_TIMESTAMP
                                   WHERE id=?
                                     AND status IN ('running','network_wait')""",
                                (
                                    "Кампания приостановлена: результат вступления неизвестен; требуется синхронизация диалогов",
                                    int(row["campaign_id"]),
                                ),
                            )
                        repaired += 1
                        affected_campaign_ids.add(int(row["campaign_id"]))

                for campaign_id in affected_campaign_ids:
                    remaining = conn.execute(
                        """SELECT COUNT(*) AS total FROM join_schedule
                           WHERE campaign_id=?
                             AND status IN ('pending','queued','running')""",
                        (campaign_id,),
                    ).fetchone()
                    if int(remaining["total"] if remaining else 0) == 0:
                        conn.execute(
                            """UPDATE join_campaigns
                               SET status='completed',
                                   pause_reason='Все запланированные вступления обработаны',
                                   network_retry_at=NULL,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id=?
                                 AND status IN ('running','network_wait')""",
                            (campaign_id,),
                        )
                return repaired
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to reconcile join schedule: {exc}") from exc

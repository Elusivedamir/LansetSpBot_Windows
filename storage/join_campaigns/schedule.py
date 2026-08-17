from __future__ import annotations

from typing import TYPE_CHECKING

import logging
from storage.sqlcipher_driver import dbapi as sqlite3
from datetime import timedelta

from core.campaign_schedule import (
    generate_join_slots,
    to_db_time,
    utc_now,
)
from storage.db_common import DatabaseError, json_dumps_safe

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class JoinScheduleMixin(_MixinHost):
    """Join slot scheduling and execution state."""

    def redistribute_pending_join_slots(self, campaign_id, *, now=None, rng=None):
        now = now or utc_now()
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                campaign = conn.execute(
                    "SELECT status, max_per_hour FROM join_campaigns WHERE id=?",
                    (int(campaign_id),),
                ).fetchone()
                if not campaign or campaign["status"] != "running":
                    return 0
                pending = conn.execute(
                    "SELECT id FROM join_schedule WHERE campaign_id=? AND status='pending' ORDER BY slot_index",
                    (int(campaign_id),),
                ).fetchall()
                if not pending:
                    return 0
                overdue = conn.execute(
                    "SELECT 1 FROM join_schedule WHERE campaign_id=? AND status='pending' AND scheduled_at<? LIMIT 1",
                    (int(campaign_id), to_db_time(now - timedelta(minutes=2))),
                ).fetchone()
                if not overdue:
                    return 0
                moments = generate_join_slots(
                    now,
                    len(pending),
                    rng=rng,
                    max_per_hour=int(campaign["max_per_hour"] or 40),
                )
                for row, moment in zip(pending, moments):
                    conn.execute(
                        "UPDATE join_schedule SET scheduled_at=?, result=NULL WHERE id=?",
                        (to_db_time(moment), int(row["id"])),
                    )
                conn.execute(
                    "UPDATE join_campaigns SET ends_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (
                        to_db_time(
                            (moments[-1] + timedelta(minutes=5)) if moments else now
                        ),
                        int(campaign_id),
                    ),
                )
                return len(pending)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to redistribute join slots: {exc}") from exc

    def queue_due_join_slot(self, *, now=None):
        now = now or utc_now()
        now_text = to_db_time(now)
        conn = None
        try:
            conn = sqlite3.connect(
                str(self.path),
                timeout=self.sqlite_timeout_seconds,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT s.id AS slot_id, s.campaign_id, c.account_id FROM join_schedule s
                   JOIN join_campaigns c ON c.id=s.campaign_id
                   JOIN saved_dialogs d ON d.id=s.saved_dialog_id
                   WHERE c.status='running' AND s.status='pending' AND s.scheduled_at<=?
                     AND NOT EXISTS(
                         SELECT 1 FROM local_ban_targets b
                         WHERE b.account_id=c.account_id AND b.peer_id=d.peer_id
                     )
                     AND NOT EXISTS(
                         SELECT 1
                         FROM join_schedule active_s
                         JOIN join_campaigns active_c
                           ON active_c.id=active_s.campaign_id
                         JOIN tasks active_t ON active_t.id=active_s.task_id
                         WHERE active_c.account_id=c.account_id
                           AND active_s.status IN ('queued','running')
                           AND active_t.status IN ('pending','running')
                     )
                   ORDER BY s.scheduled_at, s.id LIMIT 1""",
                (now_text,),
            ).fetchone()
            if not row:
                conn.commit()
                return None
            payload = json_dumps_safe(
                {
                    "campaign_id": int(row["campaign_id"]),
                    "slot_id": int(row["slot_id"]),
                    "account_id": int(row["account_id"]),
                }
            )
            cur = conn.execute(
                """INSERT INTO tasks(
                       account_id,type,payload,status,progress,max_retries,created_at,updated_at
                   )
                   VALUES(?,'join_saved_slot',?,'pending',0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                (int(row["account_id"]), payload),
            )
            if cur.lastrowid is None:
                raise DatabaseError("SQLite did not return a task id")
            task_id = int(cur.lastrowid)
            upd = conn.execute(
                "UPDATE join_schedule SET status='queued', task_id=? WHERE id=? AND status='pending'",
                (task_id, int(row["slot_id"])),
            )
            if upd.rowcount != 1:
                conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                conn.rollback()
                return None
            conn.commit()
            return {
                "task_id": task_id,
                "campaign_id": int(row["campaign_id"]),
                "slot_id": int(row["slot_id"]),
                "account_id": int(row["account_id"]),
            }
        except sqlite3.Error as exc:
            if conn:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            raise DatabaseError(f"Failed to queue join slot: {exc}") from exc
        finally:
            if conn:
                conn.close()

    def get_join_slot_context(self, campaign_id, slot_id):
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT s.*, c.account_id, c.status AS campaign_status, c.max_per_hour,
                              d.peer_id,d.username,d.title,d.kind,d.invite_link
                       FROM join_schedule s JOIN join_campaigns c ON c.id=s.campaign_id
                       JOIN saved_dialogs d ON d.id=s.saved_dialog_id
                       WHERE s.id=? AND s.campaign_id=?""",
                    (int(slot_id), int(campaign_id)),
                ).fetchone()
                return dict(row) if row else None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read join slot: {exc}") from exc

    def mark_join_slot_running(self, slot_id, task_id):
        try:
            with self.get_connection() as conn:
                cur = conn.execute(
                    "UPDATE join_schedule SET status='running' WHERE id=? AND task_id=? AND status='queued'",
                    (int(slot_id), int(task_id)),
                )
                return cur.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to start join slot: {exc}") from exc

    def defer_join_slot(self, slot_id, scheduled_at, result):
        try:
            with self.get_connection() as conn:
                cur = conn.execute(
                    """UPDATE join_schedule SET status='pending',task_id=NULL,scheduled_at=?,result=?,executed_at=NULL
                       WHERE id=? AND status IN ('queued','running')""",
                    (to_db_time(scheduled_at), str(result), int(slot_id)),
                )
                return cur.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to defer join slot: {exc}") from exc

    def cancel_join_slot(self, slot_id, *, result="Кампания остановлена"):
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE join_schedule
                       SET status='cancelled', task_id=NULL, result=?,
                           executed_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('pending','queued','running')""",
                    (str(result), int(slot_id)),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to cancel join slot: {exc}") from exc

    def finish_join_slot(self, slot_id, *, status, result, joined=False):
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
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT campaign_id FROM join_schedule WHERE id=?", (int(slot_id),)
                ).fetchone()
                if not row:
                    return False
                campaign_id = int(row["campaign_id"])
                cur = conn.execute(
                    """UPDATE join_schedule
                       SET status=?, result=?, executed_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('queued','running')""",
                    (final_status, str(result), int(slot_id)),
                )
                if cur.rowcount:
                    conn.execute(
                        """UPDATE join_campaigns
                           SET attempted_count=attempted_count+1,
                               joined_count=joined_count+?, updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (1 if joined else 0, campaign_id),
                    )
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
                                 AND status IN ('running','paused','network_wait')""",
                            (campaign_id,),
                        )
                return cur.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to finish join slot: {exc}") from exc

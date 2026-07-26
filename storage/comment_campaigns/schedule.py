from __future__ import annotations

from typing import TYPE_CHECKING

import logging
from storage.sqlcipher_driver import dbapi as sqlite3
from datetime import timedelta

from core.config import (
    DEFAULT_MAX_CHANNELS_PER_RUN,
    DEFAULT_POST_JOIN_DELAY_MAX_SECONDS,
)
from core.campaign_schedule import (
    from_db_time,
    redistribute_slots,
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


class CommentScheduleMixin(_MixinHost):
    """Pending-slot scheduling and queue binding."""

    def _redistribute_pending_comment_slots_in_transaction(
        self,
        conn,
        campaign_id,
        *,
        now,
        grace_seconds=180,
        force=False,
        rng=None,
    ):
        """Redistribute pending rows using the caller's active write transaction."""
        grace = max(0, int(grace_seconds))
        cutoff = now - timedelta(seconds=grace)
        campaign = conn.execute(
            """SELECT status, started_at, ends_at, daily_limit, cadence_seconds
               FROM comment_campaigns WHERE id=?""",
            (int(campaign_id),),
        ).fetchone()
        if campaign is None or campaign["status"] != "running":
            return 0

        start_at = from_db_time(campaign["started_at"])
        end = from_db_time(campaign["ends_at"])
        if start_at is None or end is None:
            return 0
        if end <= now and not force:
            return 0

        pending = conn.execute(
            """SELECT id, scheduled_at FROM comment_schedule
               WHERE campaign_id=? AND status='pending'
               ORDER BY slot_index ASC""",
            (int(campaign_id),),
        ).fetchall()
        if not pending:
            return 0
        overdue = any(
            (from_db_time(row["scheduled_at"]) or now) < cutoff for row in pending
        )
        if not force and not overdue:
            return 0

        cadence_raw = campaign["cadence_seconds"]
        if cadence_raw is None or float(cadence_raw) <= 0:
            # Defensive compatibility for a malformed hand-edited DB.
            # Never derive cadence from the mutable, extended ends_at.
            daily_limit = max(
                1, int(campaign["daily_limit"] or DEFAULT_MAX_CHANNELS_PER_RUN)
            )
            target_interval = 86_400.0 / daily_limit
        else:
            target_interval = float(cadence_raw)
        safe_count = len(pending)
        # Preserve only the cadence selected by the slider after a
        # pause/restart/network wait. A fixed 15-30 minute (or 30-second)
        # lead would distort dense schedules such as 223 or 1000/day.
        cadence_horizon = now + timedelta(seconds=target_interval * safe_count)
        moments = redistribute_slots(
            now,
            cadence_horizon,
            safe_count,
            minimum_lead_seconds=0,
            rng=rng,
            minimum_gap_seconds=DEFAULT_POST_JOIN_DELAY_MAX_SECONDS,
        )
        if len(moments) != safe_count:
            raise DatabaseError(
                "Could not preserve every pending comment slot during redistribution"
            )

        if cadence_horizon > end:
            conn.execute(
                """UPDATE comment_campaigns
                   SET ends_at=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (to_db_time(cadence_horizon), int(campaign_id)),
            )

        moved = 0
        for row, moment in zip(pending, moments):
            cursor = conn.execute(
                """UPDATE comment_schedule SET scheduled_at=?, result=NULL
                   WHERE id=? AND status='pending'""",
                (to_db_time(moment), int(row["id"])),
            )
            moved += max(0, int(cursor.rowcount or 0))
        return moved

    def redistribute_pending_comment_slots(
        self,
        campaign_id,
        *,
        now=None,
        grace_seconds=180,
        force=False,
        rng=None,
    ):
        """Move pending slots forward without consuming them because of a pause.

        Manual pause, network wait, application shutdown and scheduler recovery
        are all suspension states, not completed attempts. Every still-pending
        slot is therefore preserved. When the original end is too close, the
        campaign window is extended just enough to retain its configured cadence
        instead of bursting or marking rows ``missed``.
        """
        now = now or utc_now()
        try:
            grace = max(0, int(grace_seconds))
            cutoff = now - timedelta(seconds=grace)

            # The scheduler calls this method every 10 seconds. Avoid taking a
            # SQLite writer reservation during the normal no-op path, then
            # repeat all authoritative reads after BEGIN IMMEDIATE below.
            if not force:
                with self.get_connection() as conn:
                    preflight = conn.execute(
                        """SELECT status, started_at, ends_at
                           FROM comment_campaigns WHERE id=?""",
                        (int(campaign_id),),
                    ).fetchone()
                    if preflight is None or preflight["status"] != "running":
                        return 0
                    preflight_end = from_db_time(preflight["ends_at"])
                    if preflight_end is None or preflight_end <= now:
                        return 0
                    overdue_row = conn.execute(
                        """SELECT 1 FROM comment_schedule
                           WHERE campaign_id=? AND status='pending'
                             AND scheduled_at < ?
                           LIMIT 1""",
                        (int(campaign_id), to_db_time(cutoff)),
                    ).fetchone()
                    if overdue_row is None:
                        return 0

            with self.get_connection() as conn:
                # The campaign snapshot, pending set, calculations and writes
                # are protected by one write transaction. The preflight above
                # is only a no-op optimization and is never trusted for updates.
                conn.execute("BEGIN IMMEDIATE")
                return self._redistribute_pending_comment_slots_in_transaction(
                    conn,
                    campaign_id,
                    now=now,
                    grace_seconds=grace_seconds,
                    force=force,
                    rng=rng,
                )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to redistribute campaign slots: {exc}"
            ) from exc

    def defer_comment_slot(self, slot_id, *, scheduled_at, result):
        """Return a queued/running slot to pending without consuming an attempt."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE comment_schedule
                       SET status='pending', task_id=NULL, scheduled_at=?, result=?, executed_at=NULL
                       WHERE id=? AND status IN ('queued','running')""",
                    (to_db_time(scheduled_at), str(result), int(slot_id)),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to defer campaign slot: {exc}") from exc

    def defer_comment_slot_and_set_network_wait(
        self,
        task_id,
        slot_id,
        campaign_id,
        *,
        scheduled_at,
        slot_result,
        reason,
    ):
        """Atomically defer one slot and pause its running campaign for network retry."""
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """SELECT s.status AS slot_status, s.task_id,
                              c.status AS campaign_status
                       FROM comment_schedule s
                       JOIN comment_campaigns c ON c.id=s.campaign_id
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
                    """UPDATE comment_schedule
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
                    raise DatabaseError(
                        "Comment slot changed while entering network wait"
                    )

                if row["campaign_status"] == "running":
                    campaign_cursor = conn.execute(
                        """UPDATE comment_campaigns
                           SET status='network_wait', pause_reason=?, network_retry_at=?,
                               network_failure_count=network_failure_count+1,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status='running'""",
                        (str(reason), to_db_time(scheduled_at), int(campaign_id)),
                    )
                    if campaign_cursor.rowcount != 1:
                        raise DatabaseError(
                            "Comment campaign changed while entering network wait"
                        )
                return True
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to atomically defer campaign slot for network wait: {exc}"
            ) from exc

    def cancel_comment_slot(self, slot_id, *, result):
        """Cancel one queued/running slot without consuming a campaign attempt."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE comment_schedule
                       SET status='cancelled', result=?, executed_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('queued','running')""",
                    (str(result), int(slot_id)),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to cancel campaign slot: {exc}") from exc

    def set_campaign_network_wait(self, campaign_id, *, retry_at, reason):
        """Pause scheduling automatically until the next network retry time."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE comment_campaigns
                       SET status='network_wait', pause_reason=?, network_retry_at=?,
                           network_failure_count=network_failure_count+1,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='running'""",
                    (str(reason), to_db_time(retry_at), int(campaign_id)),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to put campaign into network wait: {exc}"
            ) from exc

    def resume_network_wait_campaign(self, campaign_id, *, now=None, rng=None):
        """Atomically resume a network-wait campaign and rebuild pending cadence."""
        now = now or utc_now()
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """UPDATE comment_campaigns
                       SET status='running', pause_reason=NULL, network_retry_at=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='network_wait'""",
                    (int(campaign_id),),
                )
                if cursor.rowcount != 1:
                    return False
                self._redistribute_pending_comment_slots_in_transaction(
                    conn,
                    campaign_id,
                    now=now,
                    grace_seconds=0,
                    force=True,
                    rng=rng,
                )
                return True
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to resume network-wait campaign: {exc}"
            ) from exc

    def reset_campaign_network_failures(self, campaign_id):
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE comment_campaigns
                       SET network_failure_count=0, network_retry_at=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (int(campaign_id),),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to reset network failures: {exc}") from exc

    def queue_due_comment_slot(self, *, now=None):
        """Atomically convert one due schedule slot into one queue task."""
        now = now or utc_now()
        now_text = to_db_time(now)
        connection = None
        try:
            connection = sqlite3.connect(
                str(self.path),
                timeout=self.sqlite_timeout_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("BEGIN IMMEDIATE")
            active_task = connection.execute(
                """SELECT 1 FROM comment_schedule s
                   JOIN tasks t ON t.id=s.task_id
                   WHERE s.status IN ('queued','running')
                     AND t.status IN ('pending','running') LIMIT 1"""
            ).fetchone()
            if active_task is not None:
                connection.commit()
                return None
            row = connection.execute(
                """SELECT s.id AS slot_id, s.campaign_id, c.account_id
                   FROM comment_schedule s
                   JOIN comment_campaigns c ON c.id=s.campaign_id
                   WHERE c.status='running' AND s.status='pending'
                     AND s.scheduled_at<=? AND c.ends_at>?
                   ORDER BY s.scheduled_at ASC, s.id ASC LIMIT 1""",
                (now_text, now_text),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            payload = json_dumps_safe(
                {
                    "campaign_id": int(row["campaign_id"]),
                    "slot_id": int(row["slot_id"]),
                    "account_id": int(row["account_id"]),
                }
            )
            cursor = connection.execute(
                """INSERT INTO tasks(type, payload, status, progress, max_retries,
                                      created_at, updated_at)
                   VALUES('auto_comment_slot', ?, 'pending', 0, 0,
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (payload,),
            )
            if cursor.lastrowid is None:
                raise DatabaseError("SQLite did not return a task id")
            task_id = int(cursor.lastrowid)
            updated = connection.execute(
                """UPDATE comment_schedule SET status='queued', task_id=?
                   WHERE id=? AND status='pending'""",
                (task_id, int(row["slot_id"])),
            )
            if updated.rowcount != 1:
                connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                connection.rollback()
                return None
            connection.commit()
            return {
                "task_id": task_id,
                "campaign_id": int(row["campaign_id"]),
                "slot_id": int(row["slot_id"]),
                "account_id": int(row["account_id"]),
            }
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            raise DatabaseError(f"Failed to queue due campaign slot: {exc}") from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def mark_comment_slot_running(self, slot_id, task_id):
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE comment_schedule SET status='running'
                       WHERE id=? AND task_id=? AND status='queued'""",
                    (int(slot_id), int(task_id)),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to start campaign slot: {exc}") from exc

    def validate_comment_slot_execution_context(
        self,
        task_id,
        slot_id,
        campaign_id,
        account_id,
    ):
        """Return the atomically joined execution context or ``None``.

        A queue payload is not an authority by itself.  Before any Telegram
        lookup or shuffled-bag reservation, the task, slot and campaign must be
        joined through their durable foreign-key relationships and must still
        belong to the same Telegram account.
        """

        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT s.id AS slot_id, s.campaign_id, s.task_id,
                              s.status AS slot_status,
                              c.account_id, c.status AS campaign_status,
                              t.type AS task_type, t.status AS task_status
                       FROM comment_schedule s
                       JOIN comment_campaigns c ON c.id=s.campaign_id
                       JOIN tasks t ON t.id=s.task_id
                       WHERE s.id=? AND s.campaign_id=? AND s.task_id=?
                         AND c.id=? AND c.account_id=?
                         AND t.id=? AND t.type='auto_comment_slot'
                         AND t.status='running'
                         AND s.status IN ('queued','running')""",
                    (
                        int(slot_id),
                        int(campaign_id),
                        int(task_id),
                        int(campaign_id),
                        int(account_id),
                        int(task_id),
                    ),
                ).fetchone()
                return dict(row) if row else None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to validate comment slot execution context: {exc}"
            ) from exc

    def bind_comment_slot_target(
        self,
        slot_id,
        task_id,
        *,
        channel_id,
        post_id=None,
        linked_chat_id=None,
        discussion_message_id=None,
    ):
        """Persist the external route before sending or deferring the slot."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE comment_schedule
                       SET channel_id=?, post_id=?, linked_chat_id=?,
                           discussion_message_id=?,
                           route_cached_at=CASE
                               WHEN ? IS NOT NULL THEN CURRENT_TIMESTAMP
                               ELSE route_cached_at
                           END
                       WHERE id=? AND task_id=? AND status='running'""",
                    (
                        int(channel_id),
                        int(post_id) if post_id is not None else None,
                        int(linked_chat_id) if linked_chat_id is not None else None,
                        (
                            int(discussion_message_id)
                            if discussion_message_id is not None
                            else None
                        ),
                        int(post_id) if post_id is not None else None,
                        int(slot_id),
                        int(task_id),
                    ),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to bind campaign slot target: {exc}") from exc

    def get_comment_slot_route(self, slot_id, task_id=None):
        """Return a route persisted by an earlier attempt of the same slot."""
        try:
            with self.get_connection() as conn:
                if task_id is None:
                    row = conn.execute(
                        """SELECT channel_id, post_id, linked_chat_id,
                                  discussion_message_id, route_cached_at
                           FROM comment_schedule
                           WHERE id=?""",
                        (int(slot_id),),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """SELECT channel_id, post_id, linked_chat_id,
                                  discussion_message_id, route_cached_at
                           FROM comment_schedule
                           WHERE id=? AND task_id=?""",
                        (int(slot_id), int(task_id)),
                    ).fetchone()
                return dict(row) if row else None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read campaign slot route: {exc}") from exc

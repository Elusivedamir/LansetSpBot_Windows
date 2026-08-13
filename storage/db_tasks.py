from __future__ import annotations

import json
import logging
import math
from storage.sqlcipher_driver import dbapi as sqlite3
import time

from core.boot_clock import current_boot_identity, steady_time
from core.campaign_schedule import (
    from_db_time,
    to_db_time,
    utc_now,
)
from core.performance import log_if_slow
from core.redaction import sanitize_text

from storage.db_common import DatabaseError, json_dumps_safe

log = logging.getLogger(__name__)


class TaskRepositoryMixin:
    @staticmethod
    def _validated_payload_json(payload):
        if isinstance(payload, str):
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise DatabaseError(f"Invalid task payload JSON: {exc}") from exc
            payload = decoded
        if not isinstance(payload, dict):
            raise DatabaseError("Task payload must be a JSON object")
        return json_dumps_safe(payload)

    def insert_task(self, task_type, payload, max_retries=3):
        """Insert new task."""
        try:
            if not isinstance(task_type, str) or not task_type.strip():
                raise DatabaseError("Task type must be a non-empty string")
            try:
                max_retries = int(max_retries)
            except (TypeError, ValueError, OverflowError) as exc:
                raise DatabaseError(
                    "max_retries must be a non-negative integer"
                ) from exc
            if max_retries < 0:
                raise DatabaseError("max_retries must be a non-negative integer")
            payload_json = self._validated_payload_json(payload)
            decoded = self._decode_task_payload(payload)
            try:
                account_id = max(0, int(decoded.get("account_id") or 0))
            except (TypeError, ValueError, OverflowError) as exc:
                raise DatabaseError("Task account_id must be an integer") from exc
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """INSERT INTO tasks(
                           account_id, type, payload, status, max_retries,
                           created_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                    (
                        account_id,
                        task_type.strip(),
                        payload_json,
                        "pending",
                        max_retries,
                    ),
                )
            log.info("Task created: %s", cursor.lastrowid)
            return cursor.lastrowid
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to insert task: {e}") from e

    def create_or_get_link_task(self, *, account_id, payload, max_retries=3):
        """Atomically enforce one active link task per Telegram account."""
        owner_account_id = max(0, int(account_id or 0))
        if owner_account_id <= 0:
            raise DatabaseError("link task requires a positive account_id")
        payload = dict(payload or {})
        payload["account_id"] = owner_account_id
        payload_json = self._validated_payload_json(payload)
        conn = None
        try:
            conn = sqlite3.connect(
                str(self.path),
                timeout=self.sqlite_timeout_seconds,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT id, account_id, type, payload, status, progress,
                          status_text, error, retry_count, max_retries,
                          defer_count, first_deferred_at, last_deferred_at,
                          not_before, created_at, updated_at
                   FROM tasks
                   WHERE account_id=? AND type='link_channels'
                     AND status IN ('pending','running','processing','paused')
                   ORDER BY id ASC LIMIT 1""",
                (owner_account_id,),
            ).fetchone()
            if row is not None:
                conn.execute("COMMIT")
                return dict(row), False
            cursor = conn.execute(
                """INSERT INTO tasks(
                       account_id, type, payload, status, max_retries,
                       created_at, updated_at)
                   VALUES(?, 'link_channels', ?, 'pending', ?,
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (owner_account_id, payload_json, max(0, int(max_retries))),
            )
            task_id = int(cursor.lastrowid or 0)
            row = conn.execute(
                """SELECT id, account_id, type, payload, status, progress,
                          status_text, error, retry_count, max_retries,
                          defer_count, first_deferred_at, last_deferred_at,
                          not_before, created_at, updated_at
                   FROM tasks WHERE id=?""",
                (task_id,),
            ).fetchone()
            conn.execute("COMMIT")
            return dict(row), True
        except Exception as exc:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            if isinstance(exc, DatabaseError):
                raise
            raise DatabaseError(f"Failed to create or reuse link task: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()

    def get_active_link_task(self, *, account_id=None):
        """Return the sole active link task for one account."""
        try:
            if account_id is None:
                account_id = self.get_setting("telegram.account_id", 0)
            owner_account_id = max(0, int(account_id or 0))
            if owner_account_id <= 0:
                return None
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT id, account_id, type, payload, status, progress,
                              status_text, error, retry_count, max_retries,
                              defer_count, first_deferred_at, last_deferred_at,
                              not_before, created_at, updated_at
                       FROM tasks
                       WHERE account_id=? AND type='link_channels'
                         AND status IN ('pending','running','processing','paused')
                       ORDER BY progress DESC, id ASC LIMIT 1""",
                    (owner_account_id,),
                ).fetchone()
                return dict(row) if row else None
        except Exception as exc:
            if isinstance(exc, DatabaseError):
                raise
            raise DatabaseError(f"Failed to read active link task: {exc}") from exc

    @staticmethod
    def _decode_task_payload(payload):
        if isinstance(payload, dict):
            return dict(payload)
        try:
            decoded = json.loads(payload or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = {}
        return dict(decoded) if isinstance(decoded, dict) else {}

    def request_link_task_pause(
        self,
        task_id,
        reason="Остановлено пользователем; прогресс сохранён",
    ):
        """Request a safe pause without shortening an active FloodWait.

        A deferred task keeps its ``not_before`` deadline.  The worker claims it
        only after Telegram's wait plus the safety buffer has elapsed, then moves
        it to ``paused`` before issuing another RPC.  A running task receives a
        persisted flag and pauses at the next checkpoint after the current RPC or
        configured inter-request delay finishes.
        """

        conn = None
        try:
            conn = sqlite3.connect(
                str(self.path),
                timeout=self.sqlite_timeout_seconds,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT id, payload, status, not_before
                   FROM tasks
                   WHERE id=? AND type='link_channels'""",
                (int(task_id),),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return "missing"

            status = str(row["status"] or "")
            if status == "paused":
                conn.execute("COMMIT")
                return "paused"
            if status not in {"pending", "running", "processing"}:
                conn.execute("COMMIT")
                return "finished"

            payload = self._decode_task_payload(row["payload"])
            payload["_link_pause_requested"] = True
            payload_json = self._validated_payload_json(payload)
            waiting = conn.execute(
                """SELECT CASE
                           WHEN ? IS NOT NULL AND ?>CURRENT_TIMESTAMP THEN 1
                           ELSE 0
                       END""",
                (row["not_before"], row["not_before"]),
            ).fetchone()[0]

            if status == "pending" and not bool(waiting):
                payload.pop("_link_pause_requested", None)
                conn.execute(
                    """UPDATE tasks
                       SET payload=?, status='paused', status_text=NULL, error=?,
                           not_before=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND type='link_channels' AND status='pending'""",
                    (
                        self._validated_payload_json(payload),
                        str(reason),
                        int(task_id),
                    ),
                )
                result = "paused"
            else:
                status_text = (
                    "Стоп принят: сначала завершится FloodWait и защитная задержка"
                    if status == "pending"
                    else "Стоп принят: пауза после текущего запроса или задержки"
                )
                cursor = conn.execute(
                    """UPDATE tasks
                       SET payload=?, status_text=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND type='link_channels'
                         AND status IN ('pending','running','processing')""",
                    (payload_json, status_text, int(task_id)),
                )
                if cursor.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return "missing"
                result = "waiting" if status == "pending" else "requested"

            conn.execute("COMMIT")
            return result
        except DatabaseError:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        except Exception as exc:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise DatabaseError(f"Failed to request link task pause: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()

    def pause_pending_link_task(
        self,
        task_id,
        reason="Остановлено пользователем; прогресс сохранён",
    ):
        try:
            with self.get_connection() as conn:
                # Reserve the write transaction before reading payload so a
                # concurrent checkpoint cannot overwrite the pause decision.
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT payload FROM tasks WHERE id=? AND type='link_channels'",
                    (int(task_id),),
                ).fetchone()
                if row is None:
                    return False
                payload = self._decode_task_payload(row["payload"])
                payload.pop("_link_pause_requested", None)
                cursor = conn.execute(
                    """UPDATE tasks
                       SET payload=?, status='paused', status_text=NULL, error=?,
                           not_before=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND type='link_channels' AND status='pending'""",
                    (
                        self._validated_payload_json(payload),
                        str(reason),
                        int(task_id),
                    ),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to pause pending link task: {exc}") from exc

    def pause_running_link_task(
        self,
        task_id,
        reason="Остановлено пользователем; прогресс сохранён",
    ):
        try:
            with self.get_connection() as conn:
                # Serialize payload read-modify-write with checkpoint updates.
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT payload FROM tasks WHERE id=? AND type='link_channels'",
                    (int(task_id),),
                ).fetchone()
                if row is None:
                    return False
                payload = self._decode_task_payload(row["payload"])
                payload.pop("_link_pause_requested", None)
                cursor = conn.execute(
                    """UPDATE tasks
                       SET payload=?, status='paused', status_text=NULL, error=?,
                           not_before=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND type='link_channels'
                         AND status IN ('running','processing')""",
                    (
                        self._validated_payload_json(payload),
                        str(reason),
                        int(task_id),
                    ),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to pause running link task: {exc}") from exc

    def resume_link_task(self, task_id):
        try:
            with self.get_connection() as conn:
                # Prevent a stale concurrent payload write from restoring the
                # removed pause marker after this resume operation commits.
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT payload FROM tasks WHERE id=? AND type='link_channels'",
                    (int(task_id),),
                ).fetchone()
                if row is None:
                    return False
                payload = self._decode_task_payload(row["payload"])
                payload.pop("_link_pause_requested", None)
                cursor = conn.execute(
                    """UPDATE tasks
                       SET payload=?, status='pending', status_text=NULL, error=NULL,
                           not_before=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND type='link_channels' AND status='paused'""",
                    (self._validated_payload_json(payload), int(task_id)),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to resume link task: {exc}") from exc

    def set_account_rpc_cooldown(
        self,
        *,
        account_id,
        retry_at,
        code="flood_wait_deferred",
        source_task_id=None,
        wait_seconds=None,
    ):
        """Extend (never shorten) one boot-aware persisted RPC embargo."""

        owner_account_id = max(0, int(account_id or 0))
        if owner_account_id <= 0:
            raise DatabaseError("RPC cooldown requires a positive account_id")
        retry_value = to_db_time(retry_at)
        if wait_seconds is None:
            parsed_retry = from_db_time(retry_value)
            remaining = (
                (parsed_retry - utc_now()).total_seconds()
                if parsed_retry is not None
                else 1.0
            )
            wait_value = max(1, int(math.ceil(remaining)))
        else:
            wait_value = max(1, int(math.ceil(float(wait_seconds))))
        boot_id = current_boot_identity()
        steady_deadline = steady_time() + float(wait_value)
        try:
            with self.get_connection() as conn:
                conn.execute(
                    """INSERT INTO account_rpc_cooldowns(
                           account_id, next_allowed_at, code, source_task_id, updated_at,
                           boot_id, steady_deadline, fallback_wait_seconds)
                       VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
                       ON CONFLICT(account_id) DO UPDATE SET
                           next_allowed_at=CASE
                               WHEN excluded.next_allowed_at>account_rpc_cooldowns.next_allowed_at
                                   THEN excluded.next_allowed_at
                               ELSE account_rpc_cooldowns.next_allowed_at
                           END,
                           code=CASE
                               WHEN excluded.next_allowed_at>=account_rpc_cooldowns.next_allowed_at
                                   THEN excluded.code
                               ELSE account_rpc_cooldowns.code
                           END,
                           source_task_id=CASE
                               WHEN excluded.next_allowed_at>=account_rpc_cooldowns.next_allowed_at
                                   THEN excluded.source_task_id
                               ELSE account_rpc_cooldowns.source_task_id
                           END,
                           boot_id=CASE
                               WHEN excluded.next_allowed_at>account_rpc_cooldowns.next_allowed_at
                                   THEN excluded.boot_id
                               WHEN excluded.next_allowed_at=account_rpc_cooldowns.next_allowed_at
                                    AND excluded.boot_id<>account_rpc_cooldowns.boot_id
                                   THEN excluded.boot_id
                               ELSE account_rpc_cooldowns.boot_id
                           END,
                           steady_deadline=CASE
                               WHEN excluded.next_allowed_at>account_rpc_cooldowns.next_allowed_at
                                   THEN excluded.steady_deadline
                               WHEN excluded.next_allowed_at=account_rpc_cooldowns.next_allowed_at
                                    AND excluded.boot_id=account_rpc_cooldowns.boot_id
                                   THEN MAX(
                                       COALESCE(account_rpc_cooldowns.steady_deadline, 0),
                                       excluded.steady_deadline
                                   )
                               WHEN excluded.next_allowed_at=account_rpc_cooldowns.next_allowed_at
                                   THEN excluded.steady_deadline
                               ELSE account_rpc_cooldowns.steady_deadline
                           END,
                           fallback_wait_seconds=CASE
                               WHEN excluded.next_allowed_at>account_rpc_cooldowns.next_allowed_at
                                   THEN excluded.fallback_wait_seconds
                               WHEN excluded.next_allowed_at=account_rpc_cooldowns.next_allowed_at
                                   THEN MAX(
                                       account_rpc_cooldowns.fallback_wait_seconds,
                                       excluded.fallback_wait_seconds
                                   )
                               ELSE account_rpc_cooldowns.fallback_wait_seconds
                           END,
                           updated_at=CASE
                               WHEN excluded.next_allowed_at>=account_rpc_cooldowns.next_allowed_at
                                   THEN CURRENT_TIMESTAMP
                               ELSE account_rpc_cooldowns.updated_at
                           END""",
                    (
                        owner_account_id,
                        retry_value,
                        str(code or "flood_wait_deferred"),
                        int(source_task_id) if source_task_id is not None else None,
                        boot_id,
                        steady_deadline,
                        wait_value,
                    ),
                )
                row = conn.execute(
                    """SELECT account_id, next_allowed_at, code, source_task_id, updated_at,
                              boot_id, steady_deadline, fallback_wait_seconds
                       FROM account_rpc_cooldowns WHERE account_id=?""",
                    (owner_account_id,),
                ).fetchone()
                return dict(row) if row else {}
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to persist account RPC cooldown: {exc}"
            ) from exc

    def get_account_rpc_cooldown(self, *, account_id):
        try:
            owner_account_id = max(0, int(account_id or 0))
            if owner_account_id <= 0:
                return {}
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT account_id, next_allowed_at, code, source_task_id, updated_at,
                              boot_id, steady_deadline, fallback_wait_seconds,
                              CASE WHEN next_allowed_at>CURRENT_TIMESTAMP THEN 1 ELSE 0 END AS active,
                              CASE
                                  WHEN next_allowed_at>CURRENT_TIMESTAMP THEN
                                      MAX(1, CAST(
                                          (julianday(next_allowed_at) - julianday(CURRENT_TIMESTAMP))
                                          * 86400.0 AS INTEGER
                                      ) + 1)
                                  ELSE 0
                              END AS remaining_seconds
                       FROM account_rpc_cooldowns WHERE account_id=?""",
                    (owner_account_id,),
                ).fetchone()
                return dict(row) if row else {}
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read account RPC cooldown: {exc}") from exc

    def reanchor_account_rpc_cooldown(
        self,
        *,
        account_id,
        expected_next_allowed_at: str,
        boot_id: str,
        steady_deadline: float,
        fallback_wait_seconds: int,
    ):
        """Conservatively re-anchor a persisted cooldown after boot/clock change."""

        owner_account_id = max(0, int(account_id or 0))
        if owner_account_id <= 0:
            return {}
        wait_value = max(1, int(fallback_wait_seconds or 1))
        deadline_value = float(steady_deadline)
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """UPDATE account_rpc_cooldowns
                       SET steady_deadline=CASE
                               WHEN boot_id=? THEN MAX(COALESCE(steady_deadline, 0), ?)
                               ELSE ?
                           END,
                           boot_id=?,
                           fallback_wait_seconds=MAX(fallback_wait_seconds, ?)
                       WHERE account_id=? AND next_allowed_at=?""",
                    (
                        str(boot_id),
                        deadline_value,
                        deadline_value,
                        str(boot_id),
                        wait_value,
                        owner_account_id,
                        str(expected_next_allowed_at),
                    ),
                )
                row = conn.execute(
                    """SELECT account_id, next_allowed_at, code, source_task_id, updated_at,
                              boot_id, steady_deadline, fallback_wait_seconds
                       FROM account_rpc_cooldowns WHERE account_id=?""",
                    (owner_account_id,),
                ).fetchone()
                return dict(row) if row else {}
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to re-anchor account RPC cooldown: {exc}"
            ) from exc

    def clear_elapsed_account_rpc_cooldown(
        self,
        *,
        account_id,
        expected_next_allowed_at: str,
        boot_id: str,
        observed_steady_time: float,
    ) -> bool:
        """Delete only the exact cooldown proven elapsed on the current boot."""

        owner_account_id = max(0, int(account_id or 0))
        if owner_account_id <= 0:
            return False
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """DELETE FROM account_rpc_cooldowns
                       WHERE account_id=? AND next_allowed_at=? AND boot_id=?
                         AND steady_deadline IS NOT NULL AND steady_deadline<=?""",
                    (
                        owner_account_id,
                        str(expected_next_allowed_at),
                        str(boot_id),
                        float(observed_steady_time),
                    ),
                )
                return int(cursor.rowcount or 0) == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to clear elapsed RPC cooldown: {exc}") from exc

    def postpone_running_task_for_account_cooldown(
        self, task_id, *, retry_at, code="account_flood_wait"
    ):
        """Return a claimed task to pending without consuming its defer budget."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE tasks
                       SET status='pending', status_text=NULL, error=?, not_before=?,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('running','processing')""",
                    (str(code), to_db_time(retry_at), int(task_id)),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to postpone task for account cooldown: {exc}"
            ) from exc

    def get_pending_tasks(self, limit=10):
        """Get pending tasks without claiming them. Intended for UI/read-only views."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """SELECT id, type, payload, status, progress, retry_count, max_retries, defer_count
                       FROM tasks
                       WHERE status = 'pending'
                         AND (not_before IS NULL OR not_before <= CURRENT_TIMESTAMP)
                       ORDER BY created_at ASC, id ASC
                       LIMIT ?""",
                    (limit,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to get pending tasks: {e}") from e

    def claim_next_pending_task(self, excluded_account_ids=()):
        """Claim the oldest due task not owned by a currently active account."""
        excluded = sorted({max(0, int(value)) for value in excluded_account_ids})
        conn = None
        started = time.monotonic()
        outcome = "error"
        try:
            conn = sqlite3.connect(
                str(self.path),
                timeout=self.sqlite_timeout_seconds,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("BEGIN IMMEDIATE")
            while True:
                exclusion_sql = ""
                parameters = []
                if excluded:
                    placeholders = ",".join("?" for _ in excluded)
                    exclusion_sql = f" AND account_id NOT IN ({placeholders})"
                    parameters.extend(excluded)
                row = conn.execute(
                    f"""SELECT id, account_id, type, payload, status, progress,
                               retry_count, max_retries, defer_count
                        FROM tasks
                        WHERE status='pending'
                          AND (not_before IS NULL OR not_before<=CURRENT_TIMESTAMP)
                          {exclusion_sql}
                        ORDER BY created_at ASC, id ASC
                        LIMIT 1""",
                    tuple(parameters),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    outcome = "empty"
                    return None
                claimed = dict(row)
                try:
                    decoded = json.loads(claimed.get("payload") or "{}")
                    if not isinstance(decoded, dict):
                        raise ValueError("task payload must be a JSON object")
                    column_account = max(0, int(claimed.get("account_id") or 0))
                    payload_account = max(0, int(decoded.get("account_id") or 0))
                    if column_account and payload_account and column_account != payload_account:
                        raise ValueError("task account column does not match payload")
                    account_id = column_account or payload_account

                    # Legacy rows may store account_id only inside payload.
                    # Normalize the indexed column before applying active-account
                    # exclusion, otherwise two tasks for one account may run at once.
                    if account_id != column_account:
                        normalized = conn.execute(
                            """UPDATE tasks
                               SET account_id=?, updated_at=CURRENT_TIMESTAMP
                               WHERE id=? AND status='pending'""",
                            (account_id, claimed["id"]),
                        )
                        if normalized.rowcount != 1:
                            continue
                        claimed["account_id"] = account_id

                    if account_id in excluded:
                        # The normalized row is excluded by the next SELECT.
                        continue
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    conn.execute(
                        """UPDATE tasks SET status='failed', error=?,
                                  updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status='pending'""",
                        (f"Invalid task payload: {exc}", claimed["id"]),
                    )
                    continue
                cursor = conn.execute(
                    """UPDATE tasks
                       SET account_id=?, status='running', not_before=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='pending'
                         AND (not_before IS NULL OR not_before<=CURRENT_TIMESTAMP)""",
                    (account_id, claimed["id"]),
                )
                if cursor.rowcount != 1:
                    continue
                claimed["account_id"] = account_id
                claimed["payload"] = decoded
                claimed["status"] = "running"
                conn.execute("COMMIT")
                outcome = "claimed"
                return claimed
        except sqlite3.Error as exc:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise DatabaseError(f"Failed to atomically claim task: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()
            log_if_slow(
                log,
                "sqlite_claim_next_task",
                started,
                threshold_seconds=0.5,
                outcome=outcome,
            )

    def seconds_until_next_pending_task(self) -> float | None:
        """Return seconds until the next pending task is due, or ``None``.

        This read-only helper lets the persistent queue worker sleep on an event
        instead of opening a writer transaction every 250 ms. A task without a
        ``not_before`` deadline is due immediately and therefore returns ``0``.
        """

        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT COUNT(*) AS pending_count,
                              MAX(0.0, MIN(
                                  (julianday(COALESCE(not_before, CURRENT_TIMESTAMP))
                                   - julianday(CURRENT_TIMESTAMP)) * 86400.0
                              )) AS seconds_until_due
                       FROM tasks
                       WHERE status='pending'"""
                ).fetchone()
            if row is None or int(row["pending_count"] or 0) <= 0:
                return None
            return max(0.0, float(row["seconds_until_due"] or 0.0))
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to read next pending task deadline: {exc}"
            ) from exc

    def get_tasks(self, status=None, limit=50):
        """Get tasks by status."""
        try:
            with self.get_connection() as conn:
                if status:
                    cursor = conn.execute(
                        """SELECT id, account_id, type, payload, status, progress, status_text, error, retry_count, defer_count, not_before,
                           created_at, updated_at FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?""",
                        (status, limit),
                    )
                else:
                    cursor = conn.execute(
                        """SELECT id, account_id, type, payload, status, progress, status_text, error, retry_count, defer_count, not_before,
                           created_at, updated_at FROM tasks ORDER BY created_at DESC LIMIT ?""",
                        (limit,),
                    )
                return [dict(row) for row in cursor.fetchall()]
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to get tasks: {e}") from e

    def reset_running_tasks(self):
        """Recover interrupted tasks without risking duplicate external sends.

        Idempotent tasks can be retried automatically. Message/comment tasks may
        already have produced an external side effect before a crash, so they are
        moved to manual-review failure instead of pending.
        """
        safe_types = (
            "noop",
            "sync_channels",
            "sync_new_channels",
            "sync_saved_dialogs",
            "link_channels",
            "import",
            "parse_audience",
        )
        try:
            with self.get_connection() as conn:
                requeued = conn.execute(
                    """UPDATE tasks
                        SET status='pending', error='Recovered after unclean shutdown',
                            status_text=NULL, updated_at=CURRENT_TIMESTAMP
                        WHERE status IN ('running','processing')
                          AND type IN (?,?,?,?,?,?,?)""",
                    safe_types,
                ).rowcount
                uncertain = conn.execute(
                    """UPDATE tasks
                        SET status='failed',
                            error='Interrupted with uncertain external result; review before retry',
                            status_text=NULL, updated_at=CURRENT_TIMESTAMP
                        WHERE status IN ('running','processing')
                          AND type NOT IN (?,?,?,?,?,?,?)""",
                    safe_types,
                ).rowcount
            if requeued:
                log.warning("Recovered %s idempotent interrupted task(s)", requeued)
            if uncertain:
                log.error("Marked %s side-effect task(s) for manual review", uncertain)
            return requeued + uncertain
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to recover running tasks: {e}") from e

    def requeue_task(self, task_id, error=None):
        """Return a claimed task to pending without consuming a retry attempt."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE tasks
                       SET status = 'pending', status_text = NULL, error = ?, not_before = NULL, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status IN ('running', 'processing')""",
                    (sanitize_text(error), task_id),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to requeue task: {e}") from e

    def defer_task(
        self,
        task_id,
        *,
        retry_at,
        error=None,
        max_defer_count=10,
        max_defer_age_seconds=86_400,
    ):
        """Persist a bounded future retry without blocking the worker.

        Returns ``"deferred"``, ``"exhausted"`` or ``"not_running"``.
        A separate defer budget prevents permanent FloodWait/SlowMode loops from
        keeping one task alive forever without consuming normal retry_count.
        """
        try:
            limit = max(1, int(max_defer_count))
            max_age = max(60, int(max_defer_age_seconds))
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """SELECT status, defer_count, first_deferred_at
                       FROM tasks WHERE id=?""",
                    (int(task_id),),
                ).fetchone()
                if row is None or row["status"] not in {"running", "processing"}:
                    return "not_running"

                count = max(0, int(row["defer_count"] or 0))
                age_row = conn.execute(
                    """SELECT CASE
                           WHEN ? IS NULL THEN 0
                           ELSE CAST((julianday('now') - julianday(?)) * 86400 AS INTEGER)
                       END""",
                    (row["first_deferred_at"], row["first_deferred_at"]),
                ).fetchone()
                age_seconds = max(0, int(age_row[0] or 0))
                try:
                    requested_wait = max(1, int((retry_at - utc_now()).total_seconds()))
                except Exception:
                    requested_wait = 0
                if count >= limit or age_seconds >= max_age:
                    message = (
                        f"defer_limit_exceeded: count={count}; "
                        f"age_seconds={age_seconds}; "
                        f"last_retry_after_seconds={requested_wait}; "
                        f"last_error={sanitize_text(error or '')}"
                    )
                    conn.execute(
                        """UPDATE tasks
                           SET status='failed', status_text=NULL, error=?, not_before=NULL,
                               last_deferred_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status IN ('running','processing')""",
                        (sanitize_text(message), int(task_id)),
                    )
                    return "exhausted"

                cursor = conn.execute(
                    """UPDATE tasks
                       SET status='pending', status_text=NULL, error=?, not_before=?,
                           defer_count=defer_count+1,
                           first_deferred_at=COALESCE(first_deferred_at, CURRENT_TIMESTAMP),
                           last_deferred_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('running','processing')""",
                    (sanitize_text(error), to_db_time(retry_at), int(task_id)),
                )
                return "deferred" if cursor.rowcount == 1 else "not_running"
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to defer task: {exc}") from exc

    def get_task_defer_diagnostics(self, task_id) -> dict[str, object]:
        """Return safe diagnostics for a task stopped by the defer budget."""

        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT id, type, defer_count, first_deferred_at,
                              last_deferred_at, error,
                              CASE WHEN first_deferred_at IS NULL THEN 0
                                   ELSE MAX(0, CAST((julianday('now') -
                                        julianday(first_deferred_at)) * 86400 AS INTEGER))
                              END AS elapsed_since_first_defer_seconds
                       FROM tasks WHERE id=?""",
                    (int(task_id),),
                ).fetchone()
                return dict(row) if row else {}
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read defer diagnostics: {exc}") from exc

    def has_account_change_blocking_tasks(self) -> bool:
        """Return whether unfinished queue work still belongs to the account.

        The persistent worker thread itself is intentionally ignored. Pending,
        running, processing and safely paused tasks remain account-bound and must
        be completed, cancelled or resumed before the Telegram session changes.
        """

        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT 1 FROM tasks
                       WHERE status IN ('pending','running','processing','paused')
                       LIMIT 1"""
                ).fetchone()
                return row is not None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to check account-bound unfinished tasks: {exc}"
            ) from exc

    def has_due_pending_tasks(self) -> bool:
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT 1 FROM tasks
                       WHERE status='pending'
                         AND (not_before IS NULL OR not_before<=CURRENT_TIMESTAMP)
                       LIMIT 1"""
                ).fetchone()
                return row is not None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to check due tasks: {exc}") from exc

    def set_processing(self, task_id):
        """Atomically mark a pending task as running."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE tasks SET status = 'running', status_text = NULL,
                              updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status = 'pending'""",
                    (task_id,),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to set task processing: {e}") from e

    def set_done(self, task_id):
        """Mark a currently running task as completed."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE tasks SET status = 'completed', progress = 100, status_text = NULL,
                              error = NULL, not_before = NULL, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status = 'running'""",
                    (task_id,),
                )
                changed = cursor.rowcount == 1
            if changed:
                log.info("Task %s completed", task_id)
            return changed
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to set task done: {e}") from e

    def set_failed(self, task_id, error, retry=False):
        """Fail a running task, optionally returning it to the pending queue."""
        safe_error = sanitize_text(error)
        try:
            with self.get_connection() as conn:
                if retry:
                    cursor = conn.execute(
                        """UPDATE tasks SET status = 'pending', status_text = NULL, error = ?, retry_count = retry_count + 1,
                           not_before = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'running'""",
                        (safe_error, task_id),
                    )
                else:
                    cursor = conn.execute(
                        """UPDATE tasks SET status = 'failed', status_text = NULL, error = ?, not_before = NULL, updated_at = CURRENT_TIMESTAMP
                           WHERE id = ? AND status = 'running'""",
                        (safe_error, task_id),
                    )
                changed = cursor.rowcount == 1
            if changed:
                if retry:
                    log.warning(
                        "Task %s failed and was requeued: %s", task_id, safe_error
                    )
                else:
                    log.error("Task %s failed permanently: %s", task_id, safe_error)
            return changed
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to set task failed: {e}") from e

    def fail_due_pending_task(self, task_id, error):
        """Fail one due task that no worker ever claimed.

        This is used only after the queue thread has stopped with a lifecycle or
        loop error. The ``pending`` predicate proves that no handler started, so
        recording the exact worker failure cannot duplicate an external action.
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE tasks
                       SET status='failed', status_text=NULL, error=?, not_before=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='pending'
                         AND (not_before IS NULL OR not_before<=CURRENT_TIMESTAMP)""",
                    (sanitize_text(error), int(task_id)),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to record unavailable queue for task {task_id}: {exc}"
            ) from exc

    def cancel_pending_mutating_tasks(self, reason):
        """Cancel queued Telegram mutations after a global account restriction.

        Read-only synchronization tasks remain available so the user can inspect
        local state, but joins and sends must not resume automatically.
        """
        mutating_types = (
            "auto_comment",
            "auto_comment_slot",
            "direct_message",
            "comment",
            "join_saved_slot",
        )
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE tasks
                       SET status='cancelled', status_text=NULL, error=?,
                           not_before=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE status IN ('pending','paused')
                         AND type IN (?,?,?,?,?)""",
                    (sanitize_text(reason), *mutating_types),
                )
                return int(cursor.rowcount or 0)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to cancel pending mutating tasks: {exc}"
            ) from exc

    def cancel_task(self, task_id):
        """Cancel a task that has not started; running side effects cannot be safely revoked."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE tasks
                       SET status='cancelled', status_text=NULL, error='Cancelled by user',
                           not_before=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('pending', 'paused')""",
                    (task_id,),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to cancel task: {e}") from e

    def cancel_running_audience_task(self, task_id, reason="Cancelled by user"):
        """Finalize a parser cancellation after its read-only handler stops."""

        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE tasks
                       SET status='cancelled', status_text=NULL, error=?,
                           not_before=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND type='parse_audience'
                         AND status IN ('running', 'processing')""",
                    (sanitize_text(reason), int(task_id)),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to cancel running audience task {task_id}: {exc}"
            ) from exc

    def update_task_progress(self, task_id, progress):
        """Update progress only while a task is running."""
        try:
            value = max(0, min(100, int(progress)))
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE tasks SET progress = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status = 'running'""",
                    (value, task_id),
                )
                return cursor.rowcount == 1
        except (TypeError, ValueError) as exc:
            raise DatabaseError(f"Invalid progress value: {progress!r}") from exc
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to update task progress: {e}") from e

    def update_task_checkpoint(self, task_id, payload, progress):
        """Atomically persist resumable task state together with its progress.

        Long read-only Telegram jobs may be deferred by FloodWait.  Persisting
        the cursor in the task payload prevents a resumed job from repeating all
        previously completed Telegram requests.
        """

        try:
            value = max(0, min(100, int(progress)))
            checkpoint_payload = self._decode_task_payload(payload)
            with self.get_connection() as conn:
                # Acquire the write reservation before reading the current
                # payload. This keeps a concurrent pause request from being
                # lost between the SELECT and UPDATE below.
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT payload FROM tasks WHERE id=? AND status='running'",
                    (int(task_id),),
                ).fetchone()
                if current is not None:
                    current_payload = self._decode_task_payload(current["payload"])
                    if current_payload.get("_link_pause_requested"):
                        checkpoint_payload["_link_pause_requested"] = True
                payload_json = self._validated_payload_json(checkpoint_payload)
                cursor = conn.execute(
                    """UPDATE tasks
                       SET payload=?, progress=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='running'""",
                    (payload_json, value, int(task_id)),
                )
                return cursor.rowcount == 1
        except (TypeError, ValueError) as exc:
            raise DatabaseError(
                f"Invalid task checkpoint progress: {progress!r}"
            ) from exc
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to update task checkpoint: {exc}") from exc

    def update_task_status_text(self, task_id, text):
        """Expose a transient human-readable worker state to the GUI."""
        try:
            value = str(text or "").strip() or None
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE tasks
                       SET status_text = ?, not_before = NULL,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status = 'running'""",
                    (value, int(task_id)),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to update task status text: {exc}") from exc

    def update_task_runtime_wait(self, task_id, text, *, wait_seconds):
        """Publish a running task's durable wait deadline for live UI countdowns."""
        try:
            value = str(text or "").strip() or None
            seconds = max(1, int(wait_seconds))
            modifier = f"+{seconds} seconds"
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE tasks
                       SET status_text = ?, not_before = datetime('now', ?),
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status = 'running'""",
                    (value, modifier, int(task_id)),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to update task runtime wait: {exc}") from exc

    def get_task(self, task_id):
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT id, account_id, type, payload, status, progress, status_text, error, retry_count,
                              max_retries, defer_count, first_deferred_at, last_deferred_at,
                              not_before, created_at, updated_at
                       FROM tasks WHERE id=?""",
                    (int(task_id),),
                ).fetchone()
                return dict(row) if row else None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read task: {exc}") from exc

    def has_due_pending_task_type(self, task_type):
        """Return True only when a pending task of this type is executable now."""
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT 1 FROM tasks
                       WHERE status='pending' AND type=?
                         AND (not_before IS NULL OR not_before<=CURRENT_TIMESTAMP)
                       LIMIT 1""",
                    (str(task_type),),
                ).fetchone()
                return row is not None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to check due pending task type: {exc}"
            ) from exc

    def has_pending_task_type(self, task_type):
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM tasks WHERE status='pending' AND type=? LIMIT 1",
                    (str(task_type),),
                ).fetchone()
                return row is not None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to check pending task type: {exc}") from exc

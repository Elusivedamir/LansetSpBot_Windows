from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from core.redaction import sanitize_json, sanitize_log_text, sanitize_text
from storage.db_common import DatabaseError, resolve_account_id


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


_MUTATING_TASK_TYPES = frozenset(
    {
        "auto_comment",
        "auto_comment_slot",
        "direct_message",
        "comment",
        "join_saved_slot",
    }
)


class AccountRestrictionRepositoryMixin(_MixinHost):
    """Persistent, account-scoped Telegram restriction state."""

    def get_account_restriction(self, account_id=None) -> dict[str, Any]:
        owner_account_id = resolve_account_id(self, account_id)
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT account_id, active, code, message, detected_at,
                              checked_at, details_json, updated_at
                       FROM account_restrictions WHERE account_id=?""",
                    (owner_account_id,),
                ).fetchone()
            if row is None:
                return {
                    "active": False,
                    "stored_active": False,
                    "account_id": owner_account_id,
                    "code": "",
                    "message": "",
                    "detected_at": "",
                    "checked_at": "",
                    "details": {},
                    "updated_at": "",
                }
            result = dict(row)
            try:
                details = json.loads(str(result.pop("details_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {}
            result["active"] = bool(result.get("active"))
            result["stored_active"] = result["active"]
            result["details"] = details if isinstance(details, dict) else {}
            return result
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to read account restriction for {owner_account_id}: {exc}"
            ) from exc

    @staticmethod
    def _task_belongs_to_account(conn, row, account_id: int) -> bool:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            try:
                if int(payload.get("account_id") or 0) == account_id:
                    return True
            except (TypeError, ValueError, OverflowError):
                pass

        task_id = int(row["id"])
        task_type = str(row["type"])
        if task_type == "auto_comment_slot":
            linked = conn.execute(
                """SELECT 1 FROM comment_schedule s
                   JOIN comment_campaigns c ON c.id=s.campaign_id
                   WHERE s.task_id=? AND c.account_id=? LIMIT 1""",
                (task_id, account_id),
            ).fetchone()
            return linked is not None
        if task_type == "join_saved_slot":
            linked = conn.execute(
                """SELECT 1 FROM join_schedule s
                   JOIN join_campaigns c ON c.id=s.campaign_id
                   WHERE s.task_id=? AND c.account_id=? LIMIT 1""",
                (task_id, account_id),
            ).fetchone()
            return linked is not None
        return False

    def activate_account_restriction_atomic(
        self,
        *,
        account_id,
        code: str,
        message: str,
        details_json: str,
        detected_at: str,
        reason: str,
    ) -> dict[str, Any]:
        """Activate RESTRICTED and stop only that account in one transaction.

        The restriction row, campaign state transitions, remaining schedule
        cancellations, queued mutating-task cancellations and activity log either
        all commit together or all roll back. A crash cannot leave a stopped
        campaign without its safety flag, or a safety flag without queue cleanup.
        """

        owner_account_id = resolve_account_id(self, account_id)
        safe_code = sanitize_text(code)
        safe_message = sanitize_text(message)
        safe_details_json = sanitize_json(details_json or {})
        safe_reason = sanitize_text(reason)
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """INSERT INTO account_restrictions(
                           account_id, active, code, message, detected_at,
                           checked_at, details_json, updated_at)
                       VALUES(?, 1, ?, ?, ?, NULL, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(account_id) DO UPDATE SET
                           active=1,
                           code=excluded.code,
                           message=excluded.message,
                           detected_at=excluded.detected_at,
                           checked_at=NULL,
                           details_json=excluded.details_json,
                           updated_at=CURRENT_TIMESTAMP""",
                    (
                        owner_account_id,
                        safe_code,
                        safe_message,
                        str(detected_at),
                        safe_details_json,
                    ),
                )

                comment_rows = conn.execute(
                    """SELECT id FROM comment_campaigns
                       WHERE account_id=?
                         AND status IN ('running','paused','network_wait','cycle_wait')
                       ORDER BY id""",
                    (owner_account_id,),
                ).fetchall()
                comment_ids = [int(row["id"]) for row in comment_rows]
                if comment_ids:
                    placeholders = ",".join("?" for _ in comment_ids)
                    conn.execute(
                        f"""UPDATE comment_schedule
                            SET status='cancelled', result=?, executed_at=CURRENT_TIMESTAMP
                            WHERE campaign_id IN ({placeholders})
                              AND status IN ('pending','queued')""",
                        (safe_reason, *comment_ids),
                    )
                    conn.execute(
                        f"""UPDATE comment_campaigns
                            SET status='stopped', pause_reason=?, network_retry_at=NULL,
                                updated_at=CURRENT_TIMESTAMP
                            WHERE id IN ({placeholders})""",
                        (safe_reason, *comment_ids),
                    )

                join_rows = conn.execute(
                    """SELECT id FROM join_campaigns
                       WHERE account_id=?
                         AND status IN ('running','paused','network_wait')
                       ORDER BY id""",
                    (owner_account_id,),
                ).fetchall()
                join_ids = [int(row["id"]) for row in join_rows]
                if join_ids:
                    placeholders = ",".join("?" for _ in join_ids)
                    conn.execute(
                        f"""UPDATE join_schedule
                            SET status='cancelled', result=?, executed_at=CURRENT_TIMESTAMP
                            WHERE campaign_id IN ({placeholders})
                              AND status IN ('pending','queued')""",
                        (safe_reason, *join_ids),
                    )
                    conn.execute(
                        f"""UPDATE join_campaigns
                            SET status='stopped', pause_reason=?, network_retry_at=NULL,
                                updated_at=CURRENT_TIMESTAMP
                            WHERE id IN ({placeholders})""",
                        (safe_reason, *join_ids),
                    )

                task_rows = conn.execute(
                    """SELECT id, type, payload FROM tasks
                       WHERE status IN ('pending','paused')
                         AND type IN ('auto_comment','auto_comment_slot',
                                      'direct_message','comment','join_saved_slot')
                       ORDER BY id"""
                ).fetchall()
                task_ids = [
                    int(row["id"])
                    for row in task_rows
                    if self._task_belongs_to_account(conn, row, owner_account_id)
                ]
                if task_ids:
                    conn.executemany(
                        """UPDATE tasks
                           SET status='cancelled', status_text=NULL, error=?,
                               not_before=NULL, updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status IN ('pending','paused')""",
                        [(safe_reason, task_id) for task_id in task_ids],
                    )

                log_message = (
                    f"{safe_reason} account_id={owner_account_id}; code={safe_code}; "
                    f"message={safe_message}; comment_campaigns={comment_ids or '—'}; "
                    f"join_campaigns={join_ids or '—'}; "
                    f"cancelled_tasks={len(task_ids)}"
                )
                log_message = sanitize_log_text(log_message)
                retained_before = self._get_persistent_log_retained_bytes(conn)
                conn.execute(
                    """INSERT INTO logs(account_id, level, message, created_at)
                       VALUES(?, 'ERROR', ?, CURRENT_TIMESTAMP)""",
                    (owner_account_id, log_message),
                )
                note_size = getattr(self, "_note_persistent_log_insert", None)
                if callable(note_size):
                    note_size(
                        conn,
                        "ERROR",
                        log_message,
                        retained_before=retained_before,
                    )
                # Keep the activity-log budget inside this same
                # outer transaction instead of opening another connection.
                prune = getattr(self, "_prune_persistent_logs_to_budget", None)
                if callable(prune):
                    prune(conn)

            return {
                "active": True,
                "account_id": owner_account_id,
                "code": safe_code,
                "message": safe_message,
                "detected_at": str(detected_at),
                "comment_campaign_ids": comment_ids,
                "join_campaign_ids": join_ids,
                "comment_campaign_id": comment_ids[-1] if comment_ids else None,
                "join_campaign_id": join_ids[-1] if join_ids else None,
                "cancelled_tasks": len(task_ids),
            }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to atomically restrict account {owner_account_id}: {exc}"
            ) from exc

    def clear_account_restriction(self, *, account_id, checked_at: str) -> bool:
        owner_account_id = resolve_account_id(self, account_id)
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """UPDATE account_restrictions
                       SET active=0, checked_at=?, updated_at=CURRENT_TIMESTAMP
                       WHERE account_id=? AND active=1""",
                    (str(checked_at), owner_account_id),
                )
                log_message = (
                    "Пользователь подтвердил через @SpamBot, что ограничение "
                    f"аккаунта {owner_account_id} снято. Автоматические "
                    "отправки для этого аккаунта снова разрешены."
                )
                log_message = sanitize_log_text(log_message)
                retained_before = self._get_persistent_log_retained_bytes(conn)
                conn.execute(
                    """INSERT INTO logs(account_id, level, message, created_at)
                       VALUES(?, 'INFO', ?, CURRENT_TIMESTAMP)""",
                    (owner_account_id, log_message),
                )
                note_size = getattr(self, "_note_persistent_log_insert", None)
                if callable(note_size):
                    note_size(
                        conn,
                        "INFO",
                        log_message,
                        retained_before=retained_before,
                    )
                prune = getattr(self, "_prune_persistent_logs_to_budget", None)
                if callable(prune):
                    prune(conn)
                return bool(cursor.rowcount == 1)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to clear account restriction for {owner_account_id}: {exc}"
            ) from exc

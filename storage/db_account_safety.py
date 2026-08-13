from __future__ import annotations

import json
import math
from datetime import timedelta
from typing import Any, TYPE_CHECKING

from core.account_safety import (
    AdaptiveSafetyLevel,
    CONSERVATIVE_RECOVERY_SECONDS,
    FLOOD_ESCALATION_WINDOW_SECONDS,
    POST_PROTECTIVE_CONSERVATIVE_SECONDS,
    SAFETY_PACED_TASK_TYPES,
    SOFT_PROTECTIVE_RECOVERY_SECONDS,
)
from core.campaign_schedule import from_db_time, to_db_time, utc_now
from storage.db_common import DatabaseError, resolve_account_id

if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:
    class _MixinHost:
        pass


class AccountSafetyRepositoryMixin(_MixinHost):
    """Persistent adaptive safety; hard account blocks remain authoritative elsewhere."""

    @staticmethod
    def _wait_seconds(value: Any) -> int:
        parsed = from_db_time(value)
        if parsed is None:
            return 0
        return max(0, int(math.ceil((parsed - utc_now()).total_seconds())))

    @staticmethod
    def _ensure(conn, account_id: int) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO account_safety_state(
                   account_id, adaptive_level, flood_count_window, updated_at)
               VALUES(?, 'normal', 0, CURRENT_TIMESTAMP)""",
            (int(account_id),),
        )

    @staticmethod
    def _event(conn, *, account_id: int, event_type: str, from_level: str,
               to_level: str, code: str = "", details: dict[str, Any] | None = None) -> None:
        conn.execute(
            """INSERT INTO account_safety_events(
                   account_id, event_type, from_level, to_level, code, details_json, occurred_at)
               VALUES(?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (int(account_id), str(event_type), str(from_level), str(to_level),
             str(code or ""), json.dumps(details or {}, ensure_ascii=False, sort_keys=True)),
        )

    @staticmethod
    def _hard_block(conn, account_id: int) -> tuple[bool, str, str]:
        account = conn.execute(
            """SELECT authorized, runtime_state, stopped FROM telegram_accounts
               WHERE telegram_account_id=?""", (int(account_id),)
        ).fetchone()
        if account is None:
            return True, "account_missing", "Telegram-аккаунт не найден"
        state = str(account["runtime_state"] or "").strip().lower()
        if not bool(account["authorized"]) or state == "authorization_required":
            return True, "authorization_required", "Требуется повторная авторизация Telegram"
        restriction = conn.execute(
            """SELECT code, message FROM account_restrictions
               WHERE account_id=? AND active=1""", (int(account_id),)
        ).fetchone()
        if restriction is not None or state == "restricted":
            code = str(restriction["code"] or "account_restricted") if restriction else "account_restricted"
            message = str(restriction["message"] or "") if restriction else ""
            return True, code, message or "Telegram ограничил активность аккаунта"
        if bool(account["stopped"]) or state in {"stopping", "stopped"}:
            return True, "account_stopped", "Аккаунт остановлен"
        return False, "", ""

    @staticmethod
    def _cooldown_active(conn, account_id: int) -> bool:
        return conn.execute(
            """SELECT 1 FROM account_rpc_cooldowns WHERE account_id=?
               AND next_allowed_at>CURRENT_TIMESTAMP LIMIT 1""", (int(account_id),)
        ).fetchone() is not None

    def _recover_locked(self, conn, account_id: int) -> None:
        self._ensure(conn, account_id)
        if self._hard_block(conn, account_id)[0] or self._cooldown_active(conn, account_id):
            return
        row = conn.execute(
            "SELECT adaptive_level, recovery_not_before FROM account_safety_state WHERE account_id=?",
            (int(account_id),),
        ).fetchone()
        if row is None:
            return
        recovery = from_db_time(row["recovery_not_before"])
        if recovery is None or recovery > utc_now():
            return
        current = str(row["adaptive_level"] or "normal")
        if current == AdaptiveSafetyLevel.SOFT_PROTECTIVE:
            next_recovery = utc_now() + timedelta(seconds=POST_PROTECTIVE_CONSERVATIVE_SECONDS)
            conn.execute(
                """UPDATE account_safety_state SET adaptive_level='conservative',
                   recovery_not_before=?, next_task_at=NULL, next_mutation_at=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE account_id=?""",
                (to_db_time(next_recovery), int(account_id)),
            )
            self._event(conn, account_id=account_id, event_type="auto_recovery",
                        from_level=current, to_level="conservative", code="quiet_period_elapsed")
        elif current == AdaptiveSafetyLevel.CONSERVATIVE:
            conn.execute(
                """UPDATE account_safety_state SET adaptive_level='normal',
                   recovery_not_before=NULL, next_task_at=NULL,
                   last_reserved_task_id=NULL, last_reserved_task_at=NULL,
                   next_mutation_at=NULL, last_mutation_request=NULL,
                   flood_count_window=0, updated_at=CURRENT_TIMESTAMP WHERE account_id=?""",
                (int(account_id),),
            )
            self._event(conn, account_id=account_id, event_type="auto_recovery",
                        from_level=current, to_level="normal", code="quiet_period_elapsed")

    def get_account_safety_state(self, account_id=None) -> dict[str, Any]:
        owner = resolve_account_id(self, account_id)
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                self._recover_locked(conn, owner)
                self._ensure(conn, owner)
                row = conn.execute(
                    """SELECT account_id, adaptive_level, last_flood_at, flood_count_window,
                              recovery_not_before, next_task_at, last_reserved_task_id,
                              last_reserved_task_at, next_mutation_at, last_mutation_request,
                              updated_at FROM account_safety_state WHERE account_id=?""",
                    (owner,),
                ).fetchone()
                hard, code, reason = self._hard_block(conn, owner)
            result = dict(row) if row else {"account_id": owner, "adaptive_level": "normal"}
            adaptive = str(result.get("adaptive_level") or "normal")
            mode = "protective" if hard or adaptive == "soft_protective" else (
                "conservative" if adaptive == "conservative" else "normal"
            )
            if not code and adaptive == "soft_protective":
                code, reason = "adaptive_protective", "Повторный FloodWait: мутации временно отложены"
            elif not code and adaptive == "conservative":
                code, reason = "adaptive_conservative", "Нагрузка снижена после FloodWait"
            result.update({
                "mode": mode,
                "hard_block": bool(hard),
                "reason_code": code,
                "reason_text": reason,
                "pacing_multiplier": 0.0 if mode == "protective" else 2.0 if mode == "conservative" else 1.0,
                "recovery_remaining_seconds": self._wait_seconds(result.get("recovery_not_before")),
            })
            return result
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read account safety state for {owner}: {exc}") from exc

    def record_account_flood_wait_safety(self, *, account_id, code: str,
                                         wait_seconds: int, source_task_id: int | None = None) -> dict[str, Any]:
        owner = resolve_account_id(self, account_id)
        wait = max(1, int(wait_seconds or 0))
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                self._ensure(conn, owner)
                row = conn.execute(
                    "SELECT adaptive_level, last_flood_at, flood_count_window FROM account_safety_state WHERE account_id=?",
                    (owner,),
                ).fetchone()
                current = str((row["adaptive_level"] if row else None) or "normal")
                last_flood = from_db_time(row["last_flood_at"] if row else None)
                in_window = bool(last_flood and (utc_now() - last_flood).total_seconds() <= FLOOD_ESCALATION_WINDOW_SECONDS)
                count = (int(row["flood_count_window"] or 0) + 1) if in_window and row else 1
                if current == "soft_protective" or count >= 2:
                    target, quiet = "soft_protective", SOFT_PROTECTIVE_RECOVERY_SECONDS
                else:
                    target, quiet = "conservative", CONSERVATIVE_RECOVERY_SECONDS
                recovery = utc_now() + timedelta(seconds=max(wait, quiet))
                conn.execute(
                    """UPDATE account_safety_state SET adaptive_level=?, last_flood_at=CURRENT_TIMESTAMP,
                       flood_count_window=?, recovery_not_before=?,
                       next_task_at=CASE WHEN ?='soft_protective' THEN ? ELSE next_task_at END,
                       next_mutation_at=CASE WHEN ?='soft_protective' THEN ? ELSE next_mutation_at END,
                       updated_at=CURRENT_TIMESTAMP WHERE account_id=?""",
                    (target, count, to_db_time(recovery), target, to_db_time(recovery),
                     target, to_db_time(recovery), owner),
                )
                self._event(conn, account_id=owner, event_type="flood_wait", from_level=current,
                            to_level=target, code=str(code or "flood_wait_deferred"),
                            details={"wait_seconds": wait, "source_task_id": source_task_id,
                                     "flood_count_window": count})
            return self.get_account_safety_state(owner)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to record adaptive safety FloodWait for {owner}: {exc}") from exc

    @staticmethod
    def _base_gap(conn, *, task_id: int, task_type: str, account_id: int) -> int:
        if task_type in {"auto_comment", "auto_comment_slot"}:
            row = conn.execute(
                """SELECT c.cadence_seconds FROM comment_schedule s
                   JOIN comment_campaigns c ON c.id=s.campaign_id
                   WHERE s.task_id=? AND c.account_id=? LIMIT 1""",
                (int(task_id), int(account_id)),
            ).fetchone()
            if row:
                return max(30, min(12 * 3600, int(float(row[0] or 0))))
        if task_type == "join_saved_slot":
            row = conn.execute(
                """SELECT c.max_per_hour FROM join_schedule s
                   JOIN join_campaigns c ON c.id=s.campaign_id
                   WHERE s.task_id=? AND c.account_id=? LIMIT 1""",
                (int(task_id), int(account_id)),
            ).fetchone()
            if row:
                return max(45, min(3600, int(math.ceil(3600 / max(1, int(row[0] or 1))))))
        if task_type == "warmup_step":
            row = conn.execute(
                """SELECT pair_id, sequence_no, scheduled_at FROM warmup_steps
                   WHERE queue_task_id=? AND actor_account_id=? LIMIT 1""",
                (int(task_id), int(account_id)),
            ).fetchone()
            if row:
                nxt = conn.execute(
                    """SELECT scheduled_at FROM warmup_steps WHERE pair_id=? AND actor_account_id=?
                       AND sequence_no>? ORDER BY sequence_no ASC LIMIT 1""",
                    (int(row[0]), int(account_id), int(row[1])),
                ).fetchone()
                a, b = from_db_time(row[2]), from_db_time(nxt[0] if nxt else None)
                if a and b and b > a:
                    return max(120, min(12 * 3600, int((b - a).total_seconds())))
            return 300
        if task_type == "link_channels":
            return 90
        return 60

    def reserve_account_safety_task(self, *, account_id, task_id: int, task_type: str) -> dict[str, Any]:
        owner = resolve_account_id(self, account_id)
        task_type = str(task_type or "")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                self._recover_locked(conn, owner)
                self._ensure(conn, owner)
                hard, code, reason = self._hard_block(conn, owner)
                row = conn.execute(
                    """SELECT adaptive_level, recovery_not_before, next_task_at,
                              last_reserved_task_id, last_reserved_task_at
                       FROM account_safety_state WHERE account_id=?""", (owner,)
                ).fetchone()
                adaptive = str(row["adaptive_level"] or "normal")
                if hard:
                    return {"action": "block", "mode": "protective", "reason_code": code, "reason_text": reason}
                if adaptive == "soft_protective":
                    return {"action": "postpone", "mode": "protective",
                            "wait_seconds": max(30, self._wait_seconds(row["recovery_not_before"])),
                            "reason_code": "adaptive_protective",
                            "reason_text": "Повторный FloodWait: задача отложена"}
                if adaptive != "conservative" or task_type not in SAFETY_PACED_TASK_TYPES:
                    return {"action": "allow", "mode": adaptive, "wait_seconds": 0}
                last_task = int(row["last_reserved_task_id"] or 0)
                last_at = from_db_time(row["last_reserved_task_at"])
                if last_task == int(task_id) and last_at and (utc_now() - last_at).total_seconds() <= 10:
                    return {"action": "allow", "mode": "conservative", "idempotent": True, "wait_seconds": 0}
                remaining = self._wait_seconds(row["next_task_at"])
                if remaining > 0:
                    return {"action": "postpone", "mode": "conservative", "wait_seconds": remaining,
                            "reason_code": "adaptive_conservative_pacing",
                            "reason_text": "Conservative: увеличен интервал между задачами"}
                base = self._base_gap(conn, task_id=int(task_id), task_type=task_type, account_id=owner)
                effective = max(60, base * 2)
                conn.execute(
                    """UPDATE account_safety_state SET next_task_at=?, last_reserved_task_id=?,
                       last_reserved_task_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE account_id=?""",
                    (to_db_time(utc_now() + timedelta(seconds=effective)), int(task_id), owner),
                )
                return {"action": "allow", "mode": "conservative", "wait_seconds": 0,
                        "base_gap_seconds": base, "effective_gap_seconds": effective}
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to reserve adaptive safety task for {owner}: {exc}") from exc

    def reserve_account_safety_request(self, *, account_id, request_name: str,
                                       spacing_seconds: int) -> dict[str, Any]:
        owner = resolve_account_id(self, account_id)
        spacing = max(1, int(spacing_seconds or 1))
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                self._recover_locked(conn, owner)
                self._ensure(conn, owner)
                hard, code, reason = self._hard_block(conn, owner)
                row = conn.execute(
                    "SELECT adaptive_level, recovery_not_before, next_mutation_at FROM account_safety_state WHERE account_id=?",
                    (owner,),
                ).fetchone()
                adaptive = str(row["adaptive_level"] or "normal")
                if hard:
                    return {"action": "block", "mode": "protective", "reason_code": code, "reason_text": reason}
                if adaptive == "soft_protective":
                    return {"action": "postpone", "mode": "protective",
                            "wait_seconds": max(30, self._wait_seconds(row["recovery_not_before"])),
                            "reason_code": "adaptive_protective",
                            "reason_text": "Повторный FloodWait: Telegram-мутация отложена"}
                if adaptive != "conservative":
                    return {"action": "allow", "mode": "normal", "wait_seconds": 0}
                remaining = self._wait_seconds(row["next_mutation_at"])
                if remaining > 0:
                    return {"action": "wait", "mode": "conservative", "wait_seconds": remaining}
                conn.execute(
                    """UPDATE account_safety_state SET next_mutation_at=?, last_mutation_request=?,
                       updated_at=CURRENT_TIMESTAMP WHERE account_id=?""",
                    (to_db_time(utc_now() + timedelta(seconds=spacing)), str(request_name), owner),
                )
                return {"action": "allow", "mode": "conservative", "wait_seconds": 0,
                        "spacing_seconds": spacing}
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to reserve adaptive safety RPC for {owner}: {exc}") from exc

    def get_account_safety_events(self, *, account_id=None, limit: int = 100) -> list[dict[str, Any]]:
        owner = None if account_id is None else resolve_account_id(self, account_id)
        maximum = max(1, min(1000, int(limit)))
        try:
            with self.get_connection() as conn:
                if owner is None:
                    rows = conn.execute(
                        """SELECT id, account_id, event_type, from_level, to_level, code,
                                  details_json, occurred_at FROM account_safety_events
                           ORDER BY occurred_at DESC, id DESC LIMIT ?""", (maximum,)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT id, account_id, event_type, from_level, to_level, code,
                                  details_json, occurred_at FROM account_safety_events
                           WHERE account_id=? ORDER BY occurred_at DESC, id DESC LIMIT ?""",
                        (owner, maximum),
                    ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    details = json.loads(str(item.pop("details_json") or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    details = {}
                item["details"] = details if isinstance(details, dict) else {}
                result.append(item)
            return result
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read safety events: {exc}") from exc

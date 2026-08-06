from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from storage.db_common import DatabaseError

_CHECKPOINT_KEY = "_audience_checkpoint"
_RESUMABLE_STATUSES = {"pending", "paused", "failed"}


def _decode_payload(database: Any, raw: object) -> dict[str, Any]:
    decoder = getattr(database, "_decode_task_payload", None)
    if callable(decoder):
        value = decoder(raw)
        return dict(value) if isinstance(value, Mapping) else {}
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _encode_payload(database: Any, payload: Mapping[str, Any]) -> str:
    validator = getattr(database, "_validated_payload_json", None)
    if not callable(validator):
        raise DatabaseError("Database does not support safe task payload updates")
    return str(validator(dict(payload)))


def load_audience_checkpoint(payload: object) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    checkpoint = payload.get(_CHECKPOINT_KEY)
    return dict(checkpoint) if isinstance(checkpoint, Mapping) else {}


def persist_audience_checkpoint(
    database: Any,
    task_id: int,
    checkpoint: Mapping[str, Any],
) -> bool:
    get_connection = getattr(database, "get_connection", None)
    if not callable(get_connection):
        return False
    try:
        with get_connection() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload, type FROM tasks WHERE id=?",
                (int(task_id),),
            ).fetchone()
            if row is None or str(row["type"] or "") != "parse_audience":
                return False
            payload = _decode_payload(database, row["payload"])
            payload[_CHECKPOINT_KEY] = dict(checkpoint)
            cursor = conn.execute(
                """UPDATE tasks
                   SET payload=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND type='parse_audience'""",
                (_encode_payload(database, payload), int(task_id)),
            )
            return cursor.rowcount == 1
    except DatabaseError:
        raise
    except Exception as exc:
        raise DatabaseError(f"Failed to persist audience checkpoint: {exc}") from exc


def clear_audience_checkpoint(database: Any, task_id: int) -> bool:
    get_connection = getattr(database, "get_connection", None)
    if not callable(get_connection):
        return False
    try:
        with get_connection() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload, type FROM tasks WHERE id=?",
                (int(task_id),),
            ).fetchone()
            if row is None or str(row["type"] or "") != "parse_audience":
                return False
            payload = _decode_payload(database, row["payload"])
            payload.pop(_CHECKPOINT_KEY, None)
            cursor = conn.execute(
                """UPDATE tasks
                   SET payload=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND type='parse_audience'""",
                (_encode_payload(database, payload), int(task_id)),
            )
            return cursor.rowcount == 1
    except DatabaseError:
        raise
    except Exception as exc:
        raise DatabaseError(f"Failed to clear audience checkpoint: {exc}") from exc


def pause_audience_task_for_recovery(database: Any, task_id: int, reason: str) -> bool:
    get_connection = getattr(database, "get_connection", None)
    if not callable(get_connection):
        return False
    try:
        with get_connection() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload, type, status FROM tasks WHERE id=?",
                (int(task_id),),
            ).fetchone()
            if row is None or str(row["type"] or "") != "parse_audience":
                return False
            payload = _decode_payload(database, row["payload"])
            checkpoint = load_audience_checkpoint(payload)
            if not checkpoint:
                return False
            checkpoint["awaiting_user_choice"] = True
            checkpoint["resume_approved"] = False
            payload[_CHECKPOINT_KEY] = checkpoint
            cursor = conn.execute(
                """UPDATE tasks
                   SET payload=?, status='paused', status_text=?, error=?,
                       not_before=NULL, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND type='parse_audience'
                     AND status IN ('pending','running','processing','failed','paused')""",
                (
                    _encode_payload(database, payload),
                    "Найдена незавершённая выгрузка: выберите действие",
                    str(reason),
                    int(task_id),
                ),
            )
            return cursor.rowcount == 1
    except DatabaseError:
        raise
    except Exception as exc:
        raise DatabaseError(f"Failed to pause audience task for recovery: {exc}") from exc


def find_resumable_audience_task(database: Any, *, account_id: int) -> dict[str, Any] | None:
    owner = max(0, int(account_id or 0))
    get_connection = getattr(database, "get_connection", None)
    if owner <= 0 or not callable(get_connection):
        return None
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT id, account_id, type, payload, status, progress,
                          status_text, error, created_at, updated_at
                   FROM tasks
                   WHERE account_id=? AND type='parse_audience'
                     AND (status IN ('paused','failed')
                          OR (status='pending' AND error='Recovered after unclean shutdown'))
                   ORDER BY id DESC""",
                (owner,),
            ).fetchall()
        for row in rows:
            payload = _decode_payload(database, row["payload"])
            checkpoint = load_audience_checkpoint(payload)
            if not checkpoint:
                continue
            temp_path = Path(str(checkpoint.get("temp_path") or "")).expanduser()
            try:
                expected_size = max(0, int(checkpoint.get("file_size") or 0))
            except (TypeError, ValueError, OverflowError):
                expected_size = 0
            if not temp_path.is_file() or temp_path.stat().st_size < expected_size:
                continue
            result = dict(row)
            result["payload"] = payload
            result["checkpoint"] = checkpoint
            return result
        return None
    except Exception as exc:
        raise DatabaseError(f"Failed to find resumable audience task: {exc}") from exc


def _prepare_recovery_action(
    database: Any,
    task_id: int,
    *,
    action: str,
) -> bool:
    get_connection = getattr(database, "get_connection", None)
    if not callable(get_connection):
        return False
    temp_path: Path | None = None
    quarantined_path: Path | None = None
    committed = False
    try:
        with get_connection() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload, status, type FROM tasks WHERE id=?",
                (int(task_id),),
            ).fetchone()
            if row is None or str(row["type"] or "") != "parse_audience":
                return False
            if str(row["status"] or "") not in _RESUMABLE_STATUSES:
                return False
            payload = _decode_payload(database, row["payload"])
            checkpoint = load_audience_checkpoint(payload)
            if not checkpoint:
                return False
            raw_temp = str(checkpoint.get("temp_path") or "")
            temp_path = Path(raw_temp).expanduser() if raw_temp else None

            if action == "resume":
                checkpoint["awaiting_user_choice"] = False
                checkpoint["resume_approved"] = True
                payload[_CHECKPOINT_KEY] = checkpoint
                next_status = "pending"
                reset_progress = 0
                status_text = "Продолжение сохранённого парсинга"
                error = None
            elif action in {"restart", "discard"}:
                # Hide the partial file before making the task runnable. This
                # prevents the queue from claiming a restarted task while stale
                # bytes are still visible. Restore it if the SQL update fails.
                if temp_path is not None and temp_path.is_file():
                    quarantined_path = temp_path.with_name(
                        f".{temp_path.name}.recovery-{int(task_id)}"
                    )
                    quarantined_path.unlink(missing_ok=True)
                    os.replace(temp_path, quarantined_path)
                payload.pop(_CHECKPOINT_KEY, None)
                next_status = "pending" if action == "restart" else "cancelled"
                reset_progress = 1 if action == "restart" else 0
                status_text = (
                    "Парсинг будет начат заново"
                    if action == "restart"
                    else "Незавершённая выгрузка удалена"
                )
                error = (
                    None
                    if action == "restart"
                    else "Незавершённая выгрузка удалена пользователем"
                )
            else:
                raise ValueError(f"Unknown recovery action: {action}")

            cursor = conn.execute(
                """UPDATE tasks
                   SET payload=?, status=?, progress=CASE WHEN ?=1 THEN 0 ELSE progress END,
                       status_text=?, error=?, not_before=NULL,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND type='parse_audience'
                     AND status IN ('pending','paused','failed')""",
                (
                    _encode_payload(database, payload),
                    next_status,
                    reset_progress,
                    status_text,
                    error,
                    int(task_id),
                ),
            )
            if cursor.rowcount != 1:
                raise DatabaseError("Audience recovery state changed concurrently")
        committed = True
        if quarantined_path is not None:
            quarantined_path.unlink(missing_ok=True)
        return True
    except DatabaseError:
        if not committed and quarantined_path is not None and quarantined_path.exists():
            if temp_path is not None:
                os.replace(quarantined_path, temp_path)
        raise
    except Exception as exc:
        if not committed and quarantined_path is not None and quarantined_path.exists():
            if temp_path is not None:
                os.replace(quarantined_path, temp_path)
        raise DatabaseError(f"Failed to apply audience recovery action: {exc}") from exc


def resume_audience_task(database: Any, task_id: int) -> bool:
    return _prepare_recovery_action(database, task_id, action="resume")


def restart_audience_task(database: Any, task_id: int) -> bool:
    return _prepare_recovery_action(database, task_id, action="restart")


def discard_audience_task(database: Any, task_id: int) -> bool:
    return _prepare_recovery_action(database, task_id, action="discard")

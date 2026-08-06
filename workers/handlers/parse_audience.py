from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from services.audience_parser import (
    classify_audience_user,
    normalize_audience_filters,
    validate_audience_task_payload,
)
from storage.audience_checkpoint import (
    clear_audience_checkpoint,
    load_audience_checkpoint,
    pause_audience_task_for_recovery,
    persist_audience_checkpoint,
)

_COUNTER_KEYS = (
    "scanned",
    "saved",
    "missing_username",
    "deleted",
    "bot",
    "duplicate",
    "administrator",
    "scam_fake",
    "inactive",
)


def _normalized_counters(value: object) -> dict[str, int]:
    raw = dict(value) if isinstance(value, Mapping) else {}
    result: dict[str, int] = {}
    for key in _COUNTER_KEYS:
        try:
            result[key] = max(0, int(raw.get(key) or 0))
        except (TypeError, ValueError, OverflowError):
            result[key] = 0
    return result


def _load_seen_usernames(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {
        line.strip().casefold()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _durable_file_size(output: Any) -> int:
    output.flush()
    os.fsync(output.fileno())
    return max(0, int(os.fstat(output.fileno()).st_size))


def create_audience_parser_handler(
    *,
    queue_worker,
    worker_db,
    telegram,
    set_runtime: Callable[..., None],
    publish_activity: Callable[..., None],
):
    """Build an account-owned, cancellable, resumable TXT parser task."""

    async def parse_audience(task: dict[str, Any]) -> None:
        task_id = int(task["id"])
        payload = validate_audience_task_payload(task.get("payload") or {})
        account_id = int(payload.get("account_id") or task.get("account_id") or 0)
        source = dict(payload["source"])
        filters = normalize_audience_filters(payload.get("filters"))
        output_path = Path(payload["output_path"]).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_name(f".{output_path.name}.{task_id}.part")
        source_title = str(payload.get("source_title") or source.get("title") or "группа")

        checkpoint = load_audience_checkpoint(payload)
        checkpoint_matches = bool(
            checkpoint
            and int(checkpoint.get("task_id") or 0) == task_id
            and int(checkpoint.get("account_id") or 0) == account_id
            and dict(checkpoint.get("source") or {}) == source
            and str(checkpoint.get("output_path") or "") == str(output_path)
            and str(checkpoint.get("temp_path") or "") == str(temp_path)
            and normalize_audience_filters(checkpoint.get("filters")) == filters
        )

        if checkpoint and not checkpoint_matches:
            raise NonRetryableTelegramError(
                "Сохранённый checkpoint не соответствует этой задаче. "
                "Выберите «Начать заново» или удалите незавершённую выгрузку.",
                code="audience_checkpoint_mismatch",
            )

        recovered_after_crash = (
            "Recovered after unclean shutdown" in str(task.get("error") or "")
        )
        if checkpoint_matches and (
            bool(checkpoint.get("awaiting_user_choice"))
            or (recovered_after_crash and not bool(checkpoint.get("resume_approved")))
        ):
            pause_audience_task_for_recovery(
                worker_db,
                task_id,
                "Незавершённая выгрузка восстановлена после аварийного завершения",
            )
            return

        counters = _normalized_counters(checkpoint.get("counters") if checkpoint_matches else {})
        try:
            offset = max(0, int(checkpoint.get("offset") or 0)) if checkpoint_matches else 0
            durable_size = max(0, int(checkpoint.get("file_size") or 0)) if checkpoint_matches else 0
            base_elapsed = (
                max(0.0, float(checkpoint.get("elapsed_seconds") or 0.0))
                if checkpoint_matches
                else 0.0
            )
        except (TypeError, ValueError, OverflowError):
            offset = 0
            durable_size = 0
            base_elapsed = 0.0
        session_started = time.monotonic()
        session_scanned_start = counters["scanned"]

        if checkpoint_matches:
            if not temp_path.is_file() or temp_path.stat().st_size < durable_size:
                raise NonRetryableTelegramError(
                    "Незавершённый файл повреждён или удалён. Начните парсинг заново.",
                    code="audience_checkpoint_file_missing",
                )
            with temp_path.open("r+b") as partial:
                partial.truncate(durable_size)
                partial.flush()
                os.fsync(partial.fileno())
            seen = _load_seen_usernames(temp_path)
        else:
            temp_path.unlink(missing_ok=True)
            seen = set()

        def shutdown_requested() -> bool:
            checker = getattr(queue_worker, "isInterruptionRequested", None)
            return bool(callable(checker) and checker())

        def task_cancel_requested() -> bool:
            checker = getattr(queue_worker, "is_scope_cancelled", None)
            return bool(callable(checker) and checker("task", task_id))

        def elapsed_seconds() -> float:
            return base_elapsed + max(0.0, time.monotonic() - session_started)

        def elapsed_text() -> str:
            total = max(0, int(elapsed_seconds()))
            hours, remainder = divmod(total, 3600)
            minutes, seconds = divmod(remainder, 60)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        def current_speed() -> float:
            session_elapsed = max(0.001, time.monotonic() - session_started)
            processed = max(0, counters["scanned"] - session_scanned_start)
            return processed / session_elapsed

        def status_text(prefix: str) -> str:
            return (
                f"{prefix} · просмотрено: {counters['scanned']} · "
                f"сохранено: {counters['saved']} · без username: {counters['missing_username']} · "
                f"удалённых: {counters['deleted']} · ботов: {counters['bot']} · "
                f"дубликатов: {counters['duplicate']} · администраторов: "
                f"{counters['administrator']} · scam/fake: {counters['scam_fake']} · "
                f"неактивных: {counters['inactive']} · "
                f"скорость: {current_speed():.1f}/с · время: {elapsed_text()}"
            )

        def checkpoint_payload(resolved_title: str, file_size: int) -> dict[str, Any]:
            return {
                "version": 2,
                "task_id": task_id,
                "account_id": account_id,
                "source": source,
                "source_title": resolved_title,
                "output_path": str(output_path),
                "temp_path": str(temp_path),
                "filters": filters,
                "offset": offset,
                "file_size": max(0, int(file_size)),
                "counters": dict(counters),
                "elapsed_seconds": elapsed_seconds(),
                "awaiting_user_choice": False,
                "resume_approved": False,
            }

        def persist_checkpoint(resolved_title: str, file_size: int) -> None:
            persist_audience_checkpoint(
                worker_db,
                task_id,
                checkpoint_payload(resolved_title, file_size),
            )

        def finish_task_cancelled() -> None:
            temp_path.unlink(missing_ok=True)
            clear_audience_checkpoint(worker_db, task_id)
            changed = worker_db.cancel_running_audience_task(
                task_id, "Остановлено пользователем"
            )
            if not changed:
                current = worker_db.get_task(task_id) or {}
                if str(current.get("status") or "") != "cancelled":
                    raise RuntimeError(
                        f"Could not cancel running audience parser task {task_id}"
                    )
            publish_activity(
                status_text("Парсинг остановлен"),
                level="WARNING",
                category="Парсинг аудитории",
            )

        set_runtime(
            task_id,
            (
                f"Продолжение парсинга аудитории: {source_title} · "
                f"позиция: {offset} · сохранено: {counters['saved']}"
                if checkpoint_matches
                else f"Подготовка парсинга аудитории: {source_title}"
            ),
            account_id=account_id,
        )
        publish_activity(
            (
                f"Продолжен парсинг аудитории группы «{source_title}» с позиции {offset}"
                if checkpoint_matches
                else f"Начат парсинг аудитории группы «{source_title}»"
            ),
            category="Парсинг аудитории",
        )

        resolved_title = source_title
        try:
            barrier_factory = getattr(queue_worker, "create_scope_dispatch_barrier", None)
            dispatch_barrier = (
                barrier_factory(("task", task_id))
                if callable(barrier_factory)
                else None
            )
            if task_cancel_requested():
                finish_task_cancelled()
                return
            if shutdown_requested():
                raise asyncio.CancelledError

            entity = await telegram.resolve_audience_group(
                source, dispatch_barrier=dispatch_barrier
            )
            resolved_title = str(getattr(entity, "title", None) or source_title)

            mode = "a" if checkpoint_matches else "w"
            cancel_requested = False
            with temp_path.open(mode, encoding="utf-8", newline="\n") as output:
                if not checkpoint_matches:
                    persist_checkpoint(resolved_title, _durable_file_size(output))

                page_loader = getattr(telegram, "iter_audience_member_pages", None)
                if callable(page_loader):
                    page_iterator = page_loader(
                        entity,
                        offset=offset,
                        page_size=200,
                        dispatch_barrier=dispatch_barrier,
                    )
                    async for next_offset, page in page_iterator:
                        if task_cancel_requested():
                            cancel_requested = True
                            break
                        if shutdown_requested():
                            raise asyncio.CancelledError
                        for entry in list(page or []):
                            if isinstance(entry, tuple) and len(entry) == 2:
                                user, is_admin = entry
                            else:
                                user, is_admin = entry, False
                            counters["scanned"] += 1
                            reason, username = classify_audience_user(
                                user,
                                filters=filters,
                                is_administrator=bool(is_admin),
                            )
                            if reason != "accepted" or username is None:
                                counters[reason] += 1
                            else:
                                key = username.casefold()
                                if key in seen:
                                    counters["duplicate"] += 1
                                else:
                                    seen.add(key)
                                    output.write(f"{username}\n")
                                    counters["saved"] += 1
                        file_size = _durable_file_size(output)
                        offset = max(offset, int(next_offset or offset))
                        persist_checkpoint(resolved_title, file_size)
                        set_runtime(task_id, status_text(f"Парсинг «{resolved_title}»"))
                        worker_db.update_task_progress(
                            task_id, min(95, 5 + counters["scanned"] // 50)
                        )
                else:
                    resume_offset = offset
                    skipped_for_offset = 0
                    async for user in telegram.iter_audience_members(
                        entity, dispatch_barrier=dispatch_barrier
                    ):
                        if skipped_for_offset < resume_offset:
                            skipped_for_offset += 1
                            continue
                        if task_cancel_requested():
                            cancel_requested = True
                            break
                        if shutdown_requested():
                            raise asyncio.CancelledError
                        counters["scanned"] += 1
                        reason, username = classify_audience_user(user, filters=filters)
                        if reason != "accepted" or username is None:
                            counters[reason] += 1
                        else:
                            key = username.casefold()
                            if key in seen:
                                counters["duplicate"] += 1
                            else:
                                seen.add(key)
                                output.write(f"{username}\n")
                                counters["saved"] += 1
                        offset += 1
                        if offset % 100 == 0:
                            persist_checkpoint(
                                resolved_title, _durable_file_size(output)
                            )
                            set_runtime(task_id, status_text(f"Парсинг «{resolved_title}»"))
                            worker_db.update_task_progress(
                                task_id, min(95, 5 + counters["scanned"] // 50)
                            )

                if task_cancel_requested():
                    cancel_requested = True
                if not cancel_requested and shutdown_requested():
                    raise asyncio.CancelledError
                if not cancel_requested:
                    persist_checkpoint(
                        resolved_title, _durable_file_size(output)
                    )

            if cancel_requested:
                finish_task_cancelled()
                return

            if dispatch_barrier is None:
                os.replace(temp_path, output_path)
            else:
                with dispatch_barrier.dispatch(None):
                    os.replace(temp_path, output_path)

            clear_audience_checkpoint(worker_db, task_id)
            worker_db.update_task_progress(task_id, 100)
            final = status_text(f"Готово: {output_path}")
            set_runtime(task_id, final)
            publish_activity(final, category="Парсинг аудитории")
        except DeferredTelegramError as exc:
            if task_cancel_requested():
                finish_task_cancelled()
                return
            if shutdown_requested():
                pause_audience_task_for_recovery(
                    worker_db,
                    task_id,
                    "Парсинг остановлен при завершении программы",
                )
                raise asyncio.CancelledError from exc
            raise
        except asyncio.CancelledError:
            pause_audience_task_for_recovery(
                worker_db,
                task_id,
                "Парсинг остановлен при завершении программы",
            )
            raise
        except NonRetryableTelegramError:
            temp_path.unlink(missing_ok=True)
            clear_audience_checkpoint(worker_db, task_id)
            raise
        except Exception:
            # The latest committed page remains durable. On resume the file is
            # truncated to checkpoint.file_size before the next Telegram request.
            raise

    return parse_audience

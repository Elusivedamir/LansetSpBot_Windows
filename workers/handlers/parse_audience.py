from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable

from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from services.audience_parser import (
    classify_audience_user,
    validate_audience_task_payload,
)


def create_audience_parser_handler(
    *,
    queue_worker,
    worker_db,
    telegram,
    set_runtime: Callable[..., None],
    publish_activity: Callable[..., None],
):
    """Build an account-owned, cancellable TXT audience parser task."""

    async def parse_audience(task: dict[str, Any]) -> None:
        task_id = int(task["id"])
        payload = validate_audience_task_payload(task.get("payload") or {})
        account_id = int(payload.get("account_id") or task.get("account_id") or 0)
        source = dict(payload["source"])
        output_path = Path(payload["output_path"]).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_name(f".{output_path.name}.{task_id}.part")
        source_title = str(payload.get("source_title") or source.get("title") or "группа")

        counters = {
            "scanned": 0,
            "saved": 0,
            "missing_username": 0,
            "deleted": 0,
            "bot": 0,
            "duplicate": 0,
        }
        seen: set[str] = set()

        def cancelled() -> bool:
            if queue_worker.isInterruptionRequested():
                return True
            checker = getattr(queue_worker, "is_scope_cancelled", None)
            if callable(checker) and checker("task", task_id):
                return True
            return False

        def status_text(prefix: str) -> str:
            return (
                f"{prefix} · просмотрено: {counters['scanned']} · "
                f"сохранено: {counters['saved']} · без username: "
                f"{counters['missing_username']} · удалённых: "
                f"{counters['deleted']} · ботов: {counters['bot']} · "
                f"дубликатов: {counters['duplicate']}"
            )

        set_runtime(
            task_id,
            f"Подготовка парсинга аудитории: {source_title}",
            account_id=account_id,
        )
        publish_activity(
            f"Начат парсинг аудитории группы «{source_title}»",
            category="Парсинг аудитории",
        )

        try:
            entity = await telegram.resolve_audience_group(source)
            resolved_title = str(getattr(entity, "title", None) or source_title)
            barrier_factory = getattr(queue_worker, "create_scope_dispatch_barrier", None)
            dispatch_barrier = (
                barrier_factory(("task", task_id))
                if callable(barrier_factory)
                else None
            )
            cancel_requested = False
            with temp_path.open("w", encoding="utf-8", newline="\n") as output:
                async for user in telegram.iter_audience_members(
                    entity, dispatch_barrier=dispatch_barrier
                ):
                    if cancelled():
                        cancel_requested = True
                        break

                    counters["scanned"] += 1
                    reason, username = classify_audience_user(user)
                    if reason != "accepted" or username is None:
                        counters[reason] += 1
                    else:
                        dedupe_key = username.casefold()
                        if dedupe_key in seen:
                            counters["duplicate"] += 1
                        else:
                            seen.add(dedupe_key)
                            output.write(f"{username}\n")
                            counters["saved"] += 1

                    scanned = counters["scanned"]
                    if scanned % 100 == 0:
                        output.flush()
                        set_runtime(task_id, status_text(f"Парсинг «{resolved_title}»"))
                        worker_db.update_task_progress(
                            task_id, min(95, 5 + scanned // 50)
                        )

                if not cancel_requested:
                    output.flush()
                    os.fsync(output.fileno())

            if cancel_requested or cancelled():
                temp_path.unlink(missing_ok=True)
                set_runtime(task_id, status_text("Остановлено пользователем"))
                publish_activity(
                    status_text("Парсинг остановлен"),
                    level="WARNING",
                    category="Парсинг аудитории",
                )
                return
            os.replace(temp_path, output_path)
            worker_db.update_task_progress(task_id, 100)
            final = status_text(f"Готово: {output_path}")
            set_runtime(task_id, final)
            publish_activity(final, category="Парсинг аудитории")
        except DeferredTelegramError:
            temp_path.unlink(missing_ok=True)
            if cancelled():
                set_runtime(task_id, status_text("Остановлено пользователем"))
                return
            raise
        except asyncio.CancelledError:
            temp_path.unlink(missing_ok=True)
            raise
        except NonRetryableTelegramError:
            temp_path.unlink(missing_ok=True)
            raise
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    return parse_audience

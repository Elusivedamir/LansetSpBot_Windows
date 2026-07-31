"""Factory for the explicit durable comment-slot workflow."""

from __future__ import annotations

from typing import Any, Callable

from workers.comment_slot.models import CommentSlotPhase
from workers.comment_slot.runner import CommentSlotRunner


def create_comment_slot_handler(
    *,
    as_int: Callable[[Any, int], int],
    queue_worker: Any,
    config: Any,
    worker_db: Any,
    telegram: Any,
    comments: Any,
    openai_service: Any | None = None,
    set_runtime: Callable[..., None],
):
    async def auto_comment_slot(task: dict[str, Any]) -> None:
        runner = CommentSlotRunner(
            as_int=as_int,
            queue_worker=queue_worker,
            config=config,
            worker_db=worker_db,
            telegram=telegram,
            comments=comments,
            openai_service=openai_service,
            set_runtime=set_runtime,
            task=task,
        )
        await runner.run()

    return auto_comment_slot

__all__ = ["CommentSlotPhase", "create_comment_slot_handler"]

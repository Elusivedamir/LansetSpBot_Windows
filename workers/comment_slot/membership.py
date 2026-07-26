from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class MembershipOutcome:
    post_id: int
    discussion_chat_id: int | None
    discussion_message_id: int | None
    linked_chat_id: int
    joined_now: bool
    deferred_message: str | None = None


async def ensure_comment_membership(
    *,
    as_int: Callable[[Any, int], int],
    queue_worker: Any,
    config: Any,
    worker_db: Any,
    telegram: Any,
    set_runtime: Callable[[int, str], None],
    task_id: int,
    channel: dict[str, Any],
    channel_id: int,
    channel_title: str,
    post_id: int,
    linked_chat_id: int,
    discussion_chat_id: int | None,
    discussion_message_id: int | None,
    cancellation_scope: tuple[str, int],
    scope_is_cancelled: Callable[[], bool],
    suspend_cancelled_slot: Callable[[str], None],
    account_id: int | None = None,
    campaign_id: int | None = None,
) -> MembershipOutcome:
    """Compatibility helper that trusts the one-time Links preparation.

    No get_permissions/is_member RPC and no automatic join are performed here.
    The subsequent send request is the authoritative permission check.
    """
    return MembershipOutcome(
        post_id=int(post_id),
        discussion_chat_id=(
            int(discussion_chat_id) if discussion_chat_id is not None else None
        ),
        discussion_message_id=(
            int(discussion_message_id) if discussion_message_id is not None else None
        ),
        linked_chat_id=int(linked_chat_id),
        joined_now=False,
    )

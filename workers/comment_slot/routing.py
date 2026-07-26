from __future__ import annotations

import logging
from typing import Any

from core.exceptions import NonRetryableTelegramError

log = logging.getLogger(__name__)


async def refresh_comment_target(
    *,
    reason: str,
    telegram: Any,
    worker_db: Any,
    channel: dict[str, Any],
    channel_id: int,
    post_id: int | None,
    linked_chat_id: int,
    discussion_chat_id: int | None,
    discussion_message_id: int | None,
    account_id: int | None = None,
    campaign_id: int | None = None,
) -> tuple[int, int | None, int | None, int]:
    """Re-resolve the authoritative post/discussion mapping after membership changes."""

    refreshed = await telegram.get_latest_post_for_commenting(channel_id)
    status = str(getattr(refreshed, "status", "") or "")
    refreshed_message = getattr(refreshed, "message", None)
    if status != "ok" or refreshed_message is None:
        code_map = {
            "comments_disabled": "comments_disabled",
            "discussion_missing": "message_id_invalid",
            "no_post": "message_id_invalid",
        }
        raise NonRetryableTelegramError(
            f"Discussion refresh after join failed: {status or 'unknown'}",
            code=code_map.get(status, "linked_chat_inaccessible"),
            details={
                "rpc_error": "DiscussionRefreshFailed",
                "rpc_message": status or "unknown",
                "reason": reason,
            },
        )
    refreshed_post_id = int(refreshed_message.id)
    refreshed_chat_id = getattr(refreshed, "discussion_chat_id", None)
    refreshed_message_id = getattr(refreshed, "discussion_message_id", None)
    if refreshed_chat_id is not None:
        refreshed_chat_id = int(refreshed_chat_id)
    if refreshed_message_id is not None:
        refreshed_message_id = int(refreshed_message_id)
    duplicate_discussion_id = (
        int(refreshed_chat_id) if refreshed_chat_id is not None else int(linked_chat_id)
    )
    if refreshed_post_id != post_id and worker_db.has_commented(
        channel_id,
        refreshed_post_id,
        account_id=account_id,
        linked_chat_id=duplicate_discussion_id,
        campaign_id=campaign_id,
        action_type="campaign_comment",
    ):
        raise NonRetryableTelegramError(
            "The newest post was already commented while the join was in progress",
            code="comment_already_reserved",
            details={
                "rpc_error": "PostChangedDuringJoin",
                "rpc_message": (
                    f"old_post_id={post_id}; new_post_id={refreshed_post_id}"
                ),
            },
        )
    post_id = refreshed_post_id
    if refreshed_chat_id is not None:
        discussion_chat_id = refreshed_chat_id
    if refreshed_message_id is not None:
        discussion_message_id = refreshed_message_id
    if discussion_chat_id is not None and int(discussion_chat_id) != int(
        linked_chat_id
    ):
        linked_chat_id = int(discussion_chat_id)
    diagnostic = (
        f"Обсуждение обновлено ({reason}): channel_id={channel_id}; "
        f"post_id={post_id}; discussion_chat_id={discussion_chat_id or '—'}; "
        f"discussion_message_id={discussion_message_id or '—'}"
    )
    try:
        worker_db.insert_log("INFO", diagnostic, account_id=account_id)
    except Exception:  # pragma: no cover
        log.exception("Could not persist discussion refresh log")
    log.info(diagnostic)
    return post_id, discussion_chat_id, discussion_message_id, linked_chat_id

from __future__ import annotations

from typing import Any, Callable

from core.account_restriction import get_account_restriction_state
from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from workers.rpc_boundary import dispatch_barrier_kwargs


def create_manual_comment_handler(
    *,
    as_int: Callable[[Any, int], int],
    queue_worker: Any,
    config: Any,
    worker_db: Any,
    telegram: Any,
    comments: Any,
):
    async def comment(task: dict[str, Any]) -> None:
        """Send one comment only while its durable safety context stays valid."""
        payload = task.get("payload") or {}
        task_id = as_int(task.get("id"), 0)
        strict_repository = type(worker_db).__module__.startswith("storage.")
        account_id = as_int(payload.get("account_id"), 0)
        if account_id <= 0:
            get_setting = getattr(worker_db, "get_setting", None)
            if callable(get_setting):
                account_id = as_int(get_setting("telegram.account_id", 0), 0)
        channel_id = payload.get("channel_id")
        post_id = payload.get("post_id")
        text = payload.get("text")
        if (
            task_id <= 0
            or (strict_repository and account_id <= 0)
            or channel_id is None
            or post_id is None
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise NonRetryableTelegramError(
                "comment requires task/account/post/channel ids and non-empty text",
                code="invalid_payload",
            )
        channel_id = int(channel_id)
        post_id = int(post_id)
        linked_chat_id = payload.get("linked_chat_id")
        reply_to = payload.get("reply_to")

        channel_reader = getattr(worker_db, "get_channel_by_id", None)
        ban_checker = getattr(worker_db, "is_channel_locally_banned", None)

        def read_channel(peer_id: int):
            if not callable(channel_reader):
                return None
            try:
                return channel_reader(peer_id, account_id=account_id)
            except TypeError:  # pragma: no cover - compatibility test doubles
                return channel_reader(peer_id)

        def scope_cancelled(*peer_ids: int | None) -> bool:
            callback = getattr(queue_worker, "is_scope_cancelled", None)
            if not callable(callback):
                return False
            if callback("task", task_id):
                return True
            return any(
                peer_id is not None and callback("channel", int(peer_id), account_id)
                for peer_id in peer_ids
            )

        def durable_targets_allow_rpc(*peer_ids: int | None) -> bool:
            if not strict_repository:
                return True
            current_account_id = as_int(
                worker_db.get_setting("telegram.account_id", 0), 0
            )
            if current_account_id != account_id:
                return False
            if get_account_restriction_state(worker_db, account_id=account_id).get(
                "active"
            ):
                return False
            source = read_channel(channel_id)
            if not isinstance(source, dict):
                return False
            if callable(ban_checker):
                for peer_id in peer_ids:
                    if peer_id is None:
                        continue
                    if ban_checker(int(peer_id), account_id=account_id) is True:
                        return False
            return True

        def ensure_rpc_allowed(*peer_ids: int | None) -> None:
            if scope_cancelled(*peer_ids):
                raise DeferredTelegramError(
                    "Operation stopped before Telegram request dispatch",
                    code="shutdown_before_dispatch",
                    retry_after=1,
                )
            if not durable_targets_allow_rpc(*peer_ids):
                raise NonRetryableTelegramError(
                    "Comment target is missing, restricted or locally banned",
                    code="channel_locally_banned",
                )

        def create_dispatch_barrier(*peer_ids: int | None):
            factory = getattr(type(queue_worker), "create_scope_dispatch_barrier", None)
            if queue_worker is None or not callable(factory):
                return None
            scopes: list[tuple[str, int] | tuple[str, int, int]] = [
                ("task", task_id),
                ("channel", channel_id, account_id),
            ]
            for peer_id in peer_ids:
                if peer_id is not None and int(peer_id) != channel_id:
                    scopes.append(("channel", int(peer_id), account_id))
            return factory(
                queue_worker,
                *scopes,
                pre_dispatch_check=lambda: durable_targets_allow_rpc(
                    channel_id, *peer_ids
                ),
            )

        ensure_rpc_allowed(channel_id, linked_chat_id)
        channel_row = read_channel(channel_id)
        register_peer = getattr(telegram, "register_peer_reference", None)
        if callable(register_peer):
            register_peer(
                channel_id,
                access_hash=(channel_row or {}).get("access_hash"),
                peer_type=(channel_row or {}).get("peer_type"),
            )
            if linked_chat_id is not None:
                linked_row = read_channel(int(linked_chat_id))
                register_peer(
                    int(linked_chat_id),
                    access_hash=(linked_row or {}).get("access_hash"),
                    peer_type=(linked_row or {}).get("peer_type"),
                )

        ensure_rpc_allowed(channel_id, linked_chat_id)
        latest_resolver = telegram.get_latest_post_for_commenting
        route_barrier = create_dispatch_barrier(linked_chat_id)
        resolved = await latest_resolver(
            channel_id,
            **dispatch_barrier_kwargs(latest_resolver, route_barrier),
        )
        resolved_message = getattr(resolved, "message", None)
        if (
            str(getattr(resolved, "status", "") or "") == "ok"
            and resolved_message is not None
            and int(resolved_message.id) == post_id
        ):
            linked_chat_id = (
                getattr(resolved, "discussion_chat_id", None) or linked_chat_id
            )
            reply_to = getattr(resolved, "discussion_message_id", None) or reply_to
        ensure_rpc_allowed(channel_id, linked_chat_id)
        if linked_chat_id is None:
            linked_resolver = telegram.get_linked_chat
            linked_chat_id = await linked_resolver(
                channel_id,
                **dispatch_barrier_kwargs(
                    linked_resolver,
                    create_dispatch_barrier(),
                ),
            )
        if linked_chat_id is None:
            raise NonRetryableTelegramError(
                "Channel has no linked discussion chat",
                code="linked_chat_missing",
            )
        linked_chat_id = int(linked_chat_id)
        ensure_rpc_allowed(channel_id, linked_chat_id)

        dispatch_barrier = create_dispatch_barrier(linked_chat_id)
        try:
            await comments.ensure_and_send_comment(
                linked_chat_id=linked_chat_id,
                post_message_id=post_id,
                text=text.strip(),
                reply_to=reply_to,
                channel_id=channel_id,
                membership_ready=True,
                account_id=account_id,
                campaign_id=0,
                action_type="manual_comment",
                dispatch_barrier=dispatch_barrier,
            )
        except DeferredTelegramError as exc:
            if getattr(exc, "code", "") == "local_ban_before_dispatch":
                raise NonRetryableTelegramError(
                    "Comment target became locally banned before dispatch",
                    code="channel_locally_banned",
                ) from exc
            raise

    return comment

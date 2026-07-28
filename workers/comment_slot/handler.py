from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta
from typing import Any, Callable, cast

from core.campaign_schedule import utc_now
from core.openai_settings import (
    DEFAULT_OPENAI_SYSTEM_PROMPT,
    SOURCE_OPENAI,
    CommentGenerationSettings,
    normalize_comment_source,
)
from core.account_restriction import (
    RESTRICTION_CODES,
    build_account_restriction_kwargs,
    get_account_restriction_state,
)
from workers.comment_slot.finalization import finalize_comment_slot
from workers.comment_slot.models import CommentSlotPhase
from workers.rpc_boundary import dispatch_barrier_kwargs
from services.openai_comment_service import OpenAICommentError, extract_post_text

from core.exceptions import (
    DeferredTelegramError,
    NonRetryableTelegramError,
    TelegramOperationError,
)

log = logging.getLogger(__name__)


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
        """Process exactly one persistent campaign slot and one rotated channel."""
        payload = task.get("payload") or {}
        task_id = int(task["id"])
        campaign_id = as_int(payload.get("campaign_id"), 0)
        slot_id = as_int(payload.get("slot_id"), 0)
        if campaign_id <= 0 or slot_id <= 0:
            raise NonRetryableTelegramError(
                "auto_comment_slot requires campaign_id and slot_id",
                code="invalid_payload",
            )
        campaign = worker_db.get_comment_campaign(campaign_id)
        if not campaign:
            raise NonRetryableTelegramError(
                "Comment campaign no longer exists", code="campaign_missing"
            )
        # The campaign, durable queue task and currently selected Telegram
        # session must all name the same account before any entity lookup or
        # mutating request. Migration v18 backfills queued task payloads.
        campaign_account_id = as_int(campaign.get("account_id"), 0)
        payload_account_id = as_int(payload.get("account_id"), 0)
        context_validator = getattr(
            type(worker_db), "validate_comment_slot_execution_context", None
        )
        if callable(context_validator):
            context = context_validator(
                worker_db,
                task_id,
                slot_id,
                campaign_id,
                campaign_account_id,
            )
            if not context:
                raise NonRetryableTelegramError(
                    "Queue task, comment slot and campaign do not form one "
                    "account-owned execution context",
                    code="comment_context_mismatch",
                    details={
                        "task_id": task_id,
                        "slot_id": slot_id,
                        "campaign_id": campaign_id,
                        "account_id": campaign_account_id,
                    },
                )
        if get_account_restriction_state(worker_db, account_id=campaign_account_id).get(
            "active"
        ):
            worker_db.stop_comment_campaign(
                campaign_id,
                reason="Отправки заблокированы до проверки ограничения через @SpamBot",
            )
            return
        get_setting = getattr(worker_db, "get_setting", None)
        strict_account_binding = type(worker_db).__module__.startswith("storage.")
        if strict_account_binding and callable(get_setting):
            current_account_id = as_int(get_setting("telegram.account_id", 0), 0)
            if (
                campaign_account_id <= 0
                or payload_account_id <= 0
                or current_account_id <= 0
                or campaign_account_id != payload_account_id
                or campaign_account_id != current_account_id
            ):
                reason = (
                    "Кампания приостановлена: аккаунт кампании, задачи и "
                    "активной Telegram-сессии не совпадает "
                    f"(campaign={campaign_account_id}, task={payload_account_id}, "
                    f"current={current_account_id})"
                )
                worker_db.pause_comment_campaign(campaign_id, reason=reason)
                worker_db.defer_comment_slot(
                    slot_id, scheduled_at=utc_now(), result=reason
                )
                raise NonRetryableTelegramError(
                    reason,
                    code="account_state_mismatch",
                    details={
                        "campaign_account_id": campaign_account_id,
                        "task_account_id": payload_account_id,
                        "current_account_id": current_account_id,
                    },
                )
        else:  # pragma: no cover - compatibility test doubles
            campaign_account_id = campaign_account_id or payload_account_id

        campaign_status = str(campaign.get("status") or "")
        if campaign_status != "running":
            message = f"Кампания не активна: {campaign_status or 'unknown'}"
            if campaign_status == "paused":
                worker_db.defer_comment_slot(
                    slot_id, scheduled_at=utc_now(), result=message
                )
            else:
                worker_db.cancel_comment_slot(slot_id, result=message)
            return
        settings_reader = getattr(worker_db, "get_campaign_comment_settings", None)
        source_snapshot = (
            settings_reader(campaign_id) if callable(settings_reader) else {}
        ) or {}
        comment_source = normalize_comment_source(
            source_snapshot.get("comment_source")
        )
        generation_settings = CommentGenerationSettings.from_mapping(
            {
                "openai.model": source_snapshot.get("model"),
                "openai.max_words": source_snapshot.get("max_words"),
                "openai.temperature": source_snapshot.get("temperature"),
                "openai.timeout_seconds": source_snapshot.get("timeout_seconds"),
                "openai.max_generation_attempts": source_snapshot.get(
                    "max_generation_attempts"
                ),
            }
        )
        generation_prompt = str(
            source_snapshot.get("system_prompt") or DEFAULT_OPENAI_SYSTEM_PROMPT
        ).strip()
        variants = [
            str(text).strip()
            for text in campaign.get("comments", [])
            if isinstance(text, str) and text.strip()
        ]
        if not variants:
            message = "Добавьте хотя бы один комментарий"
            pause_reason = "В кампании нет вариантов комментариев"
            finalizer = getattr(type(worker_db), "finalize_comment_slot_outcome", None)
            if callable(finalizer):
                finalizer(
                    worker_db,
                    task_id,
                    slot_id,
                    status="failed",
                    result=message,
                    consume_channel=False,
                    campaign_pause_reason=pause_reason,
                )
            else:  # pragma: no cover - compatibility test doubles
                worker_db.pause_campaign_for_safety(campaign_id, pause_reason)
                worker_db.finish_comment_slot(slot_id, status="failed", result=message)
            return
        if comment_source == SOURCE_OPENAI and openai_service is None:
            message = "OpenAI-сервис недоступен в этой сборке"
            finalizer = getattr(type(worker_db), "finalize_comment_slot_outcome", None)
            if callable(finalizer):
                finalizer(
                    worker_db, task_id, slot_id, status="failed", result=message,
                    consume_channel=False, campaign_pause_reason=message,
                )
            return
        cancellation_scope = ("comment_campaign", campaign_id)

        def target_allows_rpc(*peer_ids: int | None) -> bool:
            """Re-read durable safety state immediately before Telegram RPCs."""

            latest = worker_db.get_comment_campaign(campaign_id)
            if not latest or str(latest.get("status") or "") != "running":
                return False
            if get_account_restriction_state(
                worker_db, account_id=campaign_account_id
            ).get("active"):
                return False
            ban_checker = getattr(worker_db, "is_channel_locally_banned", None)
            if callable(ban_checker):
                for peer_id in peer_ids:
                    if peer_id is None:
                        continue
                    if (
                        ban_checker(int(peer_id), account_id=campaign_account_id)
                        is True
                    ):
                        return False
            return True

        def create_dispatch_barrier(
            channel_scope_id: int | None = None,
            related_peer_id: int | None = None,
        ):
            factory = getattr(type(queue_worker), "create_scope_dispatch_barrier", None)
            if queue_worker is None or not callable(factory):
                return None
            scopes: list[tuple[str, int] | tuple[str, int, int]] = [
                cancellation_scope,
                ("task", task_id),
            ]
            if channel_scope_id is not None:
                scopes.append(("channel", int(channel_scope_id), campaign_account_id))
            if related_peer_id is not None:
                scopes.append(("channel", int(related_peer_id), campaign_account_id))
            return factory(
                queue_worker,
                *scopes,
                pre_dispatch_check=lambda: target_allows_rpc(
                    channel_scope_id, related_peer_id
                ),
            )

        def reserve_variant() -> str:
            reserver = getattr(
                type(worker_db), "reserve_comment_variant_for_slot", None
            )
            if callable(reserver):
                reservation = reserver(
                    worker_db,
                    slot_id,
                    task_id,
                    account_id=campaign_account_id,
                    variants=variants,
                )
                text = str((reservation or {}).get("text") or "").strip()
                if not text:
                    raise NonRetryableTelegramError(
                        "Comment variant reservation returned an empty text",
                        code="comment_text_missing",
                    )
                return text
            # Compatibility path for minimal repository test doubles. Production
            # always persists the bag cursor and selected slot text in SQLite.
            last_text = str(campaign.get("last_comment_text") or "")
            pool = [item for item in variants if item != last_text] or variants
            return random.choice(pool)

        def scope_is_cancelled(channel_scope_id: int | None = None) -> bool:
            callback = getattr(queue_worker, "is_scope_cancelled", None)
            if callable(callback):
                if callback(*cancellation_scope) or callback("task", task_id):
                    return True
                if channel_scope_id is not None and callback(
                    "channel", int(channel_scope_id), campaign_account_id
                ):
                    return True
            latest = worker_db.get_comment_campaign(campaign_id)
            return not latest or str(latest.get("status") or "") != "running"

        def suspend_cancelled_slot(message: str) -> None:
            latest = worker_db.get_comment_campaign(campaign_id) or {}
            if str(latest.get("status") or "") == "stopped":
                worker_db.cancel_comment_slot(slot_id, result=message)
            else:
                worker_db.defer_comment_slot(
                    slot_id, scheduled_at=utc_now(), result=message
                )

        if scope_is_cancelled():
            suspend_cancelled_slot("Кампания приостановлена до начала проверки")
            return

        route_reader = getattr(worker_db, "get_comment_slot_route", None)
        cached_route = (
            route_reader(slot_id, task_id) if callable(route_reader) else None
        )
        cached_channel_id = as_int((cached_route or {}).get("channel_id"), 0)
        if cached_channel_id != 0:
            channel_reader = getattr(worker_db, "get_channel_by_id", None)
            cached_channel = (
                channel_reader(cached_channel_id, account_id=campaign_account_id)
                if callable(channel_reader)
                else None
            )
            channels = [cached_channel] if cached_channel else []
        else:
            channels = worker_db.get_channels_for_commenting(
                1, cooldown_hours=24, account_id=campaign_account_id
            )
        if not channels:
            if not worker_db.mark_comment_slot_running(slot_id, task_id):
                raise NonRetryableTelegramError(
                    "Campaign slot is not available for execution",
                    code="campaign_slot_unavailable",
                )
            continuous = bool(campaign.get("continuous"))
            message = (
                "Все доступные каналы и группы уже обработаны за последние "
                "24 часа; ожидание следующего цикла"
                if continuous
                else "Кампания завершена: все доступные каналы и группы уже "
                "обработаны за последние 24 часа"
            )
            finalizer = getattr(type(worker_db), "finalize_comment_slot_outcome", None)
            if callable(finalizer):
                finalizer(
                    worker_db,
                    task_id,
                    slot_id,
                    status="skipped",
                    result=message,
                    consume_channel=False,
                )
            else:  # pragma: no cover - compatibility test doubles
                worker_db.add_comment_history(
                    task_id,
                    None,
                    None,
                    None,
                    message,
                    campaign_id=campaign_id,
                    slot_id=slot_id,
                )
                worker_db.finish_comment_slot(slot_id, status="skipped", result=message)
            if not continuous:
                worker_db.complete_comment_campaign(campaign_id, message)
            try:
                worker_db.insert_log("INFO", message, account_id=campaign_account_id)
            except Exception:  # pragma: no cover
                log.exception("Could not persist exhausted-channel campaign log")
            return

        channel = channels[0]
        if channel.get("local_banned_at"):
            if not worker_db.mark_comment_slot_running(slot_id, task_id):
                raise NonRetryableTelegramError(
                    "Campaign slot is not available for execution",
                    code="campaign_slot_unavailable",
                )
            message = "Пропущено: канал локально заблокирован после неизвестного Join"
            finalizer = getattr(type(worker_db), "finalize_comment_slot_outcome", None)
            if callable(finalizer):
                finalizer(
                    worker_db,
                    task_id,
                    slot_id,
                    status="skipped",
                    result=message,
                    consume_channel=False,
                )
            else:  # pragma: no cover - compatibility test doubles
                worker_db.finish_comment_slot(slot_id, status="skipped", result=message)
            try:
                worker_db.insert_log("WARNING", message, account_id=campaign_account_id)
            except Exception:  # pragma: no cover
                log.exception("Could not persist locally-banned channel skip log")
            return
        try:
            channel_id = int(channel["channel_id"])
            comment_mode = str(channel.get("comment_mode") or "channel_post")
            raw_linked_chat_id = channel.get("linked_chat_id")
            linked_chat_id = (
                channel_id
                if comment_mode == "direct_group" and raw_linked_chat_id is None
                else int(raw_linked_chat_id)
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise NonRetryableTelegramError(
                "Stored comment target is incomplete or corrupted",
                code="invalid_comment_target",
                details={"rpc_error": type(exc).__name__, "rpc_message": str(exc)},
            ) from exc
        register_peer = getattr(telegram, "register_peer_reference", None)
        if callable(register_peer):
            register_peer(
                channel_id,
                access_hash=channel.get("access_hash"),
                peer_type=channel.get("peer_type"),
            )
            if linked_chat_id is not None:
                linked_row_reader = getattr(worker_db, "get_channel_by_id", None)
                linked_row = (
                    linked_row_reader(linked_chat_id, account_id=campaign_account_id)
                    if callable(linked_row_reader)
                    else None
                )
                register_peer(
                    linked_chat_id,
                    access_hash=(linked_row or {}).get("access_hash"),
                    peer_type=(linked_row or {}).get("peer_type"),
                )
        if not worker_db.mark_comment_slot_running(slot_id, task_id):
            raise NonRetryableTelegramError(
                "Campaign slot is not available for execution",
                code="campaign_slot_unavailable",
            )
        channel_title = (
            channel.get("title") or channel.get("username") or str(channel_id)
        )
        post_id: int | None = None
        discussion_chat_id: int | None = linked_chat_id
        discussion_message_id: int | None = None
        cached_post_id = 0
        if cached_channel_id == channel_id and cached_route:
            cached_post_id = as_int(cached_route.get("post_id"), 0)
            cached_linked_id = as_int(cached_route.get("linked_chat_id"), 0)
            cached_root_id = as_int(cached_route.get("discussion_message_id"), 0)
            if cached_post_id > 0 and cached_linked_id != 0:
                discussion_chat_id = cached_linked_id
                linked_chat_id = cached_linked_id
                discussion_message_id = cached_root_id or None
        selected = None
        generated_draft_id: int | None = None
        generated_draft_status: str | None = None
        generated_post_text = ""
        final_status = "skipped"
        final_message = "Пропущено"
        sent = False
        slot_deferred = False
        consume_channel = True
        phase = CommentSlotPhase.PRECHECK
        internal_error: Exception | None = None
        campaign_pause_reason: str | None = None
        account_restriction: tuple[str, str, dict[str, Any]] | None = None
        try:
            worker_db.insert_log(
                "INFO",
                f"Проверка цели: title={channel_title}; channel_id={channel_id}; "
                f"mode={comment_mode}; linked_chat_id={linked_chat_id}",
                account_id=campaign_account_id,
            )
        except Exception:  # pragma: no cover
            log.exception("Could not persist channel-check start log")

        try:
            set_runtime(
                task_id,
                f"Суточная кампания: {channel_title}",
                account_id=campaign_account_id,
            )
            if (
                comment_mode != "direct_group"
                and cached_post_id > 0
                and linked_chat_id is not None
                and worker_db.has_commented(
                    channel_id,
                    cached_post_id,
                    account_id=campaign_account_id,
                    linked_chat_id=linked_chat_id,
                    campaign_id=campaign_id,
                    action_type="campaign_comment",
                )
            ):
                # A resumed slot already carries the exact durable route. Check
                # the local delivery history before re-reading the source post
                # and discussion from Telegram.
                post_id = cached_post_id
                final_message = "Пропущено: этот пост уже комментировали"
                return

            # Fail fast during quiet hours before post lookup, discussion lookup
            # or OpenAI generation. CommentService repeats the same guard at the
            # final send boundary in case the active window closes mid-task.
            schedule_guard = getattr(comments, "activity_schedule", None)
            if schedule_guard is not None:
                schedule_guard.require_active()

            if comment_mode == "direct_group":
                # An ordinary group is a standalone route. It never needs a
                # channel post, discussion root or reply_to value.
                if not target_allows_rpc(channel_id):
                    final_status = "skipped"
                    final_message = "Пропущено: обычный чат локально заблокирован"
                    consume_channel = False
                    return

                route_binder = getattr(worker_db, "bind_comment_slot_target", None)
                if callable(route_binder) and not route_binder(
                    slot_id,
                    task_id,
                    channel_id=channel_id,
                    post_id=None,
                    linked_chat_id=channel_id,
                    discussion_message_id=None,
                ):
                    raise NonRetryableTelegramError(
                        "Direct-group campaign route could not be persisted",
                        code="campaign_slot_unavailable",
                    )

                selected = reserve_variant()
                set_runtime(
                    task_id,
                    f"Отправка сообщения в обычный чат: {channel_title}",
                    account_id=campaign_account_id,
                )
                if scope_is_cancelled(channel_id):
                    final_message = "Кампания приостановлена перед отправкой в чат"
                    consume_channel = False
                    suspend_cancelled_slot(final_message)
                    slot_deferred = True
                    return

                send_barrier = create_dispatch_barrier(channel_id, channel_id)
                phase = CommentSlotPhase.READY_TO_SEND
                phase = CommentSlotPhase.SEND_STARTED
                await comments.send_direct_message(
                    channel_id,
                    selected,
                    task_id=task_id,
                    account_id=campaign_account_id,
                    campaign_id=campaign_id,
                    dispatch_barrier=send_barrier,
                )
                sent = True
                phase = CommentSlotPhase.SEND_CONFIRMED
                final_status = "sent"
                final_message = "Сообщение отправлено в обычный чат"
                return

            # A checkpoint or selected row may become banned after it was read.
            # Re-read SQLite before the first route-resolution RPC instead of
            # relying on the stale ``channel`` dictionary or in-memory scopes.
            if not target_allows_rpc(channel_id, linked_chat_id):
                final_status = "skipped"
                final_message = "Пропущено: цель локально заблокирована"
                consume_channel = False
                return

            result = None
            route_barrier = create_dispatch_barrier(channel_id, linked_chat_id)
            if cached_post_id > 0:
                exact_resolver = getattr(telegram, "get_post_for_commenting", None)
                if callable(exact_resolver):
                    result = await exact_resolver(
                        channel_id,
                        cached_post_id,
                        **dispatch_barrier_kwargs(exact_resolver, route_barrier),
                    )
                else:  # compatibility for deliberately small test doubles
                    latest_resolver = telegram.get_latest_post_for_commenting
                    result = await latest_resolver(
                        channel_id,
                        **dispatch_barrier_kwargs(latest_resolver, route_barrier),
                    )
            else:
                latest_resolver = telegram.get_latest_post_for_commenting
                result = await latest_resolver(
                    channel_id,
                    **dispatch_barrier_kwargs(latest_resolver, route_barrier),
                )
            if result.message is not None:
                post_id = int(result.message.id)
            resolved_discussion_chat_id = getattr(result, "discussion_chat_id", None)
            resolved_discussion_message_id = getattr(
                result, "discussion_message_id", None
            )
            if resolved_discussion_chat_id is not None:
                discussion_chat_id = int(resolved_discussion_chat_id)
            if resolved_discussion_message_id is not None:
                discussion_message_id = int(resolved_discussion_message_id)

            skip_statuses = {
                "no_post": "Пропущено: в канале нет публикаций",
                "comments_disabled": "Пропущено: у последнего поста комментарии отключены",
                "discussion_missing": "Пропущено: ветка последнего поста удалена или недоступна",
            }
            if result.status != "ok":
                final_message = skip_statuses.get(
                    result.status,
                    f"Пропущено: последний пост недоступен ({result.status})",
                )
                cache_writer = getattr(worker_db, "set_channel_negative_cache", None)
                ttl_by_status = {
                    "no_post": 60 * 60,
                    "comments_disabled": 24 * 60 * 60,
                    "discussion_missing": 24 * 60 * 60,
                }
                if callable(cache_writer):
                    cache_writer(
                        channel_id,
                        result.status,
                        ttl_seconds=ttl_by_status.get(result.status, 60 * 60),
                        account_id=campaign_account_id,
                    )
                return
            if post_id is None:
                raise NonRetryableTelegramError(
                    "Telegram returned an incomplete commentable-post result",
                    code="message_id_invalid",
                    details={
                        "rpc_error": "IncompleteDiscussionResult",
                        "rpc_message": "missing_post_message",
                    },
                )
            if discussion_chat_id is None:
                raise NonRetryableTelegramError(
                    "Telegram did not return the linked discussion chat",
                    code="linked_chat_inaccessible",
                    details={
                        "rpc_error": "IncompleteDiscussionResult",
                        "rpc_message": "missing_discussion_chat_id",
                    },
                )

            # Use the discussion returned for this exact post, but do not perform
            # an extra title lookup or persist automatic relinking. Link discovery
            # remains a one-time operation controlled by the Links page.
            if discussion_chat_id != linked_chat_id:
                linked_chat_id = int(discussion_chat_id)

            if not target_allows_rpc(channel_id, linked_chat_id):
                final_status = "skipped"
                final_message = (
                    "Пропущено: канал или чат обсуждения локально заблокирован"
                )
                consume_channel = False
                return

            if callable(register_peer):
                linked_row_reader = getattr(worker_db, "get_channel_by_id", None)
                linked_row = (
                    linked_row_reader(linked_chat_id, account_id=campaign_account_id)
                    if callable(linked_row_reader)
                    else None
                )
                register_peer(
                    linked_chat_id,
                    access_hash=(linked_row or {}).get("access_hash"),
                    peer_type=(linked_row or {}).get("peer_type"),
                )

            route_binder = getattr(worker_db, "bind_comment_slot_target", None)
            if callable(route_binder):
                if not route_binder(
                    slot_id,
                    task_id,
                    channel_id=channel_id,
                    post_id=post_id,
                    linked_chat_id=linked_chat_id,
                    discussion_message_id=discussion_message_id,
                ):
                    raise NonRetryableTelegramError(
                        "Campaign slot route could not be persisted",
                        code="campaign_slot_unavailable",
                    )
            cache_clearer = getattr(worker_db, "clear_channel_negative_cache", None)
            if callable(cache_clearer):
                cache_clearer(channel_id, account_id=campaign_account_id)

            log.info(
                "Comment target resolved: channel_id=%s post_id=%s linked_chat_id=%s "
                "discussion_chat_id=%s discussion_message_id=%s",
                channel_id,
                post_id,
                linked_chat_id,
                discussion_chat_id,
                discussion_message_id,
            )
            try:
                worker_db.insert_log(
                    "INFO",
                    "Цель комментария найдена: "
                    f"channel_id={channel_id}; post_id={post_id or '—'}; "
                    f"linked_chat_id={linked_chat_id}; "
                    f"discussion_chat_id={discussion_chat_id or '—'}; "
                    f"discussion_message_id={discussion_message_id or '—'}",
                    account_id=campaign_account_id,
                )
            except Exception:  # pragma: no cover
                log.exception("Could not persist resolved comment target log")
            if worker_db.has_commented(
                channel_id,
                post_id,
                account_id=campaign_account_id,
                linked_chat_id=linked_chat_id,
                campaign_id=campaign_id,
                action_type="campaign_comment",
            ):
                final_message = "Пропущено: этот пост уже комментировали"
                return

            if scope_is_cancelled(channel_id):
                final_message = "Кампания приостановлена до проверки участия/отправки"
                consume_channel = False
                suspend_cancelled_slot(final_message)
                slot_deferred = True
                return

            if comment_source == SOURCE_OPENAI:
                generated_post_text = extract_post_text(result.message)
                draft_reader = getattr(
                    worker_db, "get_generated_comment_draft_for_post", None
                )
                existing_draft = (
                    draft_reader(
                        account_id=campaign_account_id,
                        source_channel_id=channel_id,
                        source_post_id=post_id,
                    )
                    if callable(draft_reader)
                    else None
                )
                existing_status = str((existing_draft or {}).get("status") or "")
                if existing_status == "sent":
                    final_message = "Пропущено: OpenAI-комментарий к этому посту уже отправлен"
                    return
                if existing_status in {"sending", "uncertain"}:
                    generated_draft_id = as_int((existing_draft or {}).get("id"), 0) or None
                    generated_draft_status = existing_status
                    final_status = "uncertain"
                    consume_channel = False
                    campaign_pause_reason = (
                        "Кампания приостановлена: результат прежней отправки "
                        "OpenAI-комментария требует проверки"
                    )
                    final_message = campaign_pause_reason
                    return

                # Telegram route resolution can take long enough for the active
                # window to close after the pre-RPC guard above. Re-check before
                # drawing a variant or sending post text to OpenAI. The
                # CommentService keeps its separate final guard at Telegram send.
                if schedule_guard is not None:
                    schedule_guard.require_active()

                # One variant is drawn from the account's shuffled bag for every
                # send, exactly as in prepared mode, and handed to the model as
                # the meaning to preserve. Rotation without repeats therefore
                # still holds in OpenAI mode.
                reference_comment = reserve_variant()
                set_runtime(
                    task_id,
                    f"OpenAI генерирует комментарий: {channel_title}",
                    account_id=campaign_account_id,
                )
                try:
                    worker_db.insert_log(
                        "INFO",
                        "OpenAI generation_started: "
                        f"campaign_id={campaign_id}; channel_id={channel_id}; "
                        f"post_id={post_id}; input_length={len(generated_post_text)}; "
                        f"reference_length={len(reference_comment)}",
                        account_id=campaign_account_id,
                    )
                except Exception:  # pragma: no cover
                    log.exception("Could not persist OpenAI generation start log")

                try:
                    # openai_service is guaranteed here: the handler returns
                    # early above when the OpenAI source has no service.
                    generator = cast(Any, openai_service)
                    generated = await generator.generate_comment(
                        generated_post_text,
                        generation_prompt,
                        generation_settings,
                        reference_comment,
                    )
                except OpenAICommentError as exc:
                    code = str(getattr(exc, "code", "openai_error") or "openai_error")
                    saver = getattr(worker_db, "upsert_generated_comment_draft", None)
                    if callable(saver):
                        row = saver(
                            account_id=campaign_account_id,
                            campaign_id=campaign_id,
                            source_channel_id=channel_id,
                            source_post_id=post_id,
                            linked_chat_id=linked_chat_id,
                            discussion_message_id=discussion_message_id,
                            post_text=generated_post_text,
                            generated_text=None,
                            status="generation_failed",
                            model=generation_settings.model,
                            error_code=code,
                            error_message=str(exc),
                        )
                        generated_draft_id = as_int((row or {}).get("id"), 0) or None
                        generated_draft_status = "generation_failed"
                    try:
                        worker_db.insert_log(
                            "WARNING",
                            "OpenAI generation_failed: "
                            f"campaign_id={campaign_id}; channel_id={channel_id}; "
                            f"post_id={post_id}; code={code}",
                            account_id=campaign_account_id,
                        )
                    except Exception:  # pragma: no cover
                        log.exception("Could not persist OpenAI generation failure log")

                    if code == "insufficient_post_text":
                        final_status = "skipped"
                        final_message = "Пропущено: недостаточно текста публикации для OpenAI"
                        consume_channel = True
                        return
                    if code in {
                        "timeout", "network_error", "rate_limit",
                        "provider_unavailable", "provider_error",
                    }:
                        retry_at = utc_now() + timedelta(minutes=5)
                        final_message = (
                            f"OpenAI временно недоступен ({code}); повтор через 5 минут"
                        )
                        consume_channel = False
                        slot_deferred = True
                        changed = worker_db.defer_comment_slot(
                            slot_id, scheduled_at=retry_at, result=final_message
                        )
                        if not changed:
                            raise RuntimeError(
                                "Comment slot was no longer eligible for OpenAI deferral"
                            )
                        return
                    final_status = "failed"
                    consume_channel = False
                    campaign_pause_reason = f"Кампания приостановлена: {exc}"
                    final_message = campaign_pause_reason
                    return

                selected = generated.text
                saver = getattr(worker_db, "upsert_generated_comment_draft", None)
                if callable(saver):
                    row = saver(
                        account_id=campaign_account_id,
                        campaign_id=campaign_id,
                        source_channel_id=channel_id,
                        source_post_id=post_id,
                        linked_chat_id=linked_chat_id,
                        discussion_message_id=discussion_message_id,
                        post_text=generated_post_text,
                        generated_text=selected,
                        status="generated",
                        model=generated.model,
                    )
                    generated_draft_id = as_int((row or {}).get("id"), 0) or None
                    generated_draft_status = "generated"
                try:
                    worker_db.insert_log(
                        "INFO",
                        "OpenAI generation_completed: "
                        f"campaign_id={campaign_id}; channel_id={channel_id}; "
                        f"post_id={post_id}; model={generated.model}; "
                        f"output_length={generated.output_length}; "
                        f"word_count={len(selected.split())}",
                        account_id=campaign_account_id,
                    )
                except Exception:  # pragma: no cover
                    log.exception("Could not persist OpenAI generation completed log")
            else:
                selected = reserve_variant()

            # Validate deterministic local constraints before the membership
            # check and before reserving a delivery. Comment campaigns never join.
            if not selected or len(selected) > 4096:
                raise NonRetryableTelegramError(
                    "Comment text is empty or exceeds Telegram's 4096-character limit",
                    code="message_too_long",
                )

            async def send_to_current_target() -> None:
                if not worker_db.bind_comment_slot_target(
                    slot_id,
                    task_id,
                    channel_id=channel_id,
                    post_id=post_id,
                    linked_chat_id=linked_chat_id,
                    discussion_message_id=discussion_message_id,
                ):
                    raise NonRetryableTelegramError(
                        "Campaign slot target could not be persisted",
                        code="campaign_slot_unavailable",
                    )
                send_kwargs = {
                    "linked_chat_id": linked_chat_id,
                    "post_message_id": post_id,
                    "text": selected,
                    "channel_id": channel_id,
                    "membership_ready": True,
                    "account_id": campaign_account_id,
                    "campaign_id": campaign_id,
                    "action_type": "campaign_comment",
                }
                if discussion_message_id is not None:
                    send_kwargs["reply_to"] = discussion_message_id
                send_barrier = create_dispatch_barrier(channel_id, linked_chat_id)
                if send_barrier is not None:
                    send_kwargs["dispatch_barrier"] = send_barrier
                await comments.ensure_and_send_comment(**send_kwargs)

            phase = CommentSlotPhase.MEMBERSHIP
            # Membership was prepared once by the Links task. Do not spend an
            # additional get_permissions RPC before every comment. Telegram's
            # send result is the authoritative permission check.
            set_runtime(
                task_id,
                f"Отправка комментария: {channel_title}",
                account_id=campaign_account_id,
            )
            phase = CommentSlotPhase.READY_TO_SEND
            if scope_is_cancelled(channel_id):
                final_message = "Кампания приостановлена перед отправкой"
                consume_channel = False
                suspend_cancelled_slot(final_message)
                slot_deferred = True
                return

            if generated_draft_id is not None:
                updater = getattr(worker_db, "mark_generated_comment_draft_status", None)
                if callable(updater):
                    updater(
                        generated_draft_id,
                        account_id=campaign_account_id,
                        status="sending",
                    )
                    generated_draft_status = "sending"
            phase = CommentSlotPhase.SEND_STARTED
            await send_to_current_target()

            sent = True
            phase = CommentSlotPhase.SEND_CONFIRMED
            final_status = "sent"
            final_message = "Отправлено"
            try:
                worker_db.insert_log(
                    "INFO",
                    f"Комментарий отправлен: channel_id={channel_id}; post_id={post_id}; "
                    f"discussion_chat_id={linked_chat_id}; "
                    f"discussion_message_id={discussion_message_id or '—'}",
                    account_id=campaign_account_id,
                )
            except Exception:  # pragma: no cover
                log.exception("Could not persist successful comment log")
        except DeferredTelegramError as exc:
            deferred_code = getattr(exc, "code", "")
            if deferred_code == "local_quiet_hours":
                wait = max(1, int(exc.retry_after))
                retry_at = utc_now() + timedelta(seconds=wait)
                final_message = str(exc)
                slot_deferred = True
                consume_channel = False
                changed = worker_db.defer_comment_slot(
                    slot_id,
                    scheduled_at=retry_at,
                    result=final_message,
                )
                if not changed:
                    raise RuntimeError(
                        "Comment slot was no longer eligible for schedule deferral"
                    )
                try:
                    worker_db.insert_log(
                        "INFO",
                        final_message,
                        account_id=campaign_account_id,
                    )
                except Exception:
                    log.exception("Could not persist local schedule deferral log")
                return
            if deferred_code == "local_ban_before_dispatch":
                final_status = "skipped"
                final_message = "Пропущено: цель локально заблокирована до отправки"
                consume_channel = False
                return
            if deferred_code == "shutdown_before_dispatch":
                final_message = "Выполнение остановлено до отправки; слот сохранён"
                consume_channel = False
                suspend_cancelled_slot(final_message)
                slot_deferred = True
                return
            wait = max(1, int(exc.retry_after))
            retry_at = utc_now() + timedelta(seconds=wait)
            final_message = f"Отложено Telegram на {max(1, round(wait / 60))} мин"
            slot_deferred = True
            consume_channel = False
            changed = worker_db.defer_comment_slot_and_set_network_wait(
                task_id,
                slot_id,
                campaign_id,
                scheduled_at=retry_at,
                slot_result=final_message,
                reason=final_message,
            )
            if not changed:
                raise RuntimeError(
                    "Comment slot was no longer eligible for network deferral"
                )
            if deferred_code == "flood_wait_deferred" and campaign_account_id > 0:
                cooldown_writer = getattr(
                    worker_db, "set_account_rpc_cooldown", None
                )
                if callable(cooldown_writer):
                    try:
                        cooldown_writer(
                            account_id=campaign_account_id,
                            retry_at=retry_at,
                            code=deferred_code,
                            source_task_id=task_id,
                            wait_seconds=wait,
                        )
                    except Exception:
                        # The campaign/slot deferral is already durable. Preserve
                        # that safe state even if the broader account cooldown
                        # cannot be extended during a transient SQLite failure.
                        log.exception(
                            "Could not persist account FloodWait cooldown"
                        )
            return
        except asyncio.CancelledError:
            if phase < CommentSlotPhase.SEND_STARTED:
                final_message = "Выполнение остановлено до отправки; слот сохранён"
                consume_channel = False
                suspend_cancelled_slot(final_message)
                slot_deferred = True
            else:
                final_status = "uncertain"
                final_message = (
                    "Остановлено после начала отправки; результат требует проверки"
                )
                campaign_pause_reason = final_message
            raise
        except NonRetryableTelegramError as exc:
            code = getattr(exc, "code", "")
            negative_ttls = {
                "chat_write_forbidden": 7 * 24 * 60 * 60,
                "plain_text_forbidden": 7 * 24 * 60 * 60,
                "chat_restricted": 7 * 24 * 60 * 60,
                "permission_denied": 30 * 24 * 60 * 60,
                "channel_private": 30 * 24 * 60 * 60,
                "linked_chat_inaccessible": 7 * 24 * 60 * 60,
                "comments_disabled": 24 * 60 * 60,
                "message_id_invalid": 24 * 60 * 60,
            }
            cache_writer = getattr(worker_db, "set_channel_negative_cache", None)
            if code in negative_ttls and callable(cache_writer):
                cache_writer(
                    channel_id,
                    code,
                    ttl_seconds=negative_ttls[code],
                    account_id=campaign_account_id,
                )
            friendly = {
                "join_required": "Пропущено: сначала подготовьте участие во вкладке «Связки»",
                "chat_write_forbidden": "Пропущено: аккаунту запрещено писать в обсуждении",
                "user_banned": "Кампания остановлена: Telegram ограничил отправку аккаунта",
                "message_too_long": "Пропущено: текст превышает лимит Telegram",
                "entity_bounds_invalid": "Пропущено: некорректная разметка текста",
                "user_blocked": "Пропущено: получатель заблокирован",
                "plain_text_forbidden": "Пропущено: в обсуждении запрещены обычные текстовые сообщения",
                "chat_restricted": "Пропущено: обсуждение ограничено Telegram",
                "privacy_restricted": "Пропущено: действие запрещено настройками приватности",
                "permission_denied": "Пропущено: приватное обсуждение требует инвайт или доступ",
                "channel_private": "Пропущено: приватное обсуждение недоступно аккаунту",
                "linked_chat_inaccessible": "Пропущено: группа обсуждения недоступна аккаунту",
                "comments_disabled": "Пропущено: у последнего поста комментарии отключены",
                "message_id_invalid": "Пропущено: ветка последнего поста удалена",
                "peer_flood": "Кампания остановлена: Telegram ограничил активность аккаунта",
                "user_restricted": "Кампания остановлена: Telegram ограничил аккаунт",
                "auth_key_duplicated": "Кампания остановлена: Telegram аннулировал дублирующий ключ сессии",
                "flood_wait_long": "Кампания приостановлена: Telegram запросил слишком долгое ожидание",
                "flood_wait_repeated": "Кампания приостановлена: Telegram повторно ограничил запросы",
                "slow_mode_wait_long": "Кампания приостановлена: слишком долгий медленный режим",
                "slow_mode_wait_repeated": "Кампания приостановлена: повторный slow mode",
                "delivery_result_unknown": "Кампания приостановлена: результат отправки неизвестен",
                "direct_message_result_unknown": "Кампания приостановлена: результат отправки в группу неизвестен",
                "join_result_unknown": "Кампания приостановлена: результат вступления неизвестен",
                "delivery_persist_failed": "Кампания приостановлена: комментарий отправлен, но подтверждение не сохранено",
                "direct_message_persist_failed": "Кампания приостановлена: сообщение в группу отправлено, но подтверждение не сохранено",
                "direct_message_duplicate_guard": "Кампания приостановлена: отправка в группу уже выполнялась или требует ручной проверки",
                "comment_already_reserved": "Пропущено: этот пост уже отправлялся или требует ручной проверки",
                "network_unavailable": "Нет соединения с Telegram. Кампания временно ожидает сеть",
                "account_state_mismatch": "Кампания приостановлена: Telegram-сессия не совпадает с локальным аккаунтом",
            }.get(code, f"Ошибка Telegram: {exc}")
            details = dict(getattr(exc, "details", {}) or {})
            cause = getattr(exc, "__cause__", None)
            rpc_error = str(
                details.get("rpc_error")
                or (type(cause).__name__ if cause is not None else "")
                or "unknown"
            )
            rpc_message = str(details.get("rpc_message") or exc)
            diagnostic = (
                f"channel_id={channel_id}; post_id={post_id or '—'}; "
                f"linked_chat_id={linked_chat_id}; "
                f"discussion_chat_id={discussion_chat_id or '—'}; "
                f"discussion_message_id={discussion_message_id or '—'}; "
                f"code={code or 'unknown'}; rpc={rpc_error}; detail={rpc_message}"
            )
            final_message = f"{friendly} | {diagnostic}"
            try:
                worker_db.insert_log(
                    "WARNING",
                    f"Комментарий не отправлен: {diagnostic}",
                    account_id=campaign_account_id,
                )
            except Exception:
                log.exception("Could not persist detailed Telegram comment log")
            log.warning("Comment send rejected: %s", diagnostic)
            if code in RESTRICTION_CODES:
                account_restriction = (
                    code,
                    friendly,
                    {
                        "channel_id": channel_id,
                        "post_id": post_id,
                        "linked_chat_id": linked_chat_id,
                        "rpc_error": rpc_error,
                        "rpc_message": rpc_message,
                    },
                )
                consume_channel = False
                final_status = "failed"
                campaign_pause_reason = friendly
            if code == "account_state_mismatch":
                consume_channel = False
                final_status = "failed"
                campaign_pause_reason = friendly
            if code == "network_unavailable":
                failure_count = int(campaign.get("network_failure_count") or 0) + 1
                backoff_steps = (60, 180, 300, 600, 1200, 1800)
                backoff = backoff_steps[min(failure_count - 1, len(backoff_steps) - 1)]
                retry_at = utc_now() + timedelta(seconds=backoff)
                slot_result = (
                    f"Ожидание сети; повтор через {max(1, round(backoff / 60))} мин"
                )
                network_reason = (
                    "Нет соединения с Telegram. "
                    f"Автоматическая проверка через {max(1, round(backoff / 60))} мин"
                )
                slot_deferred = True
                consume_channel = False
                changed = worker_db.defer_comment_slot_and_set_network_wait(
                    task_id,
                    slot_id,
                    campaign_id,
                    scheduled_at=retry_at,
                    slot_result=slot_result,
                    reason=network_reason,
                )
                if not changed:
                    raise RuntimeError(
                        "Comment slot was no longer eligible for network deferral"
                    )
                return
            if code == "discussion_relink_deferred":
                consume_channel = False
            elif code in {
                "linked_chat_inaccessible",
                "permission_denied",
                "channel_private",
            }:
                # These are final target-access outcomes for the current account.
                # Consume the target cooldown so one inaccessible channel cannot
                # be selected by every remaining slot in the same campaign.
                consume_channel = True
            if code in {
                "user_banned",
                "peer_flood",
                "user_restricted",
                "auth_key_duplicated",
                "flood_wait_long",
                "flood_wait_repeated",
                "slow_mode_wait_long",
                "slow_mode_wait_repeated",
                "delivery_result_unknown",
                "direct_message_result_unknown",
                "join_result_unknown",
                "delivery_persist_failed",
                "direct_message_persist_failed",
                "direct_message_duplicate_guard",
                "security_time_sync",
                "account_state_mismatch",
            }:
                final_status = (
                    "uncertain"
                    if code
                    in {
                        "delivery_result_unknown",
                        "direct_message_result_unknown",
                        "direct_message_duplicate_guard",
                        "join_result_unknown",
                    }
                    else "failed"
                )
                campaign_pause_reason = friendly
            else:
                final_status = "skipped"
        except TelegramOperationError as exc:
            # Unknown RPC failures are not proof that the channel is bad. Keep
            # the slot and rotation target, then retry after a bounded pause.
            cause = getattr(exc, "__cause__", None)
            rpc_error = (
                type(cause).__name__ if cause is not None else type(exc).__name__
            )
            diagnostic = (
                f"channel_id={channel_id}; post_id={post_id or '—'}; "
                f"linked_chat_id={linked_chat_id}; "
                f"discussion_chat_id={discussion_chat_id or '—'}; "
                f"discussion_message_id={discussion_message_id or '—'}; "
                f"code=telegram_operation_error; rpc={rpc_error}; detail={exc}"
            )
            final_message = f"Временная ошибка Telegram; повтор отложен | {diagnostic}"
            retry_at = utc_now() + timedelta(seconds=120)
            slot_deferred = True
            consume_channel = False
            changed = worker_db.defer_comment_slot_and_set_network_wait(
                task_id,
                slot_id,
                campaign_id,
                scheduled_at=retry_at,
                slot_result=final_message,
                reason="Временная ошибка Telegram; автоматический повтор через 2 минуты",
            )
            if not changed:
                raise RuntimeError(
                    "Comment slot was no longer eligible for network deferral"
                )
            try:
                worker_db.insert_log("WARNING", final_message, account_id=campaign_account_id)
            except Exception:
                log.exception("Could not persist Telegram operation error log")
        except Exception as exc:
            log.exception("Campaign slot failed for channel %s", channel_id)
            final_status = (
                "uncertain" if phase >= CommentSlotPhase.SEND_STARTED else "failed"
            )
            diagnostic = (
                f"channel_id={channel_id}; post_id={post_id or '—'}; "
                f"linked_chat_id={linked_chat_id}; "
                f"discussion_chat_id={discussion_chat_id or '—'}; "
                f"discussion_message_id={discussion_message_id or '—'}; "
                f"code=internal_error; rpc={type(exc).__name__}; detail={exc}"
            )
            final_message = f"Кампания приостановлена: внутренняя ошибка | {diagnostic}"
            consume_channel = phase >= CommentSlotPhase.SEND_STARTED
            campaign_pause_reason = final_message
            internal_error = exc
            try:
                worker_db.insert_log("ERROR", final_message, account_id=campaign_account_id)
            except Exception:
                log.exception("Could not persist internal comment error log")
        finally:
            if generated_draft_id is not None:
                updater = getattr(worker_db, "mark_generated_comment_draft_status", None)
                if callable(updater):
                    desired_draft_status = generated_draft_status
                    if sent:
                        desired_draft_status = "sent"
                    elif phase >= CommentSlotPhase.SEND_STARTED:
                        desired_draft_status = "uncertain"
                    elif generated_draft_status in {"generated", "sending"}:
                        desired_draft_status = (
                            "cancelled" if slot_deferred else "failed"
                        )
                    if (
                        desired_draft_status
                        and desired_draft_status != generated_draft_status
                    ):
                        try:
                            updater(
                                generated_draft_id,
                                account_id=campaign_account_id,
                                status=desired_draft_status,
                                error_code=(
                                    "send_uncertain"
                                    if desired_draft_status == "uncertain"
                                    else None
                                ),
                                error_message=(
                                    final_message
                                    if desired_draft_status in {"failed", "uncertain", "cancelled"}
                                    else None
                                ),
                            )
                            generated_draft_status = desired_draft_status
                        except Exception:
                            log.exception("Could not finalize generated comment draft")
            restriction_kwargs = None
            if account_restriction is not None:
                restriction_code, restriction_message, restriction_details = (
                    account_restriction
                )
                restriction_kwargs = build_account_restriction_kwargs(
                    worker_db,
                    code=restriction_code,
                    message=restriction_message,
                    details=restriction_details,
                    account_id=campaign_account_id,
                )
            restriction_state = finalize_comment_slot(
                worker_db=worker_db,
                task_id=task_id,
                slot_id=slot_id,
                campaign_id=campaign_id,
                channel_id=channel_id,
                post_id=post_id,
                selected=selected,
                final_status=final_status,
                final_message=final_message,
                sent=sent,
                consume_channel=consume_channel,
                campaign_pause_reason=campaign_pause_reason,
                internal_error=internal_error,
                slot_deferred=slot_deferred,
                account_id=campaign_account_id,
                restriction_kwargs=restriction_kwargs,
            )
        if restriction_state:
            request_cancel = getattr(queue_worker, "request_scope_cancellation", None)
            if callable(request_cancel):
                comment_ids = list(restriction_state.get("comment_campaign_ids") or [])
                join_ids = list(restriction_state.get("join_campaign_ids") or [])
                if not comment_ids:
                    comment_id = (
                        restriction_state.get("comment_campaign_id") or campaign_id
                    )
                    if comment_id:
                        comment_ids.append(comment_id)
                if not join_ids:
                    join_id = restriction_state.get("join_campaign_id")
                    if join_id:
                        join_ids.append(join_id)
                for comment_id in comment_ids:
                    request_cancel("comment_campaign", int(comment_id))
                for join_id in join_ids:
                    request_cancel("join_campaign", int(join_id))
        if internal_error is not None:
            raise internal_error

    return auto_comment_slot

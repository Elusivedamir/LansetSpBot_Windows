"""Explicit state-machine runner for one durable campaign comment slot."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta
from typing import Any, Callable, cast

from core.account_restriction import (
    RESTRICTION_CODES,
    build_account_restriction_kwargs,
    get_account_restriction_state,
)
from core.campaign_schedule import utc_now
from core.exceptions import (
    DeferredTelegramError,
    NonRetryableTelegramError,
    TelegramOperationError,
)
from core.openai_settings import (
    DEFAULT_OPENAI_SYSTEM_PROMPT,
    SOURCE_OPENAI,
    CommentGenerationSettings,
    normalize_comment_source,
)
from services.openai_comment_service import OpenAICommentError, extract_post_text
from workers.comment_slot.decisions import (
    DeferredCommentDisposition,
    deferred_comment_disposition,
    generated_draft_terminal_status,
    network_backoff_seconds,
    nonretryable_comment_decision,
)
from workers.comment_slot.finalization import finalize_comment_slot
from workers.comment_slot.models import CommentSlotPhase
from workers.comment_slot.state import CommentSlotState
from workers.flood_wait_guard import install_account_flood_wait
from workers.rpc_boundary import dispatch_barrier_kwargs

log = logging.getLogger(__name__)


class CommentSlotRunner:
    """Execute one account-owned slot while preserving ambiguity boundaries."""

    def __init__(
        self,
        *,
        as_int: Callable[[Any, int], int],
        queue_worker: Any,
        config: Any,
        worker_db: Any,
        telegram: Any,
        comments: Any,
        openai_service: Any | None,
        set_runtime: Callable[..., None],
        task: dict[str, Any],
    ) -> None:
        self.as_int = as_int
        self.queue_worker = queue_worker
        self.config = config
        self.worker_db = worker_db
        self.telegram = telegram
        self.comments = comments
        self.openai_service = openai_service
        self.set_runtime = set_runtime
        self.task = task
        self.state: CommentSlotState | None = None
        self.register_peer: Any | None = None

    @property
    def s(self) -> CommentSlotState:
        if self.state is None:  # pragma: no cover - construction invariant
            raise RuntimeError("Comment slot state is not initialized")
        return self.state

    async def run(self) -> None:
        state = self._initialize_campaign_state()
        if state is None:
            return
        self.state = state
        if not self._prepare_target():
            return
        self._log_target_start()

        restriction_state: dict[str, Any] | None = None
        try:
            await self._execute_delivery()
        except DeferredTelegramError as exc:
            self._handle_deferred(exc)
        except asyncio.CancelledError:
            self._handle_cancelled()
            raise
        except NonRetryableTelegramError as exc:
            self._handle_nonretryable(exc)
        except TelegramOperationError as exc:
            self._handle_telegram_operation_error(exc)
        except Exception as exc:
            self._handle_internal_error(exc)
        finally:
            restriction_state = self._finalize_slot()

        if restriction_state:
            self._cancel_restricted_scopes(restriction_state)
        if self.s.internal_error is not None:
            raise self.s.internal_error

    def _initialize_campaign_state(self) -> CommentSlotState | None:
        payload = self.task.get("payload") or {}
        task_id = int(self.task["id"])
        campaign_id = self.as_int(payload.get("campaign_id"), 0)
        slot_id = self.as_int(payload.get("slot_id"), 0)
        if campaign_id <= 0 or slot_id <= 0:
            raise NonRetryableTelegramError(
                "auto_comment_slot requires campaign_id and slot_id",
                code="invalid_payload",
            )
        campaign = self.worker_db.get_comment_campaign(campaign_id)
        if not campaign:
            raise NonRetryableTelegramError(
                "Comment campaign no longer exists", code="campaign_missing"
            )
        campaign_account_id = self.as_int(campaign.get("account_id"), 0)
        payload_account_id = self.as_int(payload.get("account_id"), 0)
        self._validate_execution_context(
            task_id=task_id,
            slot_id=slot_id,
            campaign_id=campaign_id,
            campaign_account_id=campaign_account_id,
        )
        if get_account_restriction_state(
            self.worker_db, account_id=campaign_account_id
        ).get("active"):
            self.worker_db.stop_comment_campaign(
                campaign_id,
                reason="Отправки заблокированы до проверки ограничения через @SpamBot",
            )
            return None
        campaign_account_id = self._validate_account_binding(
            campaign_id=campaign_id,
            slot_id=slot_id,
            campaign_account_id=campaign_account_id,
            payload_account_id=payload_account_id,
        )
        if not self._require_running_campaign(campaign_id, slot_id, campaign):
            return None

        state = CommentSlotState(
            task_id=task_id,
            payload=dict(payload),
            campaign_id=campaign_id,
            slot_id=slot_id,
            campaign=dict(campaign),
            campaign_account_id=campaign_account_id,
            payload_account_id=payload_account_id,
        )
        if not self._load_comment_source(state):
            return None
        return state

    def _validate_execution_context(
        self,
        *,
        task_id: int,
        slot_id: int,
        campaign_id: int,
        campaign_account_id: int,
    ) -> None:
        validator = getattr(
            type(self.worker_db), "validate_comment_slot_execution_context", None
        )
        if callable(validator) and not validator(
            self.worker_db,
            task_id,
            slot_id,
            campaign_id,
            campaign_account_id,
        ):
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

    def _validate_account_binding(
        self,
        *,
        campaign_id: int,
        slot_id: int,
        campaign_account_id: int,
        payload_account_id: int,
    ) -> int:
        get_setting = getattr(self.worker_db, "get_setting", None)
        strict = type(self.worker_db).__module__.startswith("storage.")
        if not strict or not callable(get_setting):
            return campaign_account_id or payload_account_id
        current_account_id = self.as_int(get_setting("telegram.account_id", 0), 0)
        if (
            campaign_account_id > 0
            and payload_account_id > 0
            and current_account_id > 0
            and campaign_account_id == payload_account_id
            and campaign_account_id == current_account_id
        ):
            return campaign_account_id
        reason = (
            "Кампания приостановлена: аккаунт кампании, задачи и "
            "активной Telegram-сессии не совпадает "
            f"(campaign={campaign_account_id}, task={payload_account_id}, "
            f"current={current_account_id})"
        )
        self.worker_db.pause_comment_campaign(campaign_id, reason=reason)
        self.worker_db.defer_comment_slot(
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

    def _require_running_campaign(
        self, campaign_id: int, slot_id: int, campaign: dict[str, Any]
    ) -> bool:
        status = str(campaign.get("status") or "")
        if status == "running":
            return True
        message = f"Кампания не активна: {status or 'unknown'}"
        if status == "paused":
            self.worker_db.defer_comment_slot(
                slot_id, scheduled_at=utc_now(), result=message
            )
        else:
            self.worker_db.cancel_comment_slot(slot_id, result=message)
        return False

    def _load_comment_source(self, state: CommentSlotState) -> bool:
        reader = getattr(self.worker_db, "get_campaign_comment_settings", None)
        snapshot = (reader(state.campaign_id) if callable(reader) else {}) or {}
        state.comment_source = normalize_comment_source(snapshot.get("comment_source"))
        state.generation_settings = CommentGenerationSettings.from_mapping(
            {
                "openai.model": snapshot.get("model"),
                "openai.max_words": snapshot.get("max_words"),
                "openai.temperature": snapshot.get("temperature"),
                "openai.timeout_seconds": snapshot.get("timeout_seconds"),
                "openai.max_generation_attempts": snapshot.get(
                    "max_generation_attempts"
                ),
            }
        )
        state.generation_prompt = str(
            snapshot.get("system_prompt") or DEFAULT_OPENAI_SYSTEM_PROMPT
        ).strip()
        state.variants = [
            str(text).strip()
            for text in state.campaign.get("comments", [])
            if isinstance(text, str) and text.strip()
        ]
        if not state.variants:
            self._fail_before_target(
                state,
                message="Добавьте хотя бы один комментарий",
                pause_reason="В кампании нет вариантов комментариев",
            )
            return False
        if state.comment_source == SOURCE_OPENAI and self.openai_service is None:
            message = "OpenAI-сервис недоступен в этой сборке"
            self._fail_before_target(
                state,
                message=message,
                pause_reason=message,
                compatibility_fallback=False,
            )
            return False
        return True

    def _fail_before_target(
        self,
        state: CommentSlotState,
        *,
        message: str,
        pause_reason: str,
        compatibility_fallback: bool = True,
    ) -> None:
        finalizer = getattr(
            type(self.worker_db), "finalize_comment_slot_outcome", None
        )
        if callable(finalizer):
            finalizer(
                self.worker_db,
                state.task_id,
                state.slot_id,
                status="failed",
                result=message,
                consume_channel=False,
                campaign_pause_reason=pause_reason,
            )
            return
        if compatibility_fallback:
            self.worker_db.pause_campaign_for_safety(state.campaign_id, pause_reason)
            self.worker_db.finish_comment_slot(
                state.slot_id, status="failed", result=message
            )

    def target_allows_rpc(self, *peer_ids: int | None) -> bool:
        state = self.s
        latest = self.worker_db.get_comment_campaign(state.campaign_id)
        if not latest or str(latest.get("status") or "") != "running":
            return False
        if get_account_restriction_state(
            self.worker_db, account_id=state.campaign_account_id
        ).get("active"):
            return False
        checker = getattr(self.worker_db, "is_channel_locally_banned", None)
        if callable(checker):
            for peer_id in peer_ids:
                if peer_id is not None and checker(
                    int(peer_id), account_id=state.campaign_account_id
                ) is True:
                    return False
        return True

    def create_dispatch_barrier(
        self,
        channel_scope_id: int | None = None,
        related_peer_id: int | None = None,
    ):
        state = self.s
        factory = getattr(
            type(self.queue_worker), "create_scope_dispatch_barrier", None
        )
        if self.queue_worker is None or not callable(factory):
            return None
        scopes: list[tuple[str, int] | tuple[str, int, int]] = [
            state.cancellation_scope,
            ("task", state.task_id),
        ]
        if channel_scope_id is not None:
            scopes.append(
                ("channel", int(channel_scope_id), state.campaign_account_id)
            )
        if related_peer_id is not None:
            scopes.append(
                ("channel", int(related_peer_id), state.campaign_account_id)
            )
        return factory(
            self.queue_worker,
            *scopes,
            pre_dispatch_check=lambda: self.target_allows_rpc(
                channel_scope_id, related_peer_id
            ),
        )

    def reserve_variant(self) -> str:
        state = self.s
        reserver = getattr(
            type(self.worker_db), "reserve_comment_variant_for_slot", None
        )
        if callable(reserver):
            reservation = reserver(
                self.worker_db,
                state.slot_id,
                state.task_id,
                account_id=state.campaign_account_id,
                variants=state.variants,
            )
            text = str((reservation or {}).get("text") or "").strip()
            if not text:
                raise NonRetryableTelegramError(
                    "Comment variant reservation returned an empty text",
                    code="comment_text_missing",
                )
            return text
        last_text = str(state.campaign.get("last_comment_text") or "")
        pool = [item for item in state.variants if item != last_text] or state.variants
        return random.choice(pool)

    def scope_is_cancelled(self, channel_scope_id: int | None = None) -> bool:
        state = self.s
        callback = getattr(self.queue_worker, "is_scope_cancelled", None)
        if callable(callback):
            if callback(*state.cancellation_scope) or callback("task", state.task_id):
                return True
            if channel_scope_id is not None and callback(
                "channel", int(channel_scope_id), state.campaign_account_id
            ):
                return True
        latest = self.worker_db.get_comment_campaign(state.campaign_id)
        return not latest or str(latest.get("status") or "") != "running"

    def suspend_cancelled_slot(self, message: str) -> None:
        state = self.s
        latest = self.worker_db.get_comment_campaign(state.campaign_id) or {}
        if str(latest.get("status") or "") == "stopped":
            self.worker_db.cancel_comment_slot(state.slot_id, result=message)
        else:
            self.worker_db.defer_comment_slot(
                state.slot_id, scheduled_at=utc_now(), result=message
            )

    def _prepare_target(self) -> bool:
        state = self.s
        if self.scope_is_cancelled():
            self.suspend_cancelled_slot(
                "Кампания приостановлена до начала проверки"
            )
            return False
        route_reader = getattr(self.worker_db, "get_comment_slot_route", None)
        state.cached_route = (
            route_reader(state.slot_id, state.task_id)
            if callable(route_reader)
            else None
        )
        state.cached_channel_id = self.as_int(
            (state.cached_route or {}).get("channel_id"), 0
        )
        channels = self._load_candidate_channels()
        if not channels:
            self._finish_exhausted_campaign()
            return False
        state.channel = channels[0]
        if state.channel.get("local_banned_at"):
            self._finish_locally_banned_target()
            return False
        self._initialize_target_fields()
        return True

    def _load_candidate_channels(self) -> list[dict[str, Any]]:
        state = self.s
        if state.cached_channel_id != 0:
            reader = getattr(self.worker_db, "get_channel_by_id", None)
            cached = (
                reader(
                    state.cached_channel_id,
                    account_id=state.campaign_account_id,
                )
                if callable(reader)
                else None
            )
            return [cached] if cached else []
        return list(
            self.worker_db.get_channels_for_commenting(
                1,
                cooldown_hours=24,
                account_id=state.campaign_account_id,
            )
        )

    def _mark_slot_running(self) -> None:
        state = self.s
        if not self.worker_db.mark_comment_slot_running(
            state.slot_id, state.task_id
        ):
            raise NonRetryableTelegramError(
                "Campaign slot is not available for execution",
                code="campaign_slot_unavailable",
            )

    def _finish_exhausted_campaign(self) -> None:
        state = self.s
        self._mark_slot_running()
        continuous = bool(state.campaign.get("continuous"))
        message = (
            "Все доступные каналы и группы уже обработаны за последние "
            "24 часа; ожидание следующего цикла"
            if continuous
            else "Кампания завершена: все доступные каналы и группы уже "
            "обработаны за последние 24 часа"
        )
        finalizer = getattr(
            type(self.worker_db), "finalize_comment_slot_outcome", None
        )
        if callable(finalizer):
            finalizer(
                self.worker_db,
                state.task_id,
                state.slot_id,
                status="skipped",
                result=message,
                consume_channel=False,
            )
        else:  # pragma: no cover - compatibility test doubles
            self.worker_db.add_comment_history(
                state.task_id,
                None,
                None,
                None,
                message,
                campaign_id=state.campaign_id,
                slot_id=state.slot_id,
            )
            self.worker_db.finish_comment_slot(
                state.slot_id, status="skipped", result=message
            )
        if not continuous:
            self.worker_db.complete_comment_campaign(state.campaign_id, message)
        self._safe_log("INFO", message, "exhausted-channel campaign")

    def _finish_locally_banned_target(self) -> None:
        state = self.s
        self._mark_slot_running()
        message = "Пропущено: канал локально заблокирован после неизвестного Join"
        finalizer = getattr(
            type(self.worker_db), "finalize_comment_slot_outcome", None
        )
        if callable(finalizer):
            finalizer(
                self.worker_db,
                state.task_id,
                state.slot_id,
                status="skipped",
                result=message,
                consume_channel=False,
            )
        else:  # pragma: no cover - compatibility test doubles
            self.worker_db.finish_comment_slot(
                state.slot_id, status="skipped", result=message
            )
        self._safe_log("WARNING", message, "locally-banned channel skip")

    def _initialize_target_fields(self) -> None:
        state = self.s
        channel = state.channel or {}
        try:
            state.channel_id = int(channel["channel_id"])
            state.comment_mode = str(
                channel.get("comment_mode") or "channel_post"
            )
            raw_linked_chat_id = channel.get("linked_chat_id")
            state.linked_chat_id = (
                state.channel_id
                if state.comment_mode == "direct_group"
                and raw_linked_chat_id is None
                else int(raw_linked_chat_id)
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise NonRetryableTelegramError(
                "Stored comment target is incomplete or corrupted",
                code="invalid_comment_target",
                details={
                    "rpc_error": type(exc).__name__,
                    "rpc_message": str(exc),
                },
            ) from exc
        self.register_peer = getattr(self.telegram, "register_peer_reference", None)
        self._register_target_peers(channel)
        self._mark_slot_running()
        state.channel_title = (
            channel.get("title")
            or channel.get("username")
            or str(state.channel_id)
        )
        state.discussion_chat_id = state.linked_chat_id
        self._restore_cached_route()

    def _register_target_peers(self, channel: dict[str, Any]) -> None:
        state = self.s
        if not callable(self.register_peer):
            return
        self.register_peer(
            state.channel_id,
            access_hash=channel.get("access_hash"),
            peer_type=channel.get("peer_type"),
        )
        if state.linked_chat_id is None:
            return
        reader = getattr(self.worker_db, "get_channel_by_id", None)
        linked_row = (
            reader(state.linked_chat_id, account_id=state.campaign_account_id)
            if callable(reader)
            else None
        )
        self.register_peer(
            state.linked_chat_id,
            access_hash=(linked_row or {}).get("access_hash"),
            peer_type=(linked_row or {}).get("peer_type"),
        )

    def _restore_cached_route(self) -> None:
        state = self.s
        if state.cached_channel_id != state.channel_id or not state.cached_route:
            return
        state.cached_post_id = self.as_int(state.cached_route.get("post_id"), 0)
        cached_linked_id = self.as_int(
            state.cached_route.get("linked_chat_id"), 0
        )
        cached_root_id = self.as_int(
            state.cached_route.get("discussion_message_id"), 0
        )
        if state.cached_post_id > 0 and cached_linked_id != 0:
            state.discussion_chat_id = cached_linked_id
            state.linked_chat_id = cached_linked_id
            state.discussion_message_id = cached_root_id or None

    def _log_target_start(self) -> None:
        state = self.s
        self._safe_log(
            "INFO",
            f"Проверка цели: title={state.channel_title}; "
            f"channel_id={state.channel_id}; mode={state.comment_mode}; "
            f"linked_chat_id={state.linked_chat_id}",
            "channel-check start",
        )

    def _safe_log(self, level: str, message: str, context: str) -> None:
        try:
            self.worker_db.insert_log(
                level, message, account_id=self.s.campaign_account_id
            )
        except Exception:  # pragma: no cover - logging must not change outcome
            log.exception("Could not persist %s log", context)

    async def _execute_delivery(self) -> None:
        state = self.s
        self.set_runtime(
            state.task_id,
            f"Суточная кампания: {state.channel_title}",
            account_id=state.campaign_account_id,
        )
        if self._cached_delivery_exists():
            state.post_id = state.cached_post_id
            state.final_message = "Пропущено: этот пост уже комментировали"
            return
        schedule_guard = getattr(self.comments, "activity_schedule", None)
        if schedule_guard is not None:
            schedule_guard.require_active()
        if state.comment_mode == "direct_group":
            state.final_status = "skipped"
            state.final_message = (
                "Пропущено: прямая отправка в обычные группы отключена"
            )
            state.consume_channel = False
            return
        if not self.target_allows_rpc(state.channel_id, state.linked_chat_id):
            state.final_status = "skipped"
            state.final_message = "Пропущено: цель локально заблокирована"
            state.consume_channel = False
            return
        if not await self._resolve_comment_route():
            return
        if self._delivery_exists_for_resolved_route():
            state.final_message = "Пропущено: этот пост уже комментировали"
            return
        if self.scope_is_cancelled(state.channel_id):
            state.final_message = (
                "Кампания приостановлена до проверки участия/отправки"
            )
            state.consume_channel = False
            self.suspend_cancelled_slot(state.final_message)
            state.slot_deferred = True
            return
        if not await self._prepare_selected_text(schedule_guard):
            return
        await self._send_selected_comment()

    def _cached_delivery_exists(self) -> bool:
        state = self.s
        return bool(
            state.comment_mode != "direct_group"
            and state.cached_post_id > 0
            and state.linked_chat_id is not None
            and self.worker_db.has_commented(
                state.channel_id,
                state.cached_post_id,
                account_id=state.campaign_account_id,
                linked_chat_id=state.linked_chat_id,
                campaign_id=state.campaign_id,
                action_type="campaign_comment",
            )
        )

    async def _resolve_comment_route(self) -> bool:
        state = self.s
        result = await self._request_commentable_post()
        state.resolved_result = result
        if result.message is not None:
            state.post_id = int(result.message.id)
        resolved_chat_id = getattr(result, "discussion_chat_id", None)
        resolved_message_id = getattr(result, "discussion_message_id", None)
        if resolved_chat_id is not None:
            state.discussion_chat_id = int(resolved_chat_id)
        if resolved_message_id is not None:
            state.discussion_message_id = int(resolved_message_id)
        if result.status != "ok":
            self._record_negative_route_result(result.status)
            return False
        self._validate_resolved_route()
        if state.discussion_chat_id != state.linked_chat_id:
            state.linked_chat_id = int(state.discussion_chat_id)
        if not self.target_allows_rpc(state.channel_id, state.linked_chat_id):
            state.final_status = "skipped"
            state.final_message = (
                "Пропущено: канал или чат обсуждения локально заблокирован"
            )
            state.consume_channel = False
            return False
        self._register_resolved_discussion()
        self._persist_resolved_route()
        return True

    async def _request_commentable_post(self):
        state = self.s
        barrier = self.create_dispatch_barrier(
            state.channel_id, state.linked_chat_id
        )
        if state.cached_post_id > 0:
            resolver = getattr(self.telegram, "get_post_for_commenting", None)
            if callable(resolver):
                return await resolver(
                    state.channel_id,
                    state.cached_post_id,
                    **dispatch_barrier_kwargs(resolver, barrier),
                )
        resolver = self.telegram.get_latest_post_for_commenting
        return await resolver(
            state.channel_id,
            **dispatch_barrier_kwargs(resolver, barrier),
        )

    def _record_negative_route_result(self, status: str) -> None:
        state = self.s
        skip_statuses = {
            "no_post": "Пропущено: в канале нет публикаций",
            "comments_disabled": (
                "Пропущено: у последнего поста комментарии отключены"
            ),
            "discussion_missing": (
                "Пропущено: ветка последнего поста удалена или недоступна"
            ),
        }
        state.final_message = skip_statuses.get(
            status, f"Пропущено: последний пост недоступен ({status})"
        )
        writer = getattr(self.worker_db, "set_channel_negative_cache", None)
        ttl_by_status = {
            "no_post": 60 * 60,
            "comments_disabled": 24 * 60 * 60,
            "discussion_missing": 24 * 60 * 60,
        }
        if callable(writer):
            writer(
                state.channel_id,
                status,
                ttl_seconds=ttl_by_status.get(status, 60 * 60),
                account_id=state.campaign_account_id,
            )

    def _validate_resolved_route(self) -> None:
        state = self.s
        if state.post_id is None:
            raise NonRetryableTelegramError(
                "Telegram returned an incomplete commentable-post result",
                code="message_id_invalid",
                details={
                    "rpc_error": "IncompleteDiscussionResult",
                    "rpc_message": "missing_post_message",
                },
            )
        if state.discussion_chat_id is None:
            raise NonRetryableTelegramError(
                "Telegram did not return the linked discussion chat",
                code="linked_chat_inaccessible",
                details={
                    "rpc_error": "IncompleteDiscussionResult",
                    "rpc_message": "missing_discussion_chat_id",
                },
            )

    def _register_resolved_discussion(self) -> None:
        state = self.s
        if not callable(self.register_peer):
            return
        reader = getattr(self.worker_db, "get_channel_by_id", None)
        row = (
            reader(state.linked_chat_id, account_id=state.campaign_account_id)
            if callable(reader)
            else None
        )
        self.register_peer(
            state.linked_chat_id,
            access_hash=(row or {}).get("access_hash"),
            peer_type=(row or {}).get("peer_type"),
        )

    def _persist_resolved_route(self) -> None:
        state = self.s
        binder = getattr(self.worker_db, "bind_comment_slot_target", None)
        if callable(binder) and not binder(
            state.slot_id,
            state.task_id,
            channel_id=state.channel_id,
            post_id=state.post_id,
            linked_chat_id=state.linked_chat_id,
            discussion_message_id=state.discussion_message_id,
        ):
            raise NonRetryableTelegramError(
                "Campaign slot route could not be persisted",
                code="campaign_slot_unavailable",
            )
        clearer = getattr(self.worker_db, "clear_channel_negative_cache", None)
        if callable(clearer):
            clearer(state.channel_id, account_id=state.campaign_account_id)
        log.info(
            "Comment target resolved: channel_id=%s post_id=%s linked_chat_id=%s "
            "discussion_chat_id=%s discussion_message_id=%s",
            state.channel_id,
            state.post_id,
            state.linked_chat_id,
            state.discussion_chat_id,
            state.discussion_message_id,
        )
        self._safe_log(
            "INFO",
            "Цель комментария найдена: "
            f"channel_id={state.channel_id}; post_id={state.post_id or '—'}; "
            f"linked_chat_id={state.linked_chat_id}; "
            f"discussion_chat_id={state.discussion_chat_id or '—'}; "
            f"discussion_message_id={state.discussion_message_id or '—'}",
            "resolved comment target",
        )

    def _delivery_exists_for_resolved_route(self) -> bool:
        state = self.s
        return bool(
            self.worker_db.has_commented(
                state.channel_id,
                state.post_id,
                account_id=state.campaign_account_id,
                linked_chat_id=state.linked_chat_id,
                campaign_id=state.campaign_id,
                action_type="campaign_comment",
            )
        )

    async def _prepare_selected_text(self, schedule_guard: Any | None) -> bool:
        state = self.s
        if state.comment_source != SOURCE_OPENAI:
            state.selected = self.reserve_variant()
            return self._validate_selected_text()
        return await self._prepare_openai_text(schedule_guard)

    async def _prepare_openai_text(self, schedule_guard: Any | None) -> bool:
        state = self.s
        result = state.resolved_result
        state.generated_post_text = extract_post_text(result.message)
        existing_draft = self._read_existing_generated_draft()
        existing_status = str((existing_draft or {}).get("status") or "")
        if existing_status == "sent":
            state.final_message = (
                "Пропущено: OpenAI-комментарий к этому посту уже отправлен"
            )
            return False
        if existing_status in {"sending", "uncertain"}:
            state.generated_draft_id = (
                self.as_int((existing_draft or {}).get("id"), 0) or None
            )
            state.generated_draft_status = existing_status
            state.final_status = "uncertain"
            state.consume_channel = False
            state.campaign_pause_reason = (
                "Кампания приостановлена: результат прежней отправки "
                "OpenAI-комментария требует проверки"
            )
            state.final_message = state.campaign_pause_reason
            return False
        if schedule_guard is not None:
            schedule_guard.require_active()
        reference_comment = self.reserve_variant()
        self.set_runtime(
            state.task_id,
            f"OpenAI генерирует комментарий: {state.channel_title}",
            account_id=state.campaign_account_id,
        )
        self._safe_log(
            "INFO",
            "OpenAI generation_started: "
            f"campaign_id={state.campaign_id}; channel_id={state.channel_id}; "
            f"post_id={state.post_id}; "
            f"input_length={len(state.generated_post_text)}; "
            f"reference_length={len(reference_comment)}",
            "OpenAI generation start",
        )
        try:
            generator = cast(Any, self.openai_service)
            settings = cast(CommentGenerationSettings, state.generation_settings)
            generated = await generator.generate_comment(
                state.generated_post_text,
                state.generation_prompt,
                settings,
                reference_comment,
            )
        except OpenAICommentError as exc:
            return self._handle_openai_generation_error(exc)
        state.selected = generated.text
        self._save_generated_draft(
            generated_text=state.selected,
            status="generated",
            model=generated.model,
        )
        state.generated_draft_status = "generated"
        self._safe_log(
            "INFO",
            "OpenAI generation_completed: "
            f"campaign_id={state.campaign_id}; channel_id={state.channel_id}; "
            f"post_id={state.post_id}; model={generated.model}; "
            f"output_length={generated.output_length}; "
            f"word_count={len(state.selected.split())}",
            "OpenAI generation completed",
        )
        return self._validate_selected_text()

    def _read_existing_generated_draft(self) -> dict[str, Any] | None:
        state = self.s
        reader = getattr(
            self.worker_db, "get_generated_comment_draft_for_post", None
        )
        if not callable(reader):
            return None
        return reader(
            account_id=state.campaign_account_id,
            source_channel_id=state.channel_id,
            source_post_id=state.post_id,
        )

    def _save_generated_draft(
        self,
        *,
        generated_text: str | None,
        status: str,
        model: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        state = self.s
        saver = getattr(self.worker_db, "upsert_generated_comment_draft", None)
        if not callable(saver):
            return
        row = saver(
            account_id=state.campaign_account_id,
            campaign_id=state.campaign_id,
            source_channel_id=state.channel_id,
            source_post_id=state.post_id,
            linked_chat_id=state.linked_chat_id,
            discussion_message_id=state.discussion_message_id,
            post_text=state.generated_post_text,
            generated_text=generated_text,
            status=status,
            model=model,
            error_code=error_code,
            error_message=error_message,
        )
        state.generated_draft_id = self.as_int((row or {}).get("id"), 0) or None

    def _handle_openai_generation_error(self, exc: OpenAICommentError) -> bool:
        state = self.s
        code = str(getattr(exc, "code", "openai_error") or "openai_error")
        settings = cast(CommentGenerationSettings, state.generation_settings)
        self._save_generated_draft(
            generated_text=None,
            status="generation_failed",
            model=settings.model,
            error_code=code,
            error_message=str(exc),
        )
        state.generated_draft_status = "generation_failed"
        self._safe_log(
            "WARNING",
            "OpenAI generation_failed: "
            f"campaign_id={state.campaign_id}; channel_id={state.channel_id}; "
            f"post_id={state.post_id}; code={code}",
            "OpenAI generation failure",
        )
        if code == "insufficient_post_text":
            state.final_status = "skipped"
            state.final_message = (
                "Пропущено: недостаточно текста публикации для OpenAI"
            )
            state.consume_channel = True
            return False
        if code in {
            "timeout",
            "network_error",
            "rate_limit",
            "provider_unavailable",
            "provider_error",
        }:
            retry_at = utc_now() + timedelta(minutes=5)
            state.final_message = (
                f"OpenAI временно недоступен ({code}); повтор через 5 минут"
            )
            state.consume_channel = False
            state.slot_deferred = True
            changed = self.worker_db.defer_comment_slot(
                state.slot_id,
                scheduled_at=retry_at,
                result=state.final_message,
            )
            if not changed:
                raise RuntimeError(
                    "Comment slot was no longer eligible for OpenAI deferral"
                )
            return False
        state.final_status = "failed"
        state.consume_channel = False
        state.campaign_pause_reason = f"Кампания приостановлена: {exc}"
        state.final_message = state.campaign_pause_reason
        return False

    def _validate_selected_text(self) -> bool:
        selected = self.s.selected
        if not selected or len(selected) > 4096:
            raise NonRetryableTelegramError(
                "Comment text is empty or exceeds Telegram's 4096-character limit",
                code="message_too_long",
            )
        return True

    async def _send_selected_comment(self) -> None:
        state = self.s
        state.phase = CommentSlotPhase.MEMBERSHIP
        self.set_runtime(
            state.task_id,
            f"Отправка комментария: {state.channel_title}",
            account_id=state.campaign_account_id,
        )
        state.phase = CommentSlotPhase.READY_TO_SEND
        if self.scope_is_cancelled(state.channel_id):
            state.final_message = "Кампания приостановлена перед отправкой"
            state.consume_channel = False
            self.suspend_cancelled_slot(state.final_message)
            state.slot_deferred = True
            return
        self._mark_generated_draft_sending()
        state.phase = CommentSlotPhase.SEND_STARTED
        await self._dispatch_comment()
        state.sent = True
        state.phase = CommentSlotPhase.SEND_CONFIRMED
        state.final_status = "sent"
        state.final_message = "Отправлено"
        self._safe_log(
            "INFO",
            f"Комментарий отправлен: channel_id={state.channel_id}; "
            f"post_id={state.post_id}; "
            f"discussion_chat_id={state.linked_chat_id}; "
            f"discussion_message_id={state.discussion_message_id or '—'}",
            "successful comment",
        )

    def _mark_generated_draft_sending(self) -> None:
        state = self.s
        if state.generated_draft_id is None:
            return
        updater = getattr(
            self.worker_db, "mark_generated_comment_draft_status", None
        )
        if callable(updater):
            updater(
                state.generated_draft_id,
                account_id=state.campaign_account_id,
                status="sending",
            )
            state.generated_draft_status = "sending"

    async def _dispatch_comment(self) -> None:
        state = self.s
        if not self.worker_db.bind_comment_slot_target(
            state.slot_id,
            state.task_id,
            channel_id=state.channel_id,
            post_id=state.post_id,
            linked_chat_id=state.linked_chat_id,
            discussion_message_id=state.discussion_message_id,
        ):
            raise NonRetryableTelegramError(
                "Campaign slot target could not be persisted",
                code="campaign_slot_unavailable",
            )
        kwargs: dict[str, Any] = {
            "linked_chat_id": state.linked_chat_id,
            "post_message_id": state.post_id,
            "text": state.selected,
            "channel_id": state.channel_id,
            "membership_ready": True,
            "account_id": state.campaign_account_id,
            "campaign_id": state.campaign_id,
            "action_type": "campaign_comment",
        }
        if state.discussion_message_id is not None:
            kwargs["reply_to"] = state.discussion_message_id
        barrier = self.create_dispatch_barrier(
            state.channel_id, state.linked_chat_id
        )
        if barrier is not None:
            kwargs["dispatch_barrier"] = barrier
        await self.comments.ensure_and_send_comment(**kwargs)

    def _handle_deferred(self, exc: DeferredTelegramError) -> None:
        state = self.s
        disposition = deferred_comment_disposition(getattr(exc, "code", ""))
        if disposition is DeferredCommentDisposition.QUIET_HOURS:
            self._defer_quiet_hours(exc)
            return
        if disposition is DeferredCommentDisposition.LOCAL_BAN:
            state.final_status = "skipped"
            state.final_message = (
                "Пропущено: цель локально заблокирована до отправки"
            )
            state.consume_channel = False
            return
        if disposition is DeferredCommentDisposition.SHUTDOWN:
            state.final_message = "Выполнение остановлено до отправки; слот сохранён"
            state.consume_channel = False
            self.suspend_cancelled_slot(state.final_message)
            state.slot_deferred = True
            return
        self._defer_telegram_wait(exc)

    def _defer_quiet_hours(self, exc: DeferredTelegramError) -> None:
        state = self.s
        wait = max(1, int(exc.retry_after))
        state.final_message = str(exc)
        state.slot_deferred = True
        state.consume_channel = False
        changed = self.worker_db.defer_comment_slot(
            state.slot_id,
            scheduled_at=utc_now() + timedelta(seconds=wait),
            result=state.final_message,
        )
        if not changed:
            raise RuntimeError(
                "Comment slot was no longer eligible for schedule deferral"
            )
        self._safe_log(
            "INFO", state.final_message, "local schedule deferral"
        )

    def _defer_telegram_wait(self, exc: DeferredTelegramError) -> None:
        state = self.s
        code = str(getattr(exc, "code", "") or "")
        wait = max(1, int(exc.retry_after))
        retry_at = utc_now() + timedelta(seconds=wait)
        state.final_message = (
            f"Отложено Telegram на {max(1, round(wait / 60))} мин"
        )
        state.consume_channel = False
        if code == "flood_wait_deferred" and state.campaign_account_id > 0:
            try:
                install_account_flood_wait(
                    queue_worker=self.queue_worker,
                    worker_db=self.worker_db,
                    account_id=state.campaign_account_id,
                    retry_at=retry_at,
                    code=code,
                    source_task_id=state.task_id,
                    wait_seconds=wait,
                )
            except Exception:
                log.exception(
                    "Could not persist account FloodWait; "
                    "local embargo remains active"
                )
        changed = self.worker_db.defer_comment_slot_and_set_network_wait(
            state.task_id,
            state.slot_id,
            state.campaign_id,
            scheduled_at=retry_at,
            slot_result=state.final_message,
            reason=state.final_message,
        )
        if not changed:
            raise RuntimeError(
                "Comment slot was no longer eligible for network deferral"
            )
        state.slot_deferred = True

    def _handle_cancelled(self) -> None:
        state = self.s
        if state.phase < CommentSlotPhase.SEND_STARTED:
            state.final_message = "Выполнение остановлено до отправки; слот сохранён"
            state.consume_channel = False
            self.suspend_cancelled_slot(state.final_message)
            state.slot_deferred = True
            return
        state.final_status = "uncertain"
        state.final_message = (
            "Остановлено после начала отправки; результат требует проверки"
        )
        state.campaign_pause_reason = state.final_message

    def _handle_nonretryable(self, exc: NonRetryableTelegramError) -> None:
        state = self.s
        code = str(getattr(exc, "code", "") or "")
        decision = nonretryable_comment_decision(
            code, f"Ошибка Telegram: {exc}"
        )
        writer = getattr(self.worker_db, "set_channel_negative_cache", None)
        if decision.negative_cache_ttl is not None and callable(writer):
            writer(
                state.channel_id,
                code,
                ttl_seconds=decision.negative_cache_ttl,
                account_id=state.campaign_account_id,
            )
        details = dict(getattr(exc, "details", {}) or {})
        cause = getattr(exc, "__cause__", None)
        rpc_error = str(
            details.get("rpc_error")
            or (type(cause).__name__ if cause is not None else "")
            or "unknown"
        )
        rpc_message = str(details.get("rpc_message") or exc)
        diagnostic = self._diagnostic(
            code=code or "unknown",
            rpc_error=rpc_error,
            detail=rpc_message,
        )
        state.final_message = f"{decision.friendly} | {diagnostic}"
        self._safe_log(
            "WARNING",
            f"Комментарий не отправлен: {diagnostic}",
            "detailed Telegram comment",
        )
        log.warning("Comment send rejected: %s", diagnostic)
        if code in RESTRICTION_CODES:
            state.account_restriction = (
                code,
                decision.friendly,
                {
                    "channel_id": state.channel_id,
                    "post_id": state.post_id,
                    "linked_chat_id": state.linked_chat_id,
                    "rpc_error": rpc_error,
                    "rpc_message": rpc_message,
                },
            )
            state.consume_channel = False
            state.final_status = "failed"
            state.campaign_pause_reason = decision.friendly
        elif code == "network_unavailable":
            self._defer_network_unavailable()
            return
        else:
            state.consume_channel = decision.consume_channel
            state.final_status = decision.final_status
            if decision.pause_campaign:
                state.campaign_pause_reason = decision.friendly

    def _defer_network_unavailable(self) -> None:
        state = self.s
        failure_count = int(state.campaign.get("network_failure_count") or 0) + 1
        backoff = network_backoff_seconds(failure_count)
        retry_at = utc_now() + timedelta(seconds=backoff)
        slot_result = (
            f"Ожидание сети; повтор через {max(1, round(backoff / 60))} мин"
        )
        reason = (
            "Нет соединения с Telegram. "
            f"Автоматическая проверка через {max(1, round(backoff / 60))} мин"
        )
        state.slot_deferred = True
        state.consume_channel = False
        changed = self.worker_db.defer_comment_slot_and_set_network_wait(
            state.task_id,
            state.slot_id,
            state.campaign_id,
            scheduled_at=retry_at,
            slot_result=slot_result,
            reason=reason,
        )
        if not changed:
            raise RuntimeError(
                "Comment slot was no longer eligible for network deferral"
            )

    def _handle_telegram_operation_error(
        self, exc: TelegramOperationError
    ) -> None:
        state = self.s
        cause = getattr(exc, "__cause__", None)
        rpc_error = (
            type(cause).__name__ if cause is not None else type(exc).__name__
        )
        diagnostic = self._diagnostic(
            code="telegram_operation_error",
            rpc_error=rpc_error,
            detail=str(exc),
        )
        state.final_message = (
            f"Временная ошибка Telegram; повтор отложен | {diagnostic}"
        )
        state.slot_deferred = True
        state.consume_channel = False
        changed = self.worker_db.defer_comment_slot_and_set_network_wait(
            state.task_id,
            state.slot_id,
            state.campaign_id,
            scheduled_at=utc_now() + timedelta(seconds=120),
            slot_result=state.final_message,
            reason=(
                "Временная ошибка Telegram; автоматический повтор через 2 минуты"
            ),
        )
        if not changed:
            raise RuntimeError(
                "Comment slot was no longer eligible for network deferral"
            )
        self._safe_log(
            "WARNING", state.final_message, "Telegram operation error"
        )

    def _handle_internal_error(self, exc: Exception) -> None:
        state = self.s
        log.exception("Campaign slot failed for channel %s", state.channel_id)
        state.final_status = (
            "uncertain"
            if state.phase >= CommentSlotPhase.SEND_STARTED
            else "failed"
        )
        diagnostic = self._diagnostic(
            code="internal_error",
            rpc_error=type(exc).__name__,
            detail=str(exc),
        )
        state.final_message = (
            f"Кампания приостановлена: внутренняя ошибка | {diagnostic}"
        )
        state.consume_channel = state.phase >= CommentSlotPhase.SEND_STARTED
        state.campaign_pause_reason = state.final_message
        state.internal_error = exc
        self._safe_log("ERROR", state.final_message, "internal comment error")

    def _diagnostic(self, *, code: str, rpc_error: str, detail: str) -> str:
        state = self.s
        return (
            f"channel_id={state.channel_id}; post_id={state.post_id or '—'}; "
            f"linked_chat_id={state.linked_chat_id}; "
            f"discussion_chat_id={state.discussion_chat_id or '—'}; "
            f"discussion_message_id={state.discussion_message_id or '—'}; "
            f"code={code}; rpc={rpc_error}; detail={detail}"
        )

    def _finalize_slot(self) -> dict[str, Any] | None:
        state = self.s
        self._finalize_generated_draft()
        restriction_kwargs = self._restriction_kwargs()
        return finalize_comment_slot(
            worker_db=self.worker_db,
            task_id=state.task_id,
            slot_id=state.slot_id,
            campaign_id=state.campaign_id,
            channel_id=state.channel_id,
            post_id=state.post_id,
            selected=state.selected,
            final_status=state.final_status,
            final_message=state.final_message,
            sent=state.sent,
            consume_channel=state.consume_channel,
            campaign_pause_reason=state.campaign_pause_reason,
            internal_error=state.internal_error,
            slot_deferred=state.slot_deferred,
            account_id=state.campaign_account_id,
            restriction_kwargs=restriction_kwargs,
        )

    def _finalize_generated_draft(self) -> None:
        state = self.s
        if state.generated_draft_id is None:
            return
        updater = getattr(
            self.worker_db, "mark_generated_comment_draft_status", None
        )
        if not callable(updater):
            return
        desired = generated_draft_terminal_status(
            current_status=state.generated_draft_status,
            sent=state.sent,
            send_started=state.phase >= CommentSlotPhase.SEND_STARTED,
            slot_deferred=state.slot_deferred,
        )
        if not desired or desired == state.generated_draft_status:
            return
        try:
            updater(
                state.generated_draft_id,
                account_id=state.campaign_account_id,
                status=desired,
                error_code="send_uncertain" if desired == "uncertain" else None,
                error_message=(
                    state.final_message
                    if desired in {"failed", "uncertain", "cancelled"}
                    else None
                ),
            )
            state.generated_draft_status = desired
        except Exception:
            log.exception("Could not finalize generated comment draft")

    def _restriction_kwargs(self) -> dict[str, Any] | None:
        state = self.s
        if state.account_restriction is None:
            return None
        code, message, details = state.account_restriction
        return build_account_restriction_kwargs(
            self.worker_db,
            code=code,
            message=message,
            details=details,
            account_id=state.campaign_account_id,
        )

    def _cancel_restricted_scopes(
        self, restriction_state: dict[str, Any]
    ) -> None:
        state = self.s
        request_cancel = getattr(
            self.queue_worker, "request_scope_cancellation", None
        )
        if not callable(request_cancel):
            return
        comment_ids = list(
            restriction_state.get("comment_campaign_ids") or []
        )
        join_ids = list(restriction_state.get("join_campaign_ids") or [])
        if not comment_ids:
            comment_id = (
                restriction_state.get("comment_campaign_id")
                or state.campaign_id
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

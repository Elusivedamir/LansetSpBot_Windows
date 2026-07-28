from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta
from enum import IntEnum
from typing import Any, Callable, cast

from core.campaign_schedule import utc_now
from core.account_restriction import (
    RESTRICTION_CODES,
    activate_account_restriction,
    build_account_restriction_kwargs,
    get_account_restriction_state,
)
from core.exceptions import (
    DeferredTelegramError,
    NonRetryableTelegramError,
    TelegramOperationError,
)

log = logging.getLogger(__name__)


class JoinSlotPhase(IntEnum):
    PRECHECK = 1
    MEMBERSHIP_CHECKED = 2
    READY_TO_JOIN = 3
    JOIN_STARTED = 4
    JOIN_CONFIRMED = 5


def create_join_slot_handler(
    *,
    as_int: Callable[[Any, int], int],
    queue_worker: Any,
    config: Any,
    worker_db: Any,
    telegram: Any,
    set_runtime: Callable[..., None],
):
    async def join_saved_slot(task: dict[str, Any]) -> None:
        payload = task.get("payload") or {}
        task_id = int(task["id"])
        campaign_id = as_int(payload.get("campaign_id"), 0)
        slot_id = as_int(payload.get("slot_id"), 0)
        if campaign_id <= 0 or slot_id <= 0:
            raise NonRetryableTelegramError(
                "join_saved_slot requires campaign_id and slot_id",
                code="invalid_payload",
            )

        context = worker_db.get_join_slot_context(campaign_id, slot_id)
        if not context:
            raise NonRetryableTelegramError(
                "Join campaign slot no longer exists", code="campaign_slot_missing"
            )
        campaign_account_id = as_int(context.get("account_id"), 0)
        if get_account_restriction_state(worker_db, account_id=campaign_account_id).get(
            "active"
        ):
            worker_db.stop_join_campaign(
                campaign_id,
                "Вступления заблокированы до проверки ограничения через @SpamBot",
            )
            return
        payload_account_id = as_int(payload.get("account_id"), 0)
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
                    "Кампания вступлений приостановлена: аккаунт кампании, "
                    "задачи и активной Telegram-сессии не совпадает "
                    f"(campaign={campaign_account_id}, task={payload_account_id}, "
                    f"current={current_account_id})"
                )
                worker_db.pause_join_campaign(campaign_id, reason)
                worker_db.defer_join_slot(slot_id, utc_now(), reason)
                raise NonRetryableTelegramError(
                    reason,
                    code="account_state_mismatch",
                    details={
                        "campaign_account_id": campaign_account_id,
                        "task_account_id": payload_account_id,
                        "current_account_id": current_account_id,
                    },
                )

        campaign_status = str(context.get("campaign_status") or "")
        if campaign_status != "running":
            message = f"Кампания не активна: {campaign_status or 'unknown'}"
            if campaign_status in {"paused", "network_wait"}:
                worker_db.defer_join_slot(slot_id, utc_now(), message)
            else:
                worker_db.cancel_join_slot(slot_id, result=message)
            return
        try:
            saved_dialog_id = int(context["saved_dialog_id"])
            account_id = int(context["account_id"])
            peer_id = int(context.get("peer_id") or 0)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise NonRetryableTelegramError(
                "Данные цели вступления повреждены",
                code="invalid_join_target",
            ) from exc
        if saved_dialog_id <= 0 or account_id <= 0 or peer_id == 0:
            raise NonRetryableTelegramError(
                "Данные цели вступления неполные",
                code="invalid_join_target",
            )
        if not worker_db.mark_join_slot_running(slot_id, task_id):
            raise NonRetryableTelegramError(
                "Слот вступления недоступен", code="campaign_slot_unavailable"
            )

        cancellation_scope = ("join_campaign", campaign_id)

        def target_is_locally_banned() -> bool:
            checker = getattr(type(worker_db), "is_channel_locally_banned", None)
            return bool(
                callable(checker)
                and checker(
                    worker_db,
                    peer_id,
                    account_id=account_id,
                )
            )

        def current_target_allows_rpc() -> bool:
            if target_is_locally_banned():
                return False
            if get_account_restriction_state(worker_db, account_id=account_id).get(
                "active"
            ):
                return False
            if current_campaign_status() != "running":
                return False
            if strict_account_binding and callable(get_setting):
                current_account_id = as_int(get_setting("telegram.account_id", 0), 0)
                if current_account_id != account_id:
                    return False
            return True

        def create_dispatch_barrier():
            factory = getattr(type(queue_worker), "create_scope_dispatch_barrier", None)
            if queue_worker is None or not callable(factory):
                return None
            return factory(
                queue_worker,
                cancellation_scope,
                ("task", task_id),
                ("channel", peer_id, account_id),
                pre_dispatch_check=current_target_allows_rpc,
            )

        def current_campaign_status() -> str:
            current = worker_db.get_join_campaign(campaign_id)
            if current is None:
                return ""
            if isinstance(current, dict):
                return str(current.get("status") or "")
            # Test doubles and compatibility repositories may not expose the
            # read method; the slot context is authoritative for this invocation.
            return campaign_status

        def scope_is_cancelled() -> bool:
            callback = getattr(queue_worker, "is_scope_cancelled", None)
            if callable(callback) and (
                callback(*cancellation_scope)
                or callback("task", task_id)
                or callback("channel", peer_id, account_id)
            ):
                return True
            return current_campaign_status() != "running"

        def suspend_cancelled_slot(message: str) -> None:
            if current_campaign_status() in {"stopped", "completed", ""}:
                worker_db.cancel_join_slot(slot_id, result=message)
            else:
                worker_db.defer_join_slot(slot_id, utc_now(), message)

        title = (
            context.get("title")
            or context.get("username")
            or str(context.get("saved_dialog_id"))
        )
        phase = JoinSlotPhase.PRECHECK
        joined = False
        final_status = "failed"
        final_message = "Вступление не завершено"
        slot_deferred = False
        internal_error: BaseException | None = None
        membership_status: str | None = None
        membership_error: str | None = None
        join_event_peer_id: int | None = None
        campaign_pause_reason: str | None = None
        account_restriction: tuple[str, str, dict[str, Any]] | None = None
        restriction_state: dict[str, Any] | None = None
        restriction_finalized_atomically = False

        def commit_unknown_join_ban(message: str) -> None:
            banner = getattr(type(worker_db), "ban_peer_locally", None)
            bound_banner = None
            if not callable(banner):
                bound_banner = getattr(worker_db, "ban_peer_locally", None)
                if not callable(bound_banner):
                    return

            def mutation():
                if callable(banner):
                    changed = banner(
                        worker_db,
                        peer_id,
                        message,
                        account_id=account_id,
                    )
                else:
                    fallback_banner = cast(Callable[..., Any], bound_banner)
                    changed = fallback_banner(
                        peer_id,
                        message,
                        account_id=account_id,
                    )
                if changed is False:
                    raise RuntimeError(
                        "Ambiguous Join target could not be locally banned"
                    )
                return changed

            runner = getattr(queue_worker, "cancel_scopes_and_run", None)
            if callable(runner):
                runner([("channel", peer_id, account_id)], mutation)
            else:  # pragma: no cover - compatibility for minimal test doubles
                mutation()

        try:
            if target_is_locally_banned():
                final_status = "skipped"
                final_message = (
                    "Цель пропущена: действует постоянная локальная блокировка"
                )
                membership_status = "uncertain"
                membership_error = final_message
                return

            if scope_is_cancelled():
                suspend_cancelled_slot("Кампания приостановлена до начала вступления")
                slot_deferred = True
                return

            guard = worker_db.get_join_guard(
                max_joins=max(1, int(context.get("max_per_hour") or 40)),
                min_interval_seconds=config.min_join_interval_seconds,
                window_seconds=3600,
                account_id=account_id,
            )
            if not guard["allowed"]:
                wait = max(30, int(guard["wait_seconds"]) + random.randint(5, 20))
                worker_db.defer_join_slot(
                    slot_id,
                    utc_now() + timedelta(seconds=wait),
                    f"Лимит вступлений; повтор через {max(1, round(wait / 60))} мин",
                )
                slot_deferred = True
                return

            if scope_is_cancelled():
                suspend_cancelled_slot("Кампания приостановлена до запроса вступления")
                slot_deferred = True
                return

            username = context.get("username")
            invite_link = context.get("invite_link")
            phase = JoinSlotPhase.READY_TO_JOIN
            set_runtime(
                task_id,
                f"Вступление: {title}",
                account_id=account_id,
            )
            phase = JoinSlotPhase.JOIN_STARTED
            join_kwargs = {"username": username, "invite_link": invite_link}
            join_barrier = create_dispatch_barrier()
            if join_barrier is not None:
                join_kwargs["dispatch_barrier"] = join_barrier
            newly_joined = await telegram.join_saved_dialog(**join_kwargs)
            phase = JoinSlotPhase.JOIN_CONFIRMED
            if not newly_joined:
                membership_status = "member"
                final_status = "already_member"
                final_message = "Уже состоял в канале/группе"
                return

            joined = True
            membership_status = "member"
            join_event_peer_id = peer_id
            final_status = "joined"
            final_message = "Вступление выполнено"
        except asyncio.CancelledError as exc:
            if phase < JoinSlotPhase.JOIN_STARTED:
                suspend_cancelled_slot("Остановлено до отправки запроса вступления")
                slot_deferred = True
            else:
                final_status = "uncertain"
                final_message = (
                    "Остановка произошла во время вступления; нужна ручная проверка"
                )
                membership_status = "uncertain"
                membership_error = final_message
                commit_unknown_join_ban(final_message)
            internal_error = exc
        except DeferredTelegramError as exc:
            code = getattr(exc, "code", "")
            if code == "local_ban_before_dispatch":
                if get_account_restriction_state(worker_db, account_id=account_id).get(
                    "active"
                ):
                    final_status = "cancelled"
                    final_message = (
                        "Вступление отменено: аккаунт ограничен до отправки запроса"
                    )
                    membership_status = "failed"
                    membership_error = final_message
                    campaign_pause_reason = final_message
                    return
                if strict_account_binding and callable(get_setting):
                    current_account_id = as_int(
                        get_setting("telegram.account_id", 0), 0
                    )
                    if current_account_id != account_id:
                        final_status = "failed"
                        final_message = (
                            "Вступление отменено: активный Telegram-аккаунт изменился"
                        )
                        membership_status = "failed"
                        membership_error = final_message
                        campaign_pause_reason = final_message
                        return
                final_status = "skipped"
                final_message = "Цель заблокирована до отправки запроса вступления"
                membership_status = "uncertain"
                membership_error = final_message
                return
            if code == "shutdown_before_dispatch":
                suspend_cancelled_slot(
                    "Остановлено до отправки запроса вступления; слот сохранён"
                )
                slot_deferred = True
                return
            wait = max(1, int(exc.retry_after))
            retry_at = utc_now() + timedelta(seconds=wait)
            slot_deferred = True
            changed = worker_db.defer_join_slot_and_set_network_wait(
                task_id,
                slot_id,
                campaign_id,
                scheduled_at=retry_at,
                slot_result=(
                    f"Ограничение Telegram; повтор через {max(1, round(wait / 60))} мин"
                ),
                reason="Telegram временно ограничил запросы",
            )
            if not changed:
                raise RuntimeError(
                    "Join slot was no longer eligible for network deferral"
                )
            if code == "flood_wait_deferred" and account_id > 0:
                cooldown_writer = getattr(
                    worker_db, "set_account_rpc_cooldown", None
                )
                if callable(cooldown_writer):
                    try:
                        cooldown_writer(
                            account_id=account_id,
                            retry_at=retry_at,
                            code=code,
                            source_task_id=task_id,
                            wait_seconds=wait,
                        )
                    except Exception:
                        # The join campaign is already safely in network_wait.
                        # Do not strand its slot if the broader cooldown write
                        # loses a transient SQLite race.
                        log.exception(
                            "Could not persist account FloodWait cooldown"
                        )
        except NonRetryableTelegramError as exc:
            code = getattr(exc, "code", "")
            if code == "network_unavailable":
                retry_at = utc_now() + timedelta(seconds=120)
                slot_deferred = True
                changed = worker_db.defer_join_slot_and_set_network_wait(
                    task_id,
                    slot_id,
                    campaign_id,
                    scheduled_at=retry_at,
                    slot_result="Ожидание сети",
                    reason="Нет соединения с Telegram; повтор через 2 минуты",
                )
                if not changed:
                    raise RuntimeError(
                        "Join slot was no longer eligible for network deferral"
                    )
            elif code == "join_requested":
                final_status = "join_requested"
                final_message = (
                    "Запрос на вступление отправлен; право писать ещё не получено"
                )
                membership_status = "join_requested"
                membership_error = None
            elif code == "account_state_mismatch":
                final_status = "failed"
                final_message = (
                    "Кампания приостановлена: Telegram-сессия не совпадает "
                    "с локальным аккаунтом"
                )
                membership_status = "failed"
                membership_error = str(exc)
                campaign_pause_reason = final_message
            elif code == "join_result_unknown":
                final_status = "uncertain"
                final_message = (
                    "Результат вступления неизвестен; цель локально заблокирована"
                )
                membership_status = "uncertain"
                membership_error = str(exc)
                commit_unknown_join_ban(final_message)
            else:
                final_status = "failed"
                final_message = f"Не удалось вступить: {exc}"
                membership_status = "failed"
                membership_error = str(exc)
                if code in RESTRICTION_CODES:
                    campaign_pause_reason = (
                        "Кампания остановлена: Telegram ограничил активность аккаунта"
                    )
                    account_restriction = (
                        code,
                        campaign_pause_reason,
                        {
                            "saved_dialog_id": saved_dialog_id,
                            "peer_id": peer_id,
                            "rpc_error": type(getattr(exc, "__cause__", None)).__name__,
                            "rpc_message": str(exc),
                        },
                    )
                elif code in {
                    "flood_wait_long",
                    "flood_wait_repeated",
                    "security_time_sync",
                }:
                    campaign_pause_reason = final_message
        except TelegramOperationError as exc:
            if phase < JoinSlotPhase.JOIN_STARTED:
                retry_at = utc_now() + timedelta(seconds=120)
                final_message = "Временная ошибка Telegram; повтор через 2 минуты"
                slot_deferred = True
                changed = worker_db.defer_join_slot_and_set_network_wait(
                    task_id,
                    slot_id,
                    campaign_id,
                    scheduled_at=retry_at,
                    slot_result=final_message,
                    reason=final_message,
                )
                if not changed:
                    raise RuntimeError(
                        "Join slot was no longer eligible for network deferral"
                    )
            else:
                final_status = "uncertain"
                final_message = "Ошибка Telegram после начала вступления; цель локально заблокирована"
                membership_status = "uncertain"
                membership_error = str(exc)
                commit_unknown_join_ban(final_message)
                internal_error = exc
        except Exception as exc:
            final_status = "failed"
            final_message = f"Внутренняя ошибка: {type(exc).__name__}: {exc}"
            membership_status = "failed"
            membership_error = str(exc)
            campaign_pause_reason = final_message
            internal_error = exc
        finally:
            if not slot_deferred:
                outcome_kwargs = {
                    "status": final_status,
                    "result": final_message,
                    "joined": joined,
                    "saved_dialog_id": saved_dialog_id,
                    "account_id": account_id,
                    "membership_status": membership_status,
                    "membership_error": membership_error,
                    "join_event_peer_id": join_event_peer_id,
                    "campaign_pause_reason": campaign_pause_reason,
                    "task_failed": internal_error is not None,
                    "task_error": (
                        f"{type(internal_error).__name__}: {internal_error}"
                        if internal_error is not None
                        else None
                    ),
                }
                restricted_finalizer = getattr(
                    type(worker_db),
                    "finalize_join_slot_outcome_with_restriction",
                    None,
                )
                if account_restriction is not None and callable(restricted_finalizer):
                    restriction_code, restriction_message, restriction_details = (
                        account_restriction
                    )
                    restriction_kwargs = build_account_restriction_kwargs(
                        worker_db,
                        code=restriction_code,
                        message=restriction_message,
                        details=restriction_details,
                        account_id=account_id,
                    )
                    restriction_state = dict(
                        restricted_finalizer(
                            worker_db,
                            task_id,
                            slot_id,
                            restriction_kwargs=restriction_kwargs,
                            **outcome_kwargs,
                        )
                        or {}
                    )
                    restriction_finalized_atomically = True
                else:
                    current = worker_db.get_join_slot_context(campaign_id, slot_id)
                    if current and current.get("status") in {"queued", "running"}:
                        finalizer = getattr(
                            type(worker_db), "finalize_join_slot_outcome", None
                        )
                        if callable(finalizer):
                            finalizer(
                                worker_db,
                                task_id,
                                slot_id,
                                **outcome_kwargs,
                            )
                        else:  # pragma: no cover - compatibility test doubles
                            if membership_status is not None:
                                if membership_error is None:
                                    worker_db.set_saved_dialog_membership(
                                        saved_dialog_id,
                                        account_id,
                                        membership_status,
                                    )
                                else:
                                    worker_db.set_saved_dialog_membership(
                                        saved_dialog_id,
                                        account_id,
                                        membership_status,
                                        membership_error,
                                    )
                            if join_event_peer_id is not None:
                                worker_db.record_join_event(
                                    join_event_peer_id,
                                    "joined",
                                    campaign_id=campaign_id,
                                    saved_dialog_id=saved_dialog_id,
                                    account_id=account_id,
                                )
                            if campaign_pause_reason:
                                worker_db.pause_join_campaign(
                                    campaign_id, campaign_pause_reason
                                )
                            worker_db.finish_join_slot(
                                slot_id,
                                status=final_status,
                                result=final_message,
                                joined=joined,
                            )
                            worker_db.update_task_progress(task_id, 100)
            else:
                worker_db.update_task_progress(task_id, 100)

        if account_restriction is not None and not restriction_finalized_atomically:
            restriction_code, restriction_message, restriction_details = (
                account_restriction
            )
            restriction_state = activate_account_restriction(
                worker_db,
                code=restriction_code,
                message=restriction_message,
                details=restriction_details,
                account_id=account_id,
            )
        if restriction_state:
            request_cancel = getattr(queue_worker, "request_scope_cancellation", None)
            if callable(request_cancel):
                comment_ids = list(restriction_state.get("comment_campaign_ids") or [])
                join_ids = list(restriction_state.get("join_campaign_ids") or [])
                if not comment_ids:
                    comment_id = restriction_state.get("comment_campaign_id")
                    if comment_id:
                        comment_ids.append(comment_id)
                if not join_ids:
                    join_id = restriction_state.get("join_campaign_id") or campaign_id
                    if join_id:
                        join_ids.append(join_id)
                for comment_id in comment_ids:
                    request_cancel("comment_campaign", int(comment_id))
                for join_id in join_ids:
                    request_cancel("join_campaign", int(join_id))
        if internal_error is not None:
            raise internal_error

    return join_saved_slot

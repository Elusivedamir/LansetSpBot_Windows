from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, cast

from core.account_restriction import (
    RESTRICTION_CODES,
    activate_account_restriction,
    build_account_restriction_kwargs,
    get_account_restriction_state,
)
from core.campaign_schedule import utc_now
from core.exceptions import (
    DeferredTelegramError,
    NonRetryableTelegramError,
    TelegramOperationError,
)
from workers.flood_wait_guard import install_account_flood_wait
from workers.handlers.join_slot_decisions import (
    CampaignDisposition,
    CancellationDisposition,
    JoinErrorDisposition,
    JoinSlotPhase,
    LocalBanDisposition,
    campaign_disposition,
    cancellation_disposition,
    join_error_disposition,
    local_ban_disposition,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class JoinSlotOutcome:
    phase: JoinSlotPhase = JoinSlotPhase.PRECHECK
    joined: bool = False
    status: str = "failed"
    message: str = "Вступление не завершено"
    slot_deferred: bool = False
    internal_error: BaseException | None = None
    membership_status: str | None = None
    membership_error: str | None = None
    join_event_peer_id: int | None = None
    campaign_pause_reason: str | None = None
    account_restriction: tuple[str, str, dict[str, Any]] | None = None
    restriction_state: dict[str, Any] | None = None
    restriction_finalized_atomically: bool = False

    def finalizer_kwargs(self, *, saved_dialog_id: int, account_id: int) -> dict[str, Any]:
        return {
            "status": self.status,
            "result": self.message,
            "joined": self.joined,
            "saved_dialog_id": saved_dialog_id,
            "account_id": account_id,
            "membership_status": self.membership_status,
            "membership_error": self.membership_error,
            "join_event_peer_id": self.join_event_peer_id,
            "campaign_pause_reason": self.campaign_pause_reason,
            "task_failed": self.internal_error is not None,
            "task_error": (
                f"{type(self.internal_error).__name__}: {self.internal_error}"
                if self.internal_error is not None
                else None
            ),
        }


@dataclass(slots=True)
class JoinSlotRunner:
    task_id: int
    campaign_id: int
    slot_id: int
    saved_dialog_id: int
    account_id: int
    peer_id: int
    campaign_status: str
    context: dict[str, Any]
    strict_account_binding: bool
    get_setting: Callable[..., Any] | None
    as_int: Callable[[Any, int], int]
    queue_worker: Any
    config: Any
    worker_db: Any
    telegram: Any
    set_runtime: Callable[..., None]
    outcome: JoinSlotOutcome = field(default_factory=JoinSlotOutcome)

    @property
    def cancellation_scope(self) -> tuple[str, int]:
        return ("join_campaign", self.campaign_id)

    @property
    def title(self) -> str:
        return str(
            self.context.get("title")
            or self.context.get("username")
            or self.context.get("saved_dialog_id")
        )

    def target_is_locally_banned(self) -> bool:
        checker = getattr(type(self.worker_db), "is_channel_locally_banned", None)
        return bool(
            callable(checker)
            and checker(
                self.worker_db,
                self.peer_id,
                account_id=self.account_id,
            )
        )

    def current_campaign_status(self) -> str:
        current = self.worker_db.get_join_campaign(self.campaign_id)
        if current is None:
            return ""
        if isinstance(current, dict):
            return str(current.get("status") or "")
        return self.campaign_status

    def current_target_allows_rpc(self) -> bool:
        if self.target_is_locally_banned():
            return False
        if get_account_restriction_state(
            self.worker_db, account_id=self.account_id
        ).get("active"):
            return False
        if self.current_campaign_status() != "running":
            return False
        if self.strict_account_binding and callable(self.get_setting):
            current_account_id = self.as_int(
                self.get_setting("telegram.account_id", 0), 0
            )
            if current_account_id != self.account_id:
                return False
        return True

    def create_dispatch_barrier(self):
        factory = getattr(
            type(self.queue_worker), "create_scope_dispatch_barrier", None
        )
        if self.queue_worker is None or not callable(factory):
            return None
        return factory(
            self.queue_worker,
            self.cancellation_scope,
            ("task", self.task_id),
            ("channel", self.peer_id, self.account_id),
            pre_dispatch_check=self.current_target_allows_rpc,
        )

    def scope_is_cancelled(self) -> bool:
        callback = getattr(self.queue_worker, "is_scope_cancelled", None)
        if callable(callback) and (
            callback(*self.cancellation_scope)
            or callback("task", self.task_id)
            or callback("channel", self.peer_id, self.account_id)
        ):
            return True
        return self.current_campaign_status() != "running"

    def suspend_cancelled_slot(self, message: str) -> None:
        if self.current_campaign_status() in {"stopped", "completed", ""}:
            self.worker_db.cancel_join_slot(self.slot_id, result=message)
        else:
            self.worker_db.defer_join_slot(self.slot_id, utc_now(), message)

    def commit_unknown_join_ban(self, message: str) -> None:
        banner = getattr(type(self.worker_db), "ban_peer_locally", None)
        bound_banner = None
        if not callable(banner):
            bound_banner = getattr(self.worker_db, "ban_peer_locally", None)
            if not callable(bound_banner):
                return

        def mutation():
            if callable(banner):
                changed = banner(
                    self.worker_db,
                    self.peer_id,
                    message,
                    account_id=self.account_id,
                )
            else:
                fallback_banner = cast(Callable[..., Any], bound_banner)
                changed = fallback_banner(
                    self.peer_id,
                    message,
                    account_id=self.account_id,
                )
            if changed is False:
                raise RuntimeError(
                    "Ambiguous Join target could not be locally banned"
                )
            return changed

        runner = getattr(self.queue_worker, "cancel_scopes_and_run", None)
        if callable(runner):
            runner([("channel", self.peer_id, self.account_id)], mutation)
        else:  # pragma: no cover - compatibility for minimal test doubles
            mutation()

    async def perform_join(self) -> None:
        if self.target_is_locally_banned():
            self.outcome.status = "skipped"
            self.outcome.message = (
                "Цель пропущена: действует постоянная локальная блокировка"
            )
            self.outcome.membership_status = "uncertain"
            self.outcome.membership_error = self.outcome.message
            return

        if self.scope_is_cancelled():
            self.suspend_cancelled_slot(
                "Кампания приостановлена до начала вступления"
            )
            self.outcome.slot_deferred = True
            return

        guard = self.worker_db.get_join_guard(
            max_joins=max(1, int(self.context.get("max_per_hour") or 40)),
            min_interval_seconds=self.config.min_join_interval_seconds,
            window_seconds=3600,
            account_id=self.account_id,
        )
        if not guard["allowed"]:
            wait = max(30, int(guard["wait_seconds"]) + random.randint(5, 20))
            self.worker_db.defer_join_slot(
                self.slot_id,
                utc_now() + timedelta(seconds=wait),
                f"Лимит вступлений; повтор через {max(1, round(wait / 60))} мин",
            )
            self.outcome.slot_deferred = True
            return

        if self.scope_is_cancelled():
            self.suspend_cancelled_slot(
                "Кампания приостановлена до запроса вступления"
            )
            self.outcome.slot_deferred = True
            return

        self.outcome.phase = JoinSlotPhase.READY_TO_JOIN
        self.set_runtime(
            self.task_id,
            f"Вступление: {self.title}",
            account_id=self.account_id,
        )
        self.outcome.phase = JoinSlotPhase.JOIN_STARTED
        join_kwargs = {
            "username": self.context.get("username"),
            "invite_link": self.context.get("invite_link"),
        }
        join_barrier = self.create_dispatch_barrier()
        if join_barrier is not None:
            join_kwargs["dispatch_barrier"] = join_barrier
        newly_joined = await self.telegram.join_saved_dialog(**join_kwargs)
        self.outcome.phase = JoinSlotPhase.JOIN_CONFIRMED
        if not newly_joined:
            self.outcome.membership_status = "member"
            self.outcome.status = "already_member"
            self.outcome.message = "Уже состоял в канале/группе"
            return

        self.outcome.joined = True
        self.outcome.membership_status = "member"
        self.outcome.join_event_peer_id = self.peer_id
        self.outcome.status = "joined"
        self.outcome.message = "Вступление выполнено"

    def handle_cancelled(self, exc: asyncio.CancelledError) -> None:
        disposition = cancellation_disposition(self.outcome.phase)
        if disposition is CancellationDisposition.DEFER_BEFORE_DISPATCH:
            self.suspend_cancelled_slot(
                "Остановлено до отправки запроса вступления"
            )
            self.outcome.slot_deferred = True
        else:
            self.outcome.status = "uncertain"
            self.outcome.message = (
                "Остановка произошла во время вступления; нужна ручная проверка"
            )
            self.outcome.membership_status = "uncertain"
            self.outcome.membership_error = self.outcome.message
            self.commit_unknown_join_ban(self.outcome.message)
        self.outcome.internal_error = exc

    def handle_local_ban_before_dispatch(self) -> None:
        current_account_id = self.account_id
        if self.strict_account_binding and callable(self.get_setting):
            current_account_id = self.as_int(
                self.get_setting("telegram.account_id", 0), 0
            )
        disposition = local_ban_disposition(
            account_restricted=bool(
                get_account_restriction_state(
                    self.worker_db, account_id=self.account_id
                ).get("active")
            ),
            strict_account_binding=self.strict_account_binding,
            current_account_id=current_account_id,
            expected_account_id=self.account_id,
        )
        if disposition is LocalBanDisposition.ACCOUNT_RESTRICTED:
            self.outcome.status = "cancelled"
            self.outcome.message = (
                "Вступление отменено: аккаунт ограничен до отправки запроса"
            )
            self.outcome.membership_status = "failed"
            self.outcome.membership_error = self.outcome.message
            self.outcome.campaign_pause_reason = self.outcome.message
            return
        if disposition is LocalBanDisposition.ACCOUNT_MISMATCH:
            self.outcome.status = "failed"
            self.outcome.message = (
                "Вступление отменено: активный Telegram-аккаунт изменился"
            )
            self.outcome.membership_status = "failed"
            self.outcome.membership_error = self.outcome.message
            self.outcome.campaign_pause_reason = self.outcome.message
            return
        self.outcome.status = "skipped"
        self.outcome.message = (
            "Цель заблокирована до отправки запроса вступления"
        )
        self.outcome.membership_status = "uncertain"
        self.outcome.membership_error = self.outcome.message

    def handle_deferred(self, exc: DeferredTelegramError) -> None:
        code = str(getattr(exc, "code", "") or "")
        if code == "local_ban_before_dispatch":
            self.handle_local_ban_before_dispatch()
            return
        if code == "shutdown_before_dispatch":
            self.suspend_cancelled_slot(
                "Остановлено до отправки запроса вступления; слот сохранён"
            )
            self.outcome.slot_deferred = True
            return

        wait = max(1, int(exc.retry_after))
        retry_at = utc_now() + timedelta(seconds=wait)
        if code == "flood_wait_deferred" and self.account_id > 0:
            try:
                install_account_flood_wait(
                    queue_worker=self.queue_worker,
                    worker_db=self.worker_db,
                    account_id=self.account_id,
                    retry_at=retry_at,
                    code=code,
                    source_task_id=self.task_id,
                    wait_seconds=wait,
                )
            except Exception:
                log.exception(
                    "Could not persist account FloodWait; local embargo remains active"
                )
        changed = self.worker_db.defer_join_slot_and_set_network_wait(
            self.task_id,
            self.slot_id,
            self.campaign_id,
            scheduled_at=retry_at,
            slot_result=(
                "Ограничение Telegram; повтор через "
                f"{max(1, round(wait / 60))} мин"
            ),
            reason="Telegram временно ограничил запросы",
        )
        if not changed:
            raise RuntimeError(
                "Join slot was no longer eligible for network deferral"
            )
        self.outcome.slot_deferred = True

    def defer_network_unavailable(self) -> None:
        retry_at = utc_now() + timedelta(seconds=120)
        self.outcome.slot_deferred = True
        changed = self.worker_db.defer_join_slot_and_set_network_wait(
            self.task_id,
            self.slot_id,
            self.campaign_id,
            scheduled_at=retry_at,
            slot_result="Ожидание сети",
            reason="Нет соединения с Telegram; повтор через 2 минуты",
        )
        if not changed:
            raise RuntimeError(
                "Join slot was no longer eligible for network deferral"
            )

    def handle_non_retryable(self, exc: NonRetryableTelegramError) -> None:
        code = str(getattr(exc, "code", "") or "")
        disposition = join_error_disposition(
            code,
            restriction_codes=RESTRICTION_CODES,
        )
        if disposition is JoinErrorDisposition.NETWORK_WAIT:
            self.defer_network_unavailable()
            return
        if disposition is JoinErrorDisposition.JOIN_REQUESTED:
            self.outcome.status = "join_requested"
            self.outcome.message = (
                "Запрос на вступление отправлен; право писать ещё не получено"
            )
            self.outcome.membership_status = "join_requested"
            self.outcome.membership_error = None
            return
        if disposition is JoinErrorDisposition.ACCOUNT_MISMATCH:
            self.outcome.status = "failed"
            self.outcome.message = (
                "Кампания приостановлена: Telegram-сессия не совпадает "
                "с локальным аккаунтом"
            )
            self.outcome.membership_status = "failed"
            self.outcome.membership_error = str(exc)
            self.outcome.campaign_pause_reason = self.outcome.message
            return
        if disposition is JoinErrorDisposition.RESULT_UNKNOWN:
            self.outcome.status = "uncertain"
            self.outcome.message = (
                "Результат вступления неизвестен; цель локально заблокирована"
            )
            self.outcome.membership_status = "uncertain"
            self.outcome.membership_error = str(exc)
            self.commit_unknown_join_ban(self.outcome.message)
            return

        self.outcome.status = "failed"
        self.outcome.message = f"Не удалось вступить: {exc}"
        self.outcome.membership_status = "failed"
        self.outcome.membership_error = str(exc)
        if disposition is JoinErrorDisposition.ACCOUNT_RESTRICTION:
            restriction_message = (
                "Кампания остановлена: Telegram ограничил активность аккаунта"
            )
            self.outcome.campaign_pause_reason = restriction_message
            self.outcome.account_restriction = (
                code,
                restriction_message,
                {
                    "saved_dialog_id": self.saved_dialog_id,
                    "peer_id": self.peer_id,
                    "rpc_error": type(getattr(exc, "__cause__", None)).__name__,
                    "rpc_message": str(exc),
                },
            )
        elif disposition is JoinErrorDisposition.PAUSE_CAMPAIGN:
            self.outcome.campaign_pause_reason = self.outcome.message

    def handle_telegram_operation(self, exc: TelegramOperationError) -> None:
        if self.outcome.phase < JoinSlotPhase.JOIN_STARTED:
            retry_at = utc_now() + timedelta(seconds=120)
            self.outcome.message = (
                "Временная ошибка Telegram; повтор через 2 минуты"
            )
            self.outcome.slot_deferred = True
            changed = self.worker_db.defer_join_slot_and_set_network_wait(
                self.task_id,
                self.slot_id,
                self.campaign_id,
                scheduled_at=retry_at,
                slot_result=self.outcome.message,
                reason=self.outcome.message,
            )
            if not changed:
                raise RuntimeError(
                    "Join slot was no longer eligible for network deferral"
                )
            return

        self.outcome.status = "uncertain"
        self.outcome.message = (
            "Ошибка Telegram после начала вступления; цель локально заблокирована"
        )
        self.outcome.membership_status = "uncertain"
        self.outcome.membership_error = str(exc)
        self.commit_unknown_join_ban(self.outcome.message)
        self.outcome.internal_error = exc

    def handle_internal_error(self, exc: Exception) -> None:
        self.outcome.status = "failed"
        self.outcome.message = f"Внутренняя ошибка: {type(exc).__name__}: {exc}"
        self.outcome.membership_status = "failed"
        self.outcome.membership_error = str(exc)
        self.outcome.campaign_pause_reason = self.outcome.message
        self.outcome.internal_error = exc

    def finalize_outcome(self) -> None:
        if self.outcome.slot_deferred:
            self.worker_db.update_task_progress(self.task_id, 100)
            return

        outcome_kwargs = self.outcome.finalizer_kwargs(
            saved_dialog_id=self.saved_dialog_id,
            account_id=self.account_id,
        )
        restricted_finalizer = getattr(
            type(self.worker_db),
            "finalize_join_slot_outcome_with_restriction",
            None,
        )
        if self.outcome.account_restriction is not None and callable(
            restricted_finalizer
        ):
            code, message, details = self.outcome.account_restriction
            restriction_kwargs = build_account_restriction_kwargs(
                self.worker_db,
                code=code,
                message=message,
                details=details,
                account_id=self.account_id,
            )
            self.outcome.restriction_state = dict(
                restricted_finalizer(
                    self.worker_db,
                    self.task_id,
                    self.slot_id,
                    restriction_kwargs=restriction_kwargs,
                    **outcome_kwargs,
                )
                or {}
            )
            self.outcome.restriction_finalized_atomically = True
            return

        current = self.worker_db.get_join_slot_context(
            self.campaign_id, self.slot_id
        )
        if not current or current.get("status") not in {"queued", "running"}:
            return
        finalizer = getattr(type(self.worker_db), "finalize_join_slot_outcome", None)
        if callable(finalizer):
            finalizer(
                self.worker_db,
                self.task_id,
                self.slot_id,
                **outcome_kwargs,
            )
            return
        self.finalize_compatibility_outcome()

    def finalize_compatibility_outcome(self) -> None:
        if self.outcome.membership_status is not None:
            args = (
                self.saved_dialog_id,
                self.account_id,
                self.outcome.membership_status,
            )
            if self.outcome.membership_error is None:
                self.worker_db.set_saved_dialog_membership(*args)
            else:
                self.worker_db.set_saved_dialog_membership(
                    *args, self.outcome.membership_error
                )
        if self.outcome.join_event_peer_id is not None:
            self.worker_db.record_join_event(
                self.outcome.join_event_peer_id,
                "joined",
                campaign_id=self.campaign_id,
                saved_dialog_id=self.saved_dialog_id,
                account_id=self.account_id,
            )
        if self.outcome.campaign_pause_reason:
            self.worker_db.pause_join_campaign(
                self.campaign_id, self.outcome.campaign_pause_reason
            )
        self.worker_db.finish_join_slot(
            self.slot_id,
            status=self.outcome.status,
            result=self.outcome.message,
            joined=self.outcome.joined,
        )
        self.worker_db.update_task_progress(self.task_id, 100)

    def activate_restriction_if_needed(self) -> None:
        if (
            self.outcome.account_restriction is not None
            and not self.outcome.restriction_finalized_atomically
        ):
            code, message, details = self.outcome.account_restriction
            self.outcome.restriction_state = activate_account_restriction(
                self.worker_db,
                code=code,
                message=message,
                details=details,
                account_id=self.account_id,
            )
        state = self.outcome.restriction_state
        if not state:
            return
        request_cancel = getattr(
            self.queue_worker, "request_scope_cancellation", None
        )
        if not callable(request_cancel):
            return
        comment_ids = list(state.get("comment_campaign_ids") or [])
        join_ids = list(state.get("join_campaign_ids") or [])
        if not comment_ids:
            comment_id = state.get("comment_campaign_id")
            if comment_id:
                comment_ids.append(comment_id)
        if not join_ids:
            join_id = state.get("join_campaign_id") or self.campaign_id
            if join_id:
                join_ids.append(join_id)
        for comment_id in comment_ids:
            request_cancel("comment_campaign", int(comment_id))
        for join_id in join_ids:
            request_cancel("join_campaign", int(join_id))

    async def run(self) -> None:
        try:
            await self.perform_join()
        except asyncio.CancelledError as exc:
            self.handle_cancelled(exc)
        except DeferredTelegramError as exc:
            self.handle_deferred(exc)
        except NonRetryableTelegramError as exc:
            self.handle_non_retryable(exc)
        except TelegramOperationError as exc:
            self.handle_telegram_operation(exc)
        except Exception as exc:
            self.handle_internal_error(exc)
        finally:
            self.finalize_outcome()

        self.activate_restriction_if_needed()
        if self.outcome.internal_error is not None:
            raise self.outcome.internal_error


def _validate_target(context: dict[str, Any]) -> tuple[int, int, int]:
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
    return saved_dialog_id, account_id, peer_id


def _validate_account_binding(
    *,
    as_int: Callable[[Any, int], int],
    worker_db: Any,
    payload: dict[str, Any],
    campaign_id: int,
    slot_id: int,
    campaign_account_id: int,
    strict_account_binding: bool,
    get_setting: Callable[..., Any] | None,
) -> None:
    if not strict_account_binding or not callable(get_setting):
        return
    payload_account_id = as_int(payload.get("account_id"), 0)
    current_account_id = as_int(get_setting("telegram.account_id", 0), 0)
    if (
        campaign_account_id > 0
        and payload_account_id > 0
        and current_account_id > 0
        and campaign_account_id == payload_account_id
        and campaign_account_id == current_account_id
    ):
        return
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
                "Join campaign slot no longer exists",
                code="campaign_slot_missing",
            )
        context = dict(context)
        campaign_account_id = as_int(context.get("account_id"), 0)
        if get_account_restriction_state(
            worker_db, account_id=campaign_account_id
        ).get("active"):
            worker_db.stop_join_campaign(
                campaign_id,
                "Вступления заблокированы до проверки ограничения через @SpamBot",
            )
            return

        get_setting = getattr(worker_db, "get_setting", None)
        strict_account_binding = type(worker_db).__module__.startswith("storage.")
        _validate_account_binding(
            as_int=as_int,
            worker_db=worker_db,
            payload=payload,
            campaign_id=campaign_id,
            slot_id=slot_id,
            campaign_account_id=campaign_account_id,
            strict_account_binding=strict_account_binding,
            get_setting=get_setting if callable(get_setting) else None,
        )

        campaign_status = str(context.get("campaign_status") or "")
        disposition = campaign_disposition(campaign_status)
        if disposition is not CampaignDisposition.RUN:
            message = f"Кампания не активна: {campaign_status or 'unknown'}"
            if disposition is CampaignDisposition.DEFER:
                worker_db.defer_join_slot(slot_id, utc_now(), message)
            else:
                worker_db.cancel_join_slot(slot_id, result=message)
            return

        saved_dialog_id, account_id, peer_id = _validate_target(context)
        if not worker_db.mark_join_slot_running(slot_id, task_id):
            raise NonRetryableTelegramError(
                "Слот вступления недоступен",
                code="campaign_slot_unavailable",
            )

        runner = JoinSlotRunner(
            task_id=task_id,
            campaign_id=campaign_id,
            slot_id=slot_id,
            saved_dialog_id=saved_dialog_id,
            account_id=account_id,
            peer_id=peer_id,
            campaign_status=campaign_status,
            context=context,
            strict_account_binding=strict_account_binding,
            get_setting=get_setting if callable(get_setting) else None,
            as_int=as_int,
            queue_worker=queue_worker,
            config=config,
            worker_db=worker_db,
            telegram=telegram,
            set_runtime=set_runtime,
        )
        await runner.run()

    return join_saved_slot

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import inspect
import logging
import random
import time
from contextlib import nullcontext, suppress
from typing import Any, AsyncIterator

from telethon.errors import (
    ChannelPrivateError,
    ChatDiscussionUnallowedError,
    ChatAdminInviteRequiredError,
    ChatAdminRequiredError,
    FloodError,
    FloodPremiumWaitError,
    FloodTestPhoneWaitError,
    FloodWaitError,
    InviteHashEmptyError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    MessageIdInvalidError,
    MsgIdInvalidError,
    PeerFloodError,
    RPCError,
    UserAlreadyParticipantError,
    SlowModeWaitError,
    UnauthorizedError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, SendMessageRequest

try:
    from telethon.errors import SecurityError
except ImportError:  # pragma: no cover - old Telethon compatibility
    from telethon.errors.common import SecurityError

from core.performance import log_if_slow
from core.redaction import sanitize_text
from core.exceptions import (
    DeferredTelegramError,
    NonRetryableTelegramError,
    TelegramOperationError,
)
from services.telegram_error_translation import (
    PERMANENT_SEND_ERRORS,
    translate_permanent_send_error,
)
from services.telegram_transport_decisions import (
    CancellationAction,
    NetworkFailureAction,
    OperationFailureAction,
    RpcFailureAction,
    cancellation_action,
    network_failure_decision,
    operation_failure_decision,
    rpc_failure_action,
)

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class TelegramTransportMixin(_MixinHost):
    _MUTATING_REQUEST_TYPES = (
        SendMessageRequest,
        ImportChatInviteRequest,
        JoinChannelRequest,
    )

    @staticmethod
    def _group_allows_plain_text(entity: Any) -> bool:
        """Conservatively exclude groups where this account cannot post text.

        Dialog membership alone does not imply write access.  Use the peer flags
        already returned by Telegram so read-only, deactivated, migrated and
        restricted groups never become direct campaign targets.
        """
        if bool(getattr(entity, "left", False)):
            return False
        if bool(getattr(entity, "deactivated", False)):
            return False
        if getattr(entity, "migrated_to", None) is not None:
            return False
        if (
            bool(getattr(entity, "creator", False))
            or getattr(entity, "admin_rights", None) is not None
        ):
            return True
        if bool(getattr(entity, "gigagroup", False)):
            return False
        for rights_name in ("banned_rights", "default_banned_rights"):
            rights = getattr(entity, rights_name, None)
            if rights is not None and (
                bool(getattr(rights, "send_messages", False))
                or bool(getattr(rights, "send_plain", False))
            ):
                return False
        return True

    @staticmethod
    def _interruption_requested() -> bool:
        try:
            from PySide6.QtCore import QThread

            thread = QThread.currentThread()
            return bool(thread and thread.isInterruptionRequested())
        except Exception:
            return False

    async def safe_sleep(self, seconds: float) -> bool:
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            if self._interruption_requested():
                log.info("Telegram wait interrupted by shutdown request")
                return False
            step = min(0.5, remaining)
            await asyncio.sleep(step)
            remaining -= step
        return not self._interruption_requested()

    async def _await_interruptible(self, awaitable, *, timeout: float | None):
        """Await a Telegram operation while polling cooperative QThread stop."""
        task = asyncio.ensure_future(awaitable)
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(0.001, timeout)
        try:
            while not task.done():
                if self._interruption_requested():
                    raise asyncio.CancelledError
                wait_seconds = 0.25
                if deadline is not None:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    wait_seconds = min(wait_seconds, remaining)
                await asyncio.wait((task,), timeout=wait_seconds)
            return task.result()
        except BaseException:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            else:
                # The operation can fail in the narrow race between the loop's
                # done() check and a timeout/shutdown exception. Retrieve that
                # result so asyncio never emits "Future exception was never retrieved".
                with suppress(asyncio.CancelledError, Exception):
                    task.result()
            raise

    async def _report_status(self, text: str) -> None:
        callback = getattr(self, "_status_callback", None)
        if callback is None:
            return
        try:
            result = callback(str(text or ""))
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("Could not publish Telegram runtime status")

    async def _wait_with_status(self, label: str, seconds: int) -> bool:
        """Wait cooperatively and expose a readable countdown to the GUI."""
        remaining = max(0, int(seconds))
        while remaining > 0:
            await self._report_status(f"{label}: продолжение через {remaining} сек")
            step = 1 if remaining <= 60 else min(5, remaining)
            if not await self.safe_sleep(step):
                await self._report_status("")
                return False
            remaining -= step
        await self._report_status("")
        return True

    def _protected_flood_wait_seconds(self, raw_wait: int) -> int:
        """Return a safe persisted retry delay for Telegram flood control.

        A short FloodWait gets the requested human-like three-to-five-minute
        pause. A longer server interval always wins and keeps the existing
        randomized safety buffer, so automatic continuation never resumes
        before Telegram allows it.
        """

        requested = max(0, int(raw_wait))
        buffer_min = int(getattr(self, "FLOOD_WAIT_BUFFER_MIN_SECONDS", 30))
        buffer_max = max(
            buffer_min,
            int(getattr(self, "FLOOD_WAIT_BUFFER_MAX_SECONDS", 45)),
        )
        auto_resume_min = int(
            getattr(self, "FLOOD_WAIT_AUTO_RESUME_MIN_SECONDS", 3 * 60)
        )
        auto_resume_max = max(
            auto_resume_min,
            int(getattr(self, "FLOOD_WAIT_AUTO_RESUME_MAX_SECONDS", 5 * 60)),
        )
        buffer_seconds = max(
            buffer_min,
            min(
                buffer_max,
                int(
                    random.randint(
                        buffer_min,
                        buffer_max,
                    )
                ),
            ),
        )
        auto_resume_floor = max(
            auto_resume_min,
            min(
                auto_resume_max,
                int(
                    random.randint(
                        auto_resume_min,
                        auto_resume_max,
                    )
                ),
            ),
        )
        return max(requested + buffer_seconds, auto_resume_floor)

    async def connect(self) -> None:
        """Connect an already-authorized session without console prompts."""
        started = time.monotonic()
        if not self.settings.configured:
            raise NonRetryableTelegramError(
                "Telegram API_ID/API_HASH are not configured",
                code="telegram_not_configured",
            )
        self._connected = False
        self._authorized_user = None
        try:
            if not self.client.is_connected():
                await self._await_interruptible(self.client.connect(), timeout=30.0)
            authorized = await self._await_interruptible(
                self.client.is_user_authorized(), timeout=40.0
            )
            if not authorized:
                await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
                raise NonRetryableTelegramError(
                    "Telegram session is not authorized. Authorize it before starting the queue.",
                    code="authorization_required",
                )
            me = await self._await_interruptible(self.client.get_me(), timeout=40.0)
            if me is None:
                await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
                raise NonRetryableTelegramError(
                    "Telegram session is not authorized. Authorize it before starting the queue.",
                    code="authorization_required",
                )
            actual_account_id = int(getattr(me, "id", 0) or 0)
            expected_account_id = int(
                getattr(self.settings, "expected_account_id", 0) or 0
            )
            if actual_account_id <= 0 or expected_account_id <= 0:
                await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
                raise NonRetryableTelegramError(
                    "Состояние Telegram-аккаунта не подтверждено в локальной базе. "
                    "Перезапустите приложение или подключите аккаунт заново.",
                    code="account_state_mismatch",
                    details={
                        "actual_account_id": actual_account_id,
                        "expected_account_id": expected_account_id,
                    },
                )
            if actual_account_id != expected_account_id:
                await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
                raise NonRetryableTelegramError(
                    "Telegram-сессия принадлежит другому аккаунту. Рабочие операции "
                    "заблокированы до повторного подключения аккаунта.",
                    code="account_state_mismatch",
                    details={
                        "actual_account_id": actual_account_id,
                        "expected_account_id": expected_account_id,
                    },
                )
            self._authorized_user = me
            self._connected = True
            self._last_authorization_check = time.monotonic()
            log.info("Connected to Telegram using an authorized session")
        except asyncio.TimeoutError as exc:
            self._connected = False
            raise TelegramOperationError("Telegram connection timed out") from exc
        except UnauthorizedError as exc:
            self._connected = False
            raise NonRetryableTelegramError(
                "Telegram session is no longer authorized. Authorize it again.",
                code="authorization_required",
                details={
                    "rpc_error": type(exc).__name__,
                    "rpc_message": str(exc),
                },
            ) from exc
        except (TelegramOperationError, NonRetryableTelegramError):
            self._connected = False
            raise
        except SecurityError as exc:
            self._connected = False
            raise NonRetryableTelegramError(
                "Telegram security check failed; synchronize system date/time",
                code="security_time_sync",
            ) from exc
        except Exception as exc:
            self._connected = False
            raise TelegramOperationError(
                sanitize_text(
                    f"Connection failed: {exc}",
                    secrets=(
                        getattr(self.settings, "proxy_password", None),
                        getattr(self.settings, "proxy_secret", None),
                        getattr(self.settings, "api_hash", None),
                        getattr(self.settings, "phone", None),
                    ),
                )
            ) from exc
        finally:
            # A timed-out connect or authorization probe may leave Telethon's
            # transport alive.  Always close a partially initialized socket so
            # ensure_connected() cannot later mistake it for an authorized one.
            if not self._connected and self.client.is_connected():
                try:
                    await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
                except asyncio.CancelledError:
                    # The outer wait is already bounded. Propagate shutdown
                    # immediately rather than starting an unbounded shielded
                    # disconnect that could keep the worker alive indefinitely.
                    log.info("Telegram connect cleanup cancelled by shutdown")
                    raise
                except Exception:
                    log.exception("Could not clean up failed Telegram connection")
            log_if_slow(
                log,
                "telegram_connect",
                started,
                threshold_seconds=10.0,
                connected=self._connected,
            )

    async def ensure_connected(self) -> None:
        if self.client.is_connected():
            # Revalidate periodically instead of trusting a live socket forever.
            # Revoked/corrupted sessions then surface as authorization_required
            # rather than an unrelated generic RPC failure later in the task.
            identity_probe = getattr(self.client, "get_me", None)
            identity_missing = getattr(self, "_authorized_user", None) is None
            probe_due = time.monotonic() - float(
                getattr(self, "_last_authorization_check", 0.0) or 0.0
            ) >= float(getattr(self, "AUTHORIZATION_RECHECK_SECONDS", 900.0))
            if callable(identity_probe) and (identity_missing or probe_due):
                me = await self._await_interruptible(identity_probe(), timeout=40.0)
                if me is None:
                    self._connected = False
                    await self.disconnect()
                    raise NonRetryableTelegramError(
                        "Telegram session is not authorized. Authorize it before starting the queue.",
                        code="authorization_required",
                    )
                actual_account_id = int(getattr(me, "id", 0) or 0)
                expected_account_id = int(
                    getattr(self.settings, "expected_account_id", 0) or 0
                )
                if actual_account_id <= 0 or actual_account_id != expected_account_id:
                    self._connected = False
                    await self.disconnect()
                    raise NonRetryableTelegramError(
                        "Telegram session belongs to a different account; reconnect it before starting the queue.",
                        code="account_state_mismatch",
                        details={
                            "actual_account_id": actual_account_id,
                            "expected_account_id": expected_account_id,
                        },
                    )
                self._authorized_user = me
                self._last_authorization_check = time.monotonic()
            self._connected = True
            return
        self._connected = False
        log.warning("Telegram socket is disconnected; reconnecting")
        await self.connect()

    async def get_connected_identity(self):
        """Return the already-validated account without duplicate health RPCs."""

        await self.ensure_connected()
        identity = getattr(self, "_authorized_user", None)
        if identity is None:
            raise NonRetryableTelegramError(
                "Telegram session identity is unavailable",
                code="authorization_required",
            )
        return identity

    async def disconnect(self) -> None:
        try:
            if self.client and self.client.is_connected():
                await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
        except asyncio.TimeoutError:
            log.error("Telegram disconnect timed out")
        except Exception:
            log.exception("Error disconnecting Telegram")
        finally:
            self._connected = False
            self._last_authorization_check = 0.0
            self._authorized_user = None

    async def reconnect(self) -> None:
        await self.disconnect()
        if not await self.safe_sleep(1.0):
            raise asyncio.CancelledError
        await self.connect()

    async def start(self) -> None:
        await self.connect()

    async def stop(self) -> None:
        await self.disconnect()

    async def _execute_once(
        self,
        method,
        args,
        kwargs,
        *,
        dispatch_barrier,
        attempt_state,
    ):
        """Dispatch one call while preserving the exact request boundary."""

        def mark_dispatched(request):
            if isinstance(request, self._MUTATING_REQUEST_TYPES):
                attempt_state["request_dispatched"] = True
            if dispatch_barrier is not None:
                return dispatch_barrier.dispatch(request)
            return None

        async def start_fallback_call(*, timeout: float | None):
            dispatch_context = (
                dispatch_barrier.dispatch(None)
                if dispatch_barrier is not None
                else nullcontext()
            )
            with dispatch_context:
                # A fallback client cannot expose the underlying MTProto request,
                # so handing over its coroutine is the conservative boundary.
                attempt_state["request_dispatched"] = True
                operation = method(*args, **kwargs)
                task = asyncio.ensure_future(operation)
                await asyncio.sleep(0)
            return await self._await_interruptible(task, timeout=timeout)

        await self.ensure_connected()
        if self._interruption_requested():
            raise asyncio.CancelledError
        if getattr(self.client, "_marlen_request_pacing", False):
            observer = getattr(self.client, "observe_requests", None)
            if callable(observer):
                with observer(mark_dispatched):
                    return await self._await_interruptible(
                        method(*args, **kwargs), timeout=None
                    )
            return await start_fallback_call(timeout=None)

        request_slot = getattr(self.limiter, "request_slot", None)
        if callable(request_slot):
            async with request_slot():
                if self._interruption_requested():
                    raise asyncio.CancelledError
                return await start_fallback_call(timeout=30.0)

        await self.limiter.acquire()
        if self._interruption_requested():
            raise asyncio.CancelledError
        return await start_fallback_call(timeout=30.0)

    async def _raise_flood_wait(self, exc: BaseException) -> None:
        raw_wait = max(0, int(getattr(exc, "seconds", 0) or 0))
        wait_time = self._protected_flood_wait_seconds(raw_wait)
        log.warning(
            "Telegram FloodWait: requested=%ss, protected_wait=%ss",
            raw_wait,
            wait_time,
        )
        await self._report_status(
            "Ограничение Telegram: автоматическое продолжение через "
            f"{wait_time} сек"
        )
        raise DeferredTelegramError(
            "Telegram FloodWait",
            code="flood_wait_deferred",
            retry_after=wait_time,
        ) from exc

    async def _raise_slow_mode_wait(self, exc: BaseException) -> None:
        raw_wait = max(0, int(getattr(exc, "seconds", 0) or 0))
        wait_time = raw_wait + random.randint(
            self.FLOOD_WAIT_BUFFER_MIN_SECONDS,
            self.FLOOD_WAIT_BUFFER_MAX_SECONDS,
        )
        log.warning(
            "Telegram SlowModeWait: requested=%ss, protected_wait=%ss",
            raw_wait,
            wait_time,
        )
        await self._report_status(
            f"Медленный режим чата: задача отложена на {wait_time} сек"
        )
        raise DeferredTelegramError(
            "Telegram SlowModeWait",
            code="slow_mode_wait_deferred",
            retry_after=wait_time,
        ) from exc

    def _raise_generic_flood(self, exc: BaseException) -> None:
        raw_wait = max(0, int(getattr(exc, "seconds", 0) or 0))
        if raw_wait <= 0:
            raise NonRetryableTelegramError(
                "Telegram flood control returned no retry interval; account activity stopped",
                code="peer_flood",
                details={
                    "rpc_error": type(exc).__name__,
                    "rpc_message": str(exc),
                },
            ) from exc
        wait_time = self._protected_flood_wait_seconds(raw_wait)
        log.warning(
            "Telegram generic FloodError: requested=%ss, protected_wait=%ss",
            raw_wait,
            wait_time,
        )
        raise DeferredTelegramError(
            "Telegram generic FloodError",
            code="flood_wait_deferred",
            retry_after=wait_time,
        ) from exc

    async def _retry_after_network_failure(
        self,
        exc: BaseException,
        *,
        retry_network: bool,
        request_dispatched: bool,
        network_attempts: int,
        max_network_attempts: int,
        unknown_result_code: str,
    ) -> int:
        self._connected = False
        decision = network_failure_decision(
            retry_network=retry_network,
            request_dispatched=request_dispatched,
            attempts=network_attempts,
            max_attempts=max_network_attempts,
        )
        if decision.action is NetworkFailureAction.UNCERTAIN:
            raise NonRetryableTelegramError(
                "Telegram delivery result is unknown after a network failure; review before retry",
                code=unknown_result_code,
            ) from exc
        if decision.action is NetworkFailureAction.EXHAUSTED:
            raise NonRetryableTelegramError(
                "Telegram network unavailable after "
                f"{decision.attempts} attempts: {exc}",
                code="network_unavailable",
            ) from exc

        log.warning("Transient Telegram network error, reconnecting: %s", exc)
        await self.disconnect()
        if not await self.safe_sleep(min(2**decision.attempts, 5)):
            action = cancellation_action(
                retry_network=retry_network,
                request_dispatched=request_dispatched,
            )
            if action is CancellationAction.DEFER_BEFORE_DISPATCH:
                raise DeferredTelegramError(
                    "Operation stopped before Telegram request dispatch",
                    code="shutdown_before_dispatch",
                    retry_after=1,
                )
            raise asyncio.CancelledError
        return decision.attempts

    async def _retry_after_operation_failure(
        self,
        exc: TelegramOperationError,
        *,
        retry_network: bool,
        request_dispatched: bool,
        network_attempts: int,
        max_network_attempts: int,
    ) -> int:
        decision = operation_failure_decision(
            request_dispatched=request_dispatched,
            attempts=network_attempts,
            max_attempts=max_network_attempts,
        )
        if decision.action is OperationFailureAction.PROPAGATE:
            raise exc
        self._connected = False
        if decision.action is OperationFailureAction.EXHAUSTED:
            raise NonRetryableTelegramError(
                "Telegram network unavailable after "
                f"{decision.attempts} attempts: {exc}",
                code="network_unavailable",
            ) from exc

        log.warning("Telegram pre-dispatch failure, reconnecting: %s", exc)
        await self.disconnect()
        if not await self.safe_sleep(min(2**decision.attempts, 5)):
            if not retry_network:
                raise DeferredTelegramError(
                    "Operation stopped before Telegram request dispatch",
                    code="shutdown_before_dispatch",
                    retry_after=1,
                )
            raise asyncio.CancelledError
        return decision.attempts

    def _raise_rpc_error(
        self,
        exc: RPCError,
        *,
        retry_network: bool,
        request_dispatched: bool,
        unknown_result_code: str,
    ) -> None:
        rpc_code = int(getattr(exc, "code", 0) or 0)
        rpc_name = type(exc).__name__
        rpc_message = str(exc)
        action = rpc_failure_action(
            rpc_code=rpc_code,
            rpc_name=rpc_name,
            rpc_text=rpc_message,
            retry_network=retry_network,
            request_dispatched=request_dispatched,
        )
        details: dict[str, object] = {
            "rpc_error": rpc_name,
            "rpc_message": rpc_message,
        }
        if action is RpcFailureAction.USER_RESTRICTED:
            raise NonRetryableTelegramError(
                "Telegram restricted this user account",
                code="user_restricted",
                details=details,
            ) from exc
        if action is RpcFailureAction.AUTH_KEY_DUPLICATED:
            self._connected = False
            raise NonRetryableTelegramError(
                "Telegram invalidated the duplicated authorization key",
                code="auth_key_duplicated",
                details=details,
            ) from exc
        if action is RpcFailureAction.UNCERTAIN:
            raise NonRetryableTelegramError(
                "Telegram delivery result is unknown after a transient RPC failure; "
                "review before retry",
                code=unknown_result_code,
                details=details,
            ) from exc
        if action is RpcFailureAction.DEFER:
            raise DeferredTelegramError(
                f"Temporary Telegram RPC error: {exc}",
                code="telegram_rpc_deferred",
                retry_after=random.randint(30, 90),
            ) from exc
        raise TelegramOperationError(f"Telegram RPC error: {exc}") from exc

    async def execute(
        self,
        method,
        *args,
        retry_network: bool = True,
        unknown_result_code: str = "delivery_result_unknown",
        dispatch_barrier=None,
        **kwargs,
    ):
        """Run one Telegram call with timeout and bounded, ambiguity-safe retries."""

        network_attempts = 0
        max_network_attempts = 3
        while True:
            attempt_state = {"request_dispatched": False}
            try:
                return await self._execute_once(
                    method,
                    args,
                    kwargs,
                    dispatch_barrier=dispatch_barrier,
                    attempt_state=attempt_state,
                )
            except asyncio.CancelledError as exc:
                action = cancellation_action(
                    retry_network=retry_network,
                    request_dispatched=attempt_state["request_dispatched"],
                )
                if action is CancellationAction.DEFER_BEFORE_DISPATCH:
                    raise DeferredTelegramError(
                        "Operation stopped before Telegram request dispatch",
                        code="shutdown_before_dispatch",
                        retry_after=1,
                    ) from exc
                raise
            except (
                FloodWaitError,
                FloodPremiumWaitError,
                FloodTestPhoneWaitError,
            ) as exc:
                await self._raise_flood_wait(exc)
            except SlowModeWaitError as exc:
                await self._raise_slow_mode_wait(exc)
            except FloodError as exc:
                self._raise_generic_flood(exc)
            except UserAlreadyParticipantError:
                return False
            except PeerFloodError as exc:
                raise NonRetryableTelegramError(
                    "Telegram PeerFlood restriction", code="peer_flood"
                ) from exc
            except (MessageIdInvalidError, MsgIdInvalidError) as exc:
                raise NonRetryableTelegramError(
                    "Invalid Telegram message ID",
                    code="message_id_invalid",
                    details={
                        "rpc_error": type(exc).__name__,
                        "rpc_message": str(exc),
                    },
                ) from exc
            except ChatDiscussionUnallowedError as exc:
                raise NonRetryableTelegramError(
                    "Comments are disabled for this post",
                    code="comments_disabled",
                    details={
                        "rpc_error": type(exc).__name__,
                        "rpc_message": str(exc),
                    },
                ) from exc
            except ChannelPrivateError as exc:
                raise NonRetryableTelegramError(
                    "Telegram channel is private or inaccessible",
                    code="channel_private",
                    details={
                        "rpc_error": type(exc).__name__,
                        "rpc_message": str(exc),
                    },
                ) from exc
            except PERMANENT_SEND_ERRORS as exc:
                raise translate_permanent_send_error(exc) from exc
            except SecurityError as exc:
                self._connected = False
                raise NonRetryableTelegramError(
                    "Telegram security check failed; synchronize system date/time",
                    code="security_time_sync",
                ) from exc
            except UnauthorizedError as exc:
                self._connected = False
                await self.disconnect()
                raise NonRetryableTelegramError(
                    "Telegram session is no longer authorized. Authorize it again.",
                    code="authorization_required",
                    details={
                        "rpc_error": type(exc).__name__,
                        "rpc_message": str(exc),
                    },
                ) from exc
            except InviteRequestSentError as exc:
                raise NonRetryableTelegramError(
                    "Telegram accepted a request to join, but membership is not active yet",
                    code="join_requested",
                    details={
                        "rpc_error": type(exc).__name__,
                        "rpc_message": str(exc),
                    },
                ) from exc
            except (
                ChatAdminInviteRequiredError,
                ChatAdminRequiredError,
                InviteHashEmptyError,
                InviteHashExpiredError,
                InviteHashInvalidError,
            ) as exc:
                raise NonRetryableTelegramError(
                    f"Telegram permission/invite error: {exc}",
                    code="permission_denied",
                    details={
                        "rpc_error": type(exc).__name__,
                        "rpc_message": str(exc),
                    },
                ) from exc
            except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
                network_attempts = await self._retry_after_network_failure(
                    exc,
                    retry_network=retry_network,
                    request_dispatched=attempt_state["request_dispatched"],
                    network_attempts=network_attempts,
                    max_network_attempts=max_network_attempts,
                    unknown_result_code=unknown_result_code,
                )
            except DeferredTelegramError:
                raise
            except TelegramOperationError as exc:
                network_attempts = await self._retry_after_operation_failure(
                    exc,
                    retry_network=retry_network,
                    request_dispatched=attempt_state["request_dispatched"],
                    network_attempts=network_attempts,
                    max_network_attempts=max_network_attempts,
                )
            except RPCError as exc:
                self._raise_rpc_error(
                    exc,
                    retry_network=retry_network,
                    request_dispatched=attempt_state["request_dispatched"],
                    unknown_result_code=unknown_result_code,
                )

    async def _iter_with_timeout(
        self,
        iterator: AsyncIterator[Any],
        timeout: float = 40.0,
        *,
        dispatch_barrier=None,
    ):
        """Iterate Telethon paginators with FloodWait and network recovery."""
        flood_attempts = 0
        network_attempts = 0
        while True:
            if self._interruption_requested():
                raise asyncio.CancelledError
            try:
                if dispatch_barrier is None:
                    item = await self._await_interruptible(
                        iterator.__anext__(), timeout=timeout
                    )
                else:
                    # Compatibility path for non-Paced clients.  Production's
                    # PacedTelegramClient uses observe_requests above and checks
                    # every actual paginator MTProto request.  For alternate
                    # clients, keep the barrier until the coroutine has entered
                    # its request path.
                    with dispatch_barrier.dispatch(None):
                        operation = asyncio.ensure_future(iterator.__anext__())
                        await asyncio.sleep(0)
                    item = await self._await_interruptible(operation, timeout=timeout)
                flood_attempts = 0
                network_attempts = 0
                yield item
            except StopAsyncIteration:
                return
            except (
                FloodWaitError,
                FloodPremiumWaitError,
                FloodTestPhoneWaitError,
            ) as exc:
                raw_wait = max(0, int(exc.seconds))
                flood_attempts += 1
                wait_time = self._protected_flood_wait_seconds(raw_wait)
                log.warning(
                    "Telegram pagination FloodWait #%s: requested=%ss, protected_wait=%ss",
                    flood_attempts,
                    raw_wait,
                    wait_time,
                )
                raise DeferredTelegramError(
                    "Telegram pagination FloodWait",
                    code="flood_wait_deferred",
                    retry_after=wait_time,
                ) from exc
            except SlowModeWaitError as exc:
                raw_wait = max(0, int(exc.seconds))
                wait_time = raw_wait + random.randint(
                    self.FLOOD_WAIT_BUFFER_MIN_SECONDS,
                    self.FLOOD_WAIT_BUFFER_MAX_SECONDS,
                )
                raise DeferredTelegramError(
                    "Telegram pagination SlowModeWait",
                    code="slow_mode_wait_deferred",
                    retry_after=wait_time,
                ) from exc
            except FloodError as exc:
                raw_wait = max(0, int(getattr(exc, "seconds", 0) or 0))
                if raw_wait <= 0:
                    raise NonRetryableTelegramError(
                        "Telegram pagination flood control returned no retry interval",
                        code="peer_flood",
                        details={
                            "rpc_error": type(exc).__name__,
                            "rpc_message": str(exc),
                        },
                    ) from exc
                wait_time = self._protected_flood_wait_seconds(raw_wait)
                raise DeferredTelegramError(
                    "Telegram pagination generic FloodError",
                    code="flood_wait_deferred",
                    retry_after=wait_time,
                ) from exc
            except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
                self._connected = False
                network_attempts += 1
                if network_attempts >= 3:
                    raise NonRetryableTelegramError(
                        f"Telegram pagination unavailable after {network_attempts} attempts: {exc}",
                        code="network_unavailable",
                    ) from exc
                log.warning("Pagination network error, reconnecting: %s", exc)
                await self.disconnect()
                if not await self.safe_sleep(min(2**network_attempts, 5)):
                    raise asyncio.CancelledError
                await self.ensure_connected()

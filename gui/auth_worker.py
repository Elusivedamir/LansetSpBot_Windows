from __future__ import annotations

import asyncio
import logging
import traceback
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, TypeVar
import warnings

from PySide6.QtCore import QThread, Signal
from telethon import connection
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from core.account_state import persist_account_state
from core.rate_limiter import RateLimiter
from core.redaction import sanitize_text
from core.version import APP_NAME, __version__
from services.encrypted_telethon_session import EncryptedSQLiteSession
from services.paced_telegram_client import PacedTelegramClient as TelegramClient
from services.telegram_service import TelegramService
from services.mtproxy_faketls import ConnectionTcpMTProxyFakeTLS

log = logging.getLogger(__name__)

T = TypeVar("T")


class TemporaryTelegramRequestError(RuntimeError):
    """A short-lived Telegram request failure after bounded retries."""


class TelegramAuthWorker(QThread):
    """Run bounded, cooperatively cancellable phone authorization off the GUI thread."""

    authorized = Signal(dict)
    code_sent = Signal(str)
    password_required = Signal()
    failed = Signal(str)
    temporary_failed = Signal(str)

    NETWORK_TIMEOUT_SECONDS = 45.0
    CODE_REQUEST_TIMEOUT_SECONDS = 90.0
    INTERRUPT_POLL_SECONDS = 0.2
    TRANSIENT_RETRY_ATTEMPTS = 3
    TRANSIENT_RETRY_DELAY_SECONDS = 1.0

    def __init__(
        self,
        *,
        mode: str,
        settings: dict,
        session_dir: Path,
        database_path: Path | None = None,
        code: str = "",
        phone_code_hash: str = "",
        password: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self.settings = dict(settings)
        self.session_dir = Path(session_dir)
        self.database_path = Path(database_path) if database_path is not None else None
        self.code = code.strip()
        self.phone_code_hash = phone_code_hash
        self.password = password

    def run(self) -> None:
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            # Normal application shutdown or user-requested cancellation.
            return
        except TimeoutError:
            log.warning("Telegram did not answer in time")
            self.failed.emit("Telegram не ответил вовремя. Проверьте сеть или proxy")
        except PhoneNumberInvalidError:
            log.warning("Telegram rejected the phone number")
            self.failed.emit("Telegram не принял номер телефона")
        except PhoneCodeInvalidError:
            log.warning("Telegram rejected the confirmation code")
            self.failed.emit("Неверный код подтверждения")
        except PhoneCodeExpiredError:
            log.warning("The Telegram confirmation code expired")
            self.failed.emit("Код подтверждения устарел. Запросите новый код")
        except PasswordHashInvalidError:
            log.warning("The two-factor password was rejected")
            self.failed.emit("Неверный пароль двухэтапной аутентификации")
        except FloodWaitError as exc:
            log.warning("Telegram FloodWait during authorization: %ss", exc.seconds)
            self.failed.emit(
                f"Telegram временно ограничил вход. Повторите через {exc.seconds} сек."
            )
        except TemporaryTelegramRequestError as exc:
            log.warning(
                "Temporary Telegram authorization failure: %s: %s",
                type(exc).__name__,
                sanitize_text(str(exc)),
            )
            self.temporary_failed.emit(self._safe_error_text(exc))
        except Exception as exc:
            # An authorization failure the operator can see must also be
            # reconstructible afterwards. Without this the dialog was the only
            # record: nothing reached marlen.log, so a report carried the
            # message and no traceback.
            log.error(
                "Telegram authorization failed: %s: %s\n%s",
                type(exc).__name__,
                sanitize_text(str(exc)),
                sanitize_text(traceback.format_exc()),
            )
            self.failed.emit(f"Ошибка подключения: {self._safe_error_text(exc)}")

    def _safe_error_text(self, exc: BaseException) -> str:
        sensitive_setting_keys = (
            "telegram.api_hash",
            "telegram.proxy_password",
            "telegram.proxy_username",
            "telegram.proxy_secret",
            "telegram.phone",
            # Compatibility with older/lightweight callers.
            "api_hash",
            "proxy_password",
            "proxy_username",
            "proxy_secret",
            "phone",
        )
        secrets = [self.settings.get(key) for key in sensitive_setting_keys]
        secrets.extend((self.code, self.password, self.phone_code_hash))
        return sanitize_text(
            f"{type(exc).__name__}: {exc}",
            secrets=secrets,
        )

    def request_stop(self) -> None:
        """Ask the worker to cancel the current Telethon await safely."""
        self.requestInterruption()

    async def _await_interruptible(
        self, awaitable: Awaitable[T], *, timeout: float | None = None
    ) -> T:
        """Await a network operation while polling QThread interruption.

        Telethon calls may otherwise leave this QThread alive while Qt is tearing down
        its parent widget. The helper imposes a finite deadline and cancels the pending
        coroutine when application shutdown is requested.
        """
        if self.isInterruptionRequested():
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise asyncio.CancelledError

        limit = float(timeout or self.NETWORK_TIMEOUT_SECONDS)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.1, limit)
        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                if self.isInterruptionRequested():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise asyncio.CancelledError
                remaining = deadline - loop.time()
                if remaining <= 0:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise TimeoutError
                done, _ = await asyncio.wait(
                    {task}, timeout=min(self.INTERRUPT_POLL_SECONDS, remaining)
                )
                if task in done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    @staticmethod
    def _is_transient_request_error(exc: BaseException) -> bool:
        """Recognize Telethon's one-shot temporary request failure precisely."""
        return isinstance(exc, ValueError) and "Request was unsuccessful" in str(exc)

    async def _sleep_interruptible(self, seconds: float) -> None:
        if seconds <= 0:
            return
        await self._await_interruptible(
            asyncio.sleep(seconds),
            timeout=max(seconds + 1.0, self.NETWORK_TIMEOUT_SECONDS),
        )

    async def _with_transient_retries(
        self,
        factory: Callable[[], Awaitable[T]],
        *,
        timeout: float | None = None,
    ) -> T:
        """Retry only the known transient Telethon request error.

        Authentication errors, invalid codes, FloodWait and other RPC errors are not
        swallowed and keep their existing behavior.
        """
        attempts = max(1, int(self.TRANSIENT_RETRY_ATTEMPTS))
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await self._await_interruptible(factory(), timeout=timeout)
            except Exception as exc:
                if not self._is_transient_request_error(exc):
                    raise
                last_error = exc
                if attempt >= attempts:
                    break
                await self._sleep_interruptible(
                    self.TRANSIENT_RETRY_DELAY_SECONDS * attempt
                )
        raise TemporaryTelegramRequestError(
            "Telegram временно не ответил при проверке подключения. "
            "Сохранённая сессия не удалена; повторите проверку позже."
        ) from last_error

    def _proxy_settings(self):
        return SimpleNamespace(
            proxy_enabled=str(self.settings.get("telegram.proxy_enabled", "0")).lower()
            in {"1", "true", "yes", "on"},
            proxy_type=self.settings.get("telegram.proxy_type", "SOCKS5"),
            proxy_host=self.settings.get("telegram.proxy_host"),
            proxy_port=self.settings.get("telegram.proxy_port"),
            proxy_username=self.settings.get("telegram.proxy_username"),
            proxy_password=self.settings.get("telegram.proxy_password"),
            proxy_secret=self.settings.get("telegram.proxy_secret"),
        )

    async def _run(self) -> None:
        api_id = int(self.settings.get("telegram.api_id") or 0)
        api_hash = str(self.settings.get("telegram.api_hash") or "").strip()
        phone = str(self.settings.get("telegram.phone") or "").strip()
        if self.mode not in {"request_code", "sign_in", "logout"}:
            raise ValueError(f"Неизвестный режим авторизации: {self.mode}")
        if api_id <= 0 or not api_hash:
            raise ValueError("Заполните API ID и API Hash")
        if self.mode != "logout" and not phone:
            raise ValueError("Заполните номер телефона")

        self.session_dir.mkdir(parents=True, exist_ok=True)
        # Authorization uses the same SQLite session as the queue worker. Repair
        # or quarantine corruption before Telethon opens it, otherwise the auth
        # thread can fail before the user is able to sign in again.
        session_file = (self.session_dir / "main").with_suffix(".session")
        TelegramService.purge_session_backups(session_file)
        TelegramService._prepare_session_file(session_file)
        proxy_settings = self._proxy_settings()
        proxy, connection_type = TelegramService.build_transport(proxy_settings)
        with warnings.catch_warnings():
            if connection_type in {
                connection.ConnectionTcpMTProxyRandomizedIntermediate,
                ConnectionTcpMTProxyFakeTLS,
            }:
                warnings.filterwarnings(
                    "ignore",
                    message="proxy argument will be ignored because python-socks is not installed",
                    category=UserWarning,
                    module=r"telethon\.client\.telegrambaseclient",
                )
            encrypted_session = EncryptedSQLiteSession(self.session_dir / "main")
            client = TelegramClient(
                encrypted_session,
                api_id,
                api_hash,
                proxy=proxy,
                connection=connection_type,
                device_model=f"{APP_NAME} Desktop",
                app_version=__version__,
                lang_code="ru",
                system_lang_code="ru-RU",
                flood_sleep_threshold=0,
                # One retry is required for Telethon's PhoneMigrate flow: the first
                # request learns the account DC, then Telethon reconnects and repeats it.
                request_retries=1,
                connection_retries=3,
                retry_delay=1,
                request_limiter=RateLimiter(1.0),
                request_timeout=60.0,
            )
        TelegramService._secure_session_file(session_file)
        try:
            await self._with_transient_retries(client.connect)
            if self.mode == "logout":
                if await self._with_transient_retries(client.is_user_authorized):
                    await self._await_interruptible(client.log_out())
                # Close SQLite before deleting it on Windows, then revoke every
                # local backup so corruption recovery cannot resurrect the old
                # account after an intentional logout.
                if client.is_connected():
                    await self._await_interruptible(client.disconnect(), timeout=5.0)
                TelegramService.purge_session_artifacts(
                    (self.session_dir / "main").with_suffix(".session")
                )
                if not self.isInterruptionRequested():
                    account: dict[str, Any] = {
                        "id": None,
                        "name": "",
                        "username": "",
                        "phone": "",
                    }
                    self._persist_account_state(account)
                    account["_persisted"] = self.database_path is not None
                    self.authorized.emit(account)
                return
            if await self._with_transient_retries(client.is_user_authorized):
                await self._emit_me(client)
                return

            if self.mode == "request_code":
                sent = await self._with_transient_retries(
                    lambda: client.send_code_request(phone),
                    timeout=self.CODE_REQUEST_TIMEOUT_SECONDS,
                )
                TelegramService._secure_session_file(session_file)
                if not self.isInterruptionRequested():
                    self.code_sent.emit(str(sent.phone_code_hash))
                return

            if not self.code or not self.phone_code_hash:
                raise ValueError("Введите код из Telegram")

            try:
                await self._await_interruptible(
                    client.sign_in(
                        phone=phone,
                        code=self.code,
                        phone_code_hash=self.phone_code_hash,
                    )
                )
            except SessionPasswordNeededError:
                if not self.password:
                    if not self.isInterruptionRequested():
                        self.password_required.emit()
                    return
                await self._await_interruptible(client.sign_in(password=self.password))
            await self._emit_me(client)
        finally:
            if client.is_connected():
                # A bounded disconnect prevents shutdown from hanging indefinitely.
                with suppress(Exception, asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(client.disconnect(), timeout=5.0)

    def _persist_account_state(self, account: dict) -> None:
        if self.database_path is None:
            return
        account_id = account.get("id")
        values = {
            "telegram.account_id": account_id or "",
            "telegram.account_name": account.get("name") or "",
            "telegram.account_username": account.get("username") or "",
            "telegram.authorized": "1" if account_id else "0",
        }
        persist_account_state(self.database_path, values)

    async def _emit_me(self, client: TelegramClient) -> None:
        me = await self._with_transient_retries(client.get_me)
        TelegramService._secure_session_file(
            (self.session_dir / "main").with_suffix(".session")
        )
        if self.isInterruptionRequested():
            raise asyncio.CancelledError
        first = str(getattr(me, "first_name", "") or "").strip()
        last = str(getattr(me, "last_name", "") or "").strip()
        account: dict[str, Any] = {
            "id": getattr(me, "id", None),
            "name": " ".join(part for part in (first, last) if part)
            or "Telegram Account",
            "username": getattr(me, "username", None),
            "phone": getattr(me, "phone", None),
        }
        self._persist_account_state(account)
        account["_persisted"] = self.database_path is not None
        self.authorized.emit(account)

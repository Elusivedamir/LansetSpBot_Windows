from __future__ import annotations

import random  # public monkeypatch compatibility and FloodWait buffer source
from typing import Any, Callable

from telethon import connection

from core.exceptions import DeferredTelegramError
from core.version import APP_NAME, __version__
from services.paced_telegram_client import PacedTelegramClient
from services.account_sessions import validate_session_name
from services.telegram import (
    LatestPostResult,
    TelegramDialogsMixin,
    TelegramMembershipMixin,
    TelegramMessagingMixin,
    TelegramPostResolverMixin,
    TelegramTransportMixin,
)
from services.encrypted_telethon_session import EncryptedSQLiteSession
from services.proxy_validation import normalize_proxy_config
from services.telegram_session import TelegramSessionMixin


class TelegramService(
    TelegramSessionMixin,
    TelegramTransportMixin,
    TelegramDialogsMixin,
    TelegramMembershipMixin,
    TelegramMessagingMixin,
    TelegramPostResolverMixin,
):
    """Telethon lifecycle facade composed from focused capabilities."""

    FLOOD_WAIT_BUFFER_MIN_SECONDS = 30
    FLOOD_WAIT_BUFFER_MAX_SECONDS = 45
    # Retained for configuration/backward compatibility. A real Telegram
    # FloodWait no longer receives a synthetic 3–5 minute floor.
    FLOOD_WAIT_AUTO_RESUME_MIN_SECONDS = 3 * 60
    FLOOD_WAIT_AUTO_RESUME_MAX_SECONDS = 5 * 60
    AUTHORIZATION_RECHECK_SECONDS = 15 * 60

    def _protected_flood_wait_seconds(self, raw_wait: int) -> int:
        """Honor Telegram's exact wait and add only the required safety buffer."""

        server_seconds = max(0, int(raw_wait))
        buffer_seconds = random.randint(
            self.FLOOD_WAIT_BUFFER_MIN_SECONDS,
            self.FLOOD_WAIT_BUFFER_MAX_SECONDS,
        )
        return server_seconds + int(buffer_seconds)

    async def _raise_flood_wait(self, exc: BaseException) -> None:
        """Translate every timed Telegram FloodWait using server time + buffer."""

        raw_wait = max(0, int(getattr(exc, "seconds", 0) or 0))
        buffer_seconds = random.randint(
            self.FLOOD_WAIT_BUFFER_MIN_SECONDS,
            self.FLOOD_WAIT_BUFFER_MAX_SECONDS,
        )
        wait_time = raw_wait + int(buffer_seconds)
        await self._report_status(
            "Telegram FloodWait: "
            f"{raw_wait} сек + защитный запас {buffer_seconds} сек"
        )
        raise DeferredTelegramError(
            "Telegram FloodWait",
            code="flood_wait_deferred",
            retry_after=wait_time,
        ) from exc

    def __init__(
        self,
        settings,
        limiter,
        status_callback: Callable[[str], Any] | None = None,
    ):
        self.settings = settings
        self.limiter = limiter
        self.account_id = int(getattr(settings, "account_id", 0) or 0)
        settings.session_dir.mkdir(parents=True, exist_ok=True)
        session_name = validate_session_name(
            getattr(settings, "session_name", "main")
        )
        session_base = settings.session_dir / "main"
        if session_name != "main":
            session_base = settings.session_dir / session_name
        telegram_session_base = session_base
        session_file = telegram_session_base.with_suffix(".session")
        self.purge_session_backups(session_file)
        self._prepare_session_file(session_file)
        proxy, connection_type = self.build_transport(settings)
        encrypted_session = EncryptedSQLiteSession(telegram_session_base)
        self.client = PacedTelegramClient(
            encrypted_session,
            settings.api_id,
            settings.api_hash,
            proxy=proxy,
            connection=connection_type,
            device_model=f"{APP_NAME} Desktop",
            app_version=__version__,
            lang_code="ru",
            system_lang_code="ru-RU",
            flood_sleep_threshold=0,
            request_retries=0,
            connection_retries=1,
            receive_updates=False,
            request_limiter=limiter,
            request_timeout=30.0,
        )
        self._secure_session_file(telegram_session_base.with_suffix(".session"))
        self._connected = False
        self._last_authorization_check = 0.0
        self._authorized_user = None
        self._status_callback = status_callback
        self._peer_references: dict[int, Any] = {}

    @staticmethod
    def build_transport(settings):
        """Return ``(proxy, connection class)`` for the pinned Telethon client."""

        if not bool(getattr(settings, "proxy_enabled", False)):
            return None, connection.ConnectionTcpFull
        config = normalize_proxy_config(
            getattr(settings, "proxy_type", "SOCKS5"),
            getattr(settings, "proxy_host", ""),
            getattr(settings, "proxy_port", 0),
            getattr(settings, "proxy_username", ""),
            getattr(settings, "proxy_password", ""),
        )
        try:
            import socks
        except ImportError as exc:  # pragma: no cover - dependency error
            raise RuntimeError(
                "PySocks is required for SOCKS/HTTP proxy support"
            ) from exc
        mapping = {
            "SOCKS5": socks.SOCKS5,
            "SOCKS4": socks.SOCKS4,
            "HTTP": socks.HTTP,
        }
        return (
            mapping[config.proxy_type],
            config.host,
            config.port,
            True,
            config.username or None,
            config.password or None,
        ), connection.ConnectionTcpFull

    @staticmethod
    def build_proxy(settings):
        """Compatibility helper returning only Telethon's proxy tuple."""

        return TelegramService.build_transport(settings)[0]

    @staticmethod
    def build_connection(settings):
        """Compatibility helper returning Telethon's connection implementation."""

        return TelegramService.build_transport(settings)[1]


__all__ = ["LatestPostResult", "TelegramService"]

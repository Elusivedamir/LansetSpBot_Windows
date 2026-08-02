from __future__ import annotations

from pathlib import Path

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, cast

from core.config import TelegramSettings
from storage.account_database_view import AccountDatabaseView


SECRET_SETTING_KEYS = frozenset(
    {
        "telegram.api_hash",
        "telegram.phone",
        "telegram.proxy_username",
        "telegram.proxy_password",
        "openai.api_key",
    }
)


def account_secret_key(account_id: int, key: str) -> str:
    owner = int(account_id)
    if owner <= 0:
        raise ValueError("Account secret requires a positive account id")
    clean = str(key or "").strip()
    if clean not in SECRET_SETTING_KEYS:
        raise ValueError(f"Unsupported account secret key: {clean!r}")
    return f"account.{owner}.{clean}"


class AccountSecretStoreView:
    def __init__(self, base, account_id: int) -> None:
        self.base = base
        self.account_id = int(account_id)

    def set(self, key: str, value: str | None) -> None:
        self.base.set(account_secret_key(self.account_id, key), value)

    def get(self, key: str, default: str = "") -> str:
        value = self.base.get(account_secret_key(self.account_id, key), default)
        return default if value is None else cast(str, value)

    def get_strict_optional(self, key: str) -> str | None:
        value = self.base.get_strict_optional(
            account_secret_key(self.account_id, key)
        )
        return cast(str | None, value)

    def delete(self, key: str) -> None:
        self.base.delete(account_secret_key(self.account_id, key))


class AccountQueueWorkerView:
    """Bind implicit database and cancellation scopes to one account."""

    def __init__(self, base, database: AccountDatabaseView, account_id: int) -> None:
        self._base = base
        self._database = database
        self.account_id = int(account_id)

    def get_db(self):
        return self._database

    def create_scope_dispatch_barrier(self, *scopes, pre_dispatch_check=None):
        values = list(scopes)
        values.append(("account", self.account_id))
        return self._base.create_scope_dispatch_barrier(
            *values, pre_dispatch_check=pre_dispatch_check
        )

    def request_scope_cancellation(
        self, scope_type: str, scope_id: int, account_id: int | None = None
    ) -> None:
        self._base.request_scope_cancellation(
            scope_type,
            scope_id,
            self.account_id if scope_type == "channel" else account_id,
        )

    def clear_scope_cancellation(
        self, scope_type: str, scope_id: int, account_id: int | None = None
    ) -> None:
        self._base.clear_scope_cancellation(
            scope_type,
            scope_id,
            self.account_id if scope_type == "channel" else account_id,
        )

    def is_scope_cancelled(
        self, scope_type: str, scope_id: int, account_id: int | None = None
    ) -> bool:
        if self._base.is_scope_cancelled("account", self.account_id):
            return True
        return cast(
            bool,
            self._base.is_scope_cancelled(
                scope_type,
                scope_id,
                self.account_id if scope_type == "channel" else account_id,
            ),
        )

    async def safe_sleep(self, seconds, step=0.5, *, cancel_scope=None):
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            if self._base.isInterruptionRequested():
                return False
            if self._base.is_scope_cancelled("account", self.account_id):
                return False
            if cancel_scope is not None and self.is_scope_cancelled(*cancel_scope):
                return False
            delay = min(float(step), remaining)
            import asyncio

            await asyncio.sleep(delay)
            remaining -= delay
        return not self._base.is_scope_cancelled("account", self.account_id)

    def __getattr__(self, name: str):
        return getattr(self._base, name)


@dataclass
class AccountAPIContext:
    _secret_lock: Any


class AccountContainerView:
    """Provide existing handler factories with one isolated account context."""

    def __init__(
        self,
        base,
        *,
        account_id: int,
        worker_database,
    ) -> None:
        self._base = base
        self.account_id = int(account_id)
        self.config = base.config
        self.secret_store = AccountSecretStoreView(base.secret_store, self.account_id)
        self.database = AccountDatabaseView(worker_database, self.account_id)
        self.queue_worker = AccountQueueWorkerView(
            base.queue_worker, self.database, self.account_id
        )
        api = getattr(base, "api", None)
        self.api = AccountAPIContext(
            _secret_lock=getattr(api, "_secret_lock", nullcontext())
        )

    @staticmethod
    def _as_bool(value: Any) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    def _strict_secret_value(self, key: str) -> str | None:
        getter = getattr(type(self.secret_store), "get_strict_optional", None)
        if callable(getter):
            return self.secret_store.get_strict_optional(key)
        value = self.secret_store.get(key, "")
        return None if value in (None, "") else str(value)

    def _telegram_settings(self, db=None) -> TelegramSettings:
        database = db or self.database
        account = database.get_telegram_account(self.account_id)
        if not account:
            raise RuntimeError(f"Telegram account {self.account_id} does not exist")
        saved = database.get_settings("telegram.")
        telegram_config = getattr(self.config, "telegram", None)
        api_id = self._as_int(
            saved.get("telegram.api_id"),
            getattr(telegram_config, "api_id", 0),
        )
        api_hash = str(
            self._strict_secret_value("telegram.api_hash")
            or getattr(telegram_config, "api_hash", "")
            or ""
        ).strip()
        phone = str(
            self._strict_secret_value("telegram.phone")
            or getattr(telegram_config, "phone", None)
            or ""
        ).strip() or None
        proxy_port = self._as_int(saved.get("telegram.proxy_port"), 0) or None
        return TelegramSettings(
            api_id=api_id,
            api_hash=api_hash,
            session_dir=getattr(telegram_config, "session_dir", Path("sessions")),
            session_name=str(account.get("session_name") or f"account_{self.account_id}"),
            account_id=self.account_id,
            phone=phone,
            proxy_enabled=self._as_bool(saved.get("telegram.proxy_enabled")),
            proxy_type=str(saved.get("telegram.proxy_type") or "SOCKS5").upper(),
            proxy_host=str(saved.get("telegram.proxy_host") or "").strip() or None,
            proxy_port=proxy_port,
            proxy_username=str(
                self._strict_secret_value("telegram.proxy_username") or ""
            ).strip()
            or None,
            proxy_password=str(
                self._strict_secret_value("telegram.proxy_password") or ""
            )
            or None,
            expected_account_id=self.account_id,
        )

    def __getattr__(self, name: str):
        return getattr(self._base, name)

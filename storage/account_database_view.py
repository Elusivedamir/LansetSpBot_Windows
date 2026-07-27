from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any

from storage.database import Database


ACCOUNT_SCOPED_PREFIXES = (
    "telegram.",
    "automation.",
    "commenting.",
    "openai.",
    "scheduler.",
)


class AccountDatabaseView(Database):
    """A Database facade whose implicit settings resolve to one account.

    Domain repositories already accept explicit account_id values. Legacy methods
    that still read ``telegram.account_id`` or other settings are safely contained
    by this facade when worker handlers are created for a particular account.
    """

    def __init__(self, base: Database, account_id: int) -> None:
        # Deliberately do not call Database.__init__: this view shares the owning
        # thread's existing SQLCipher connection and key material.
        self._base = base
        self.account_id = int(account_id)
        if self.account_id <= 0:
            raise ValueError("AccountDatabaseView requires a positive account id")
        self.path = base.path
        self.key_storage_dir = base.key_storage_dir
        self.busy_timeout_ms = base.busy_timeout_ms
        self.sqlite_timeout_seconds = base.sqlite_timeout_seconds
        self._database_key = base._database_key

    def get_connection(self) -> AbstractContextManager:
        return self._base.get_connection()

    def close_thread_connection(self) -> None:
        # The parent QueueWorker owns the shared thread-local connection.
        return None

    def _scoped(self, key: str) -> bool:
        return str(key).startswith(ACCOUNT_SCOPED_PREFIXES)

    def get_setting(self, key, default=None):
        name = str(key)
        if name == "telegram.account_id":
            return str(self.account_id)
        if name == "telegram.authorized":
            account = self._base.get_telegram_account(self.account_id)
            return "1" if account and account.get("authorized") else "0"
        if name == "telegram.account_name":
            account = self._base.get_telegram_account(self.account_id)
            return (
                str(account.get("display_name") or "Telegram Account")
                if account
                else default
            )
        if name == "telegram.account_username":
            account = self._base.get_telegram_account(self.account_id)
            return str(account.get("username") or "") if account else default
        if self._scoped(name):
            return self._base.get_account_setting(self.account_id, name, default)
        return self._base.get_setting(name, default)

    def get_settings(self, prefix=None):
        if prefix and self._scoped(str(prefix)):
            return self._base.get_account_settings(self.account_id, str(prefix))
        if prefix is None:
            global_values = self._base.get_settings()
            account_values = self._base.get_account_settings(self.account_id)
            global_values.update(account_values)
            account = self._base.get_telegram_account(self.account_id) or {}
            global_values.update(
                {
                    "telegram.account_id": str(self.account_id),
                    "telegram.account_name": str(
                        account.get("display_name") or "Telegram Account"
                    ),
                    "telegram.account_username": str(account.get("username") or ""),
                    "telegram.authorized": (
                        "1" if account.get("authorized") else "0"
                    ),
                }
            )
            return global_values
        return self._base.get_settings(prefix)

    def set_setting(self, key, value):
        name = str(key)
        if name in {
            "telegram.account_id",
            "telegram.account_name",
            "telegram.account_username",
            "telegram.authorized",
        }:
            raise ValueError(f"Identity setting {name} is managed by telegram_accounts")
        if self._scoped(name):
            self._base.set_account_settings(self.account_id, {name: value})
            return
        self._base.set_setting(name, value)

    def set_settings(self, values):
        account_values: dict[str, Any] = {}
        global_values: dict[str, Any] = {}
        for key, value in dict(values).items():
            name = str(key)
            if name in {
                "telegram.account_id",
                "telegram.account_name",
                "telegram.account_username",
                "telegram.authorized",
            }:
                continue
            if self._scoped(name):
                account_values[name] = value
            else:
                global_values[name] = value
        if account_values:
            self._base.set_account_settings(self.account_id, account_values)
        if global_values:
            self._base.set_settings(global_values)

    def delete_setting(self, key):
        name = str(key)
        if self._scoped(name):
            self._base.delete_account_setting(self.account_id, name)
            return
        self._base.delete_setting(name)

    def __getattr__(self, name: str):
        # Runtime-only fields and future helper methods remain available without
        # copying the Database object or its SQLCipher state.
        return getattr(self._base, name)

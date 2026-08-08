from __future__ import annotations
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any, cast
from core.account_limits import (
    MAX_REGISTERED_TELEGRAM_ACCOUNTS,
    account_limit_message,
)
from core.config import MAX_COMMENT_VARIANTS
from storage.db_common import DatabaseError
from storage.sqlcipher_driver import dbapi as sqlite3
from storage.account_repository_parts.common import (
    ACCOUNT_SETTING_PREFIXES,
    ACCOUNT_STATES,
    MAX_TELEGRAM_ACCOUNTS,
    SECRET_ACCOUNT_SETTING_KEYS,
    SESSION_NAME_RE,
    _active_unique,
    _fingerprint,
    _mask_phone,
    _normalized_slots,
    _positive_account_id,
)

class AccountSettingsRepositoryMixin:
    def get_account_setting(
        self, account_id: object, key: str, default: object = None
    ) -> object:
        owner = _positive_account_id(account_id)
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    "SELECT value FROM account_settings WHERE account_id=? AND key=?",
                    (owner, str(key)),
                ).fetchone()
                return default if row is None else row[0]
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read account setting: {exc}") from exc
    def get_account_settings(
        self, account_id: object, prefix: str | None = None
    ) -> dict[str, Any]:
        owner = _positive_account_id(account_id)
        try:
            with self.get_connection() as conn:
                if prefix:
                    rows = conn.execute(
                        """SELECT key, value FROM account_settings
                           WHERE account_id=? AND key LIKE ? ORDER BY key""",
                        (owner, f"{prefix}%"),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT key, value FROM account_settings
                           WHERE account_id=? ORDER BY key""",
                        (owner,),
                    ).fetchall()
                return {
                    str(row["key"]): row["value"]
                    for row in rows
                    if str(row["key"]) not in SECRET_ACCOUNT_SETTING_KEYS
                }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read account settings: {exc}") from exc
    def set_account_settings(
        self, account_id: object, values: dict[str, Any]
    ) -> None:
        owner = _positive_account_id(account_id)
        if not isinstance(values, dict):
            raise DatabaseError("Account settings must be an object")
        secret_keys = sorted(
            str(key)
            for key in values
            if str(key) in SECRET_ACCOUNT_SETTING_KEYS
        )
        if secret_keys:
            raise DatabaseError(
                "Secret account settings must be stored in SecretStore: "
                + ", ".join(secret_keys)
            )
        try:
            with self.get_connection() as conn:
                if self._account_row(conn, owner) is None:
                    raise DatabaseError("Telegram account does not exist")
                for key, value in values.items():
                    conn.execute(
                        """INSERT INTO account_settings(
                               account_id, key, value, updated_at)
                           VALUES(?, ?, ?, CURRENT_TIMESTAMP)
                           ON CONFLICT(account_id, key) DO UPDATE SET
                               value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                        (owner, str(key), None if value is None else str(value)),
                    )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to save account settings: {exc}") from exc
    def set_account_settings_with_selected_projection(
        self, account_id: object, values: dict[str, Any]
    ) -> None:
        """Atomically update account settings and the selected projection."""

        owner = _positive_account_id(account_id)
        if not isinstance(values, dict):
            raise DatabaseError("Account settings must be an object")
        secret_keys = sorted(
            str(key)
            for key in values
            if str(key) in SECRET_ACCOUNT_SETTING_KEYS
        )
        if secret_keys:
            raise DatabaseError(
                "Secret account settings must be stored in SecretStore: "
                + ", ".join(secret_keys)
            )
        normalized = {
            str(key): None if value is None else str(value)
            for key, value in values.items()
        }
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                if self._account_row(conn, owner) is None:
                    raise DatabaseError("Telegram account does not exist")
                for key, value in normalized.items():
                    conn.execute(
                        """INSERT INTO account_settings(
                               account_id, key, value, updated_at)
                           VALUES(?, ?, ?, CURRENT_TIMESTAMP)
                           ON CONFLICT(account_id, key) DO UPDATE SET
                               value=excluded.value,
                               updated_at=CURRENT_TIMESTAMP""",
                        (owner, key, value),
                    )

                selected_row = conn.execute(
                    "SELECT value FROM settings "
                    "WHERE key='ui.selected_account_id'"
                ).fetchone()
                try:
                    selected_account_id = (
                        int(selected_row[0] or 0) if selected_row else 0
                    )
                except (TypeError, ValueError, OverflowError):
                    selected_account_id = 0
                if selected_account_id == owner:
                    for key, value in normalized.items():
                        conn.execute(
                            """INSERT INTO settings(key, value, updated_at)
                               VALUES(?, ?, CURRENT_TIMESTAMP)
                               ON CONFLICT(key) DO UPDATE SET
                                   value=excluded.value,
                                   updated_at=CURRENT_TIMESTAMP""",
                            (key, value),
                        )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to save account settings atomically: {exc}"
            ) from exc
    def replace_account_settings(
        self, account_id: object, values: dict[str, Any]
    ) -> None:
        """Replace the complete public settings snapshot for one account."""

        owner = _positive_account_id(account_id)
        if not isinstance(values, dict):
            raise DatabaseError("Account settings must be an object")
        secret_keys = sorted(
            str(key)
            for key in values
            if str(key) in SECRET_ACCOUNT_SETTING_KEYS
        )
        if secret_keys:
            raise DatabaseError(
                "Secret account settings must be stored in SecretStore: "
                + ", ".join(secret_keys)
            )
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                if self._account_row(conn, owner) is None:
                    raise DatabaseError("Telegram account does not exist")
                conn.execute(
                    "DELETE FROM account_settings WHERE account_id=?",
                    (owner,),
                )
                for key, value in values.items():
                    conn.execute(
                        """INSERT INTO account_settings(
                               account_id, key, value, updated_at)
                           VALUES(?, ?, ?, CURRENT_TIMESTAMP)""",
                        (owner, str(key), None if value is None else str(value)),
                    )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to replace account settings: {exc}"
            ) from exc
    def delete_account_setting(self, account_id: object, key: str) -> None:
        owner = _positive_account_id(account_id)
        try:
            with self.get_connection() as conn:
                conn.execute(
                    "DELETE FROM account_settings WHERE account_id=? AND key=?",
                    (owner, str(key)),
                )
        except Exception as exc:
            raise DatabaseError(f"Failed to delete account setting: {exc}") from exc

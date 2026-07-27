"""The registry of Telegram accounts the program can work with.

Account-scoped data has existed since schema v18; what did not exist was a
place to record the accounts themselves. Everything here is deliberately
non-secret: api_hash, phone numbers and proxy credentials belong in the secret
store under per-account keys, never in SQLite rows that a support file or a
screenshot might carry.

A row is created before the account authorizes, because the operator enters
API credentials first and Telegram only reports the account id afterwards.
Until then ``telegram_account_id`` is 0 and the row owns no data.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from core.mixin_host import MixinHost
from storage.db_common import DatabaseError

log = logging.getLogger(__name__)

_SESSION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

ACCOUNT_COLUMNS = (
    "id",
    "telegram_account_id",
    "label",
    "session_name",
    "api_id",
    "phone_hint",
    "proxy_enabled",
    "proxy_type",
    "proxy_host",
    "proxy_port",
    "enabled",
    "position",
    "created_at",
    "updated_at",
)


class AccountRegistryRepositoryMixin(MixinHost):
    def _account_row(self, row: Any) -> dict[str, Any]:
        account = {column: row[index] for index, column in enumerate(ACCOUNT_COLUMNS)}
        account["telegram_account_id"] = int(account["telegram_account_id"] or 0)
        account["enabled"] = bool(account["enabled"])
        account["proxy_enabled"] = bool(account["proxy_enabled"])
        account["authorized"] = account["telegram_account_id"] != 0
        return account

    def list_accounts(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = f"SELECT {', '.join(ACCOUNT_COLUMNS)} FROM accounts"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY position, id"
        with self.get_connection() as conn:
            rows = conn.execute(query).fetchall()
        return [self._account_row(row) for row in rows]

    def get_account(self, account_row_id: int) -> dict[str, Any] | None:
        with self.get_connection() as conn:
            row = conn.execute(
                f"SELECT {', '.join(ACCOUNT_COLUMNS)} FROM accounts WHERE id = ?",
                (int(account_row_id),),
            ).fetchone()
        return self._account_row(row) if row else None

    def get_account_by_telegram_id(
        self, telegram_account_id: int
    ) -> dict[str, Any] | None:
        telegram_account_id = int(telegram_account_id or 0)
        if telegram_account_id == 0:
            return None
        with self.get_connection() as conn:
            row = conn.execute(
                f"SELECT {', '.join(ACCOUNT_COLUMNS)} FROM accounts "
                "WHERE telegram_account_id = ?",
                (telegram_account_id,),
            ).fetchone()
        return self._account_row(row) if row else None

    def create_account(
        self,
        *,
        label: str = "",
        api_id: int | None = None,
        phone_hint: str = "",
        session_name: str | None = None,
    ) -> dict[str, Any]:
        """Add an account that has not authorized yet.

        The session name is what Telethon opens on disk, so it is validated
        rather than taken on trust: a label the operator typed must never be
        able to reach outside the sessions directory.
        """

        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1, COALESCE(MAX(id), 0) + 1 "
                "FROM accounts"
            ).fetchone()
            position = int(row[0] or 0)
            next_id = int(row[1] or 1)
            name = str(session_name or f"account-{next_id}").strip()
            if not _SESSION_NAME.match(name):
                raise DatabaseError(
                    f"Недопустимое имя файла сессии: {name!r}. "
                    "Разрешены латинские буквы, цифры, дефис и подчёркивание."
                )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO accounts(
                        telegram_account_id, label, session_name, api_id,
                        phone_hint, enabled, position
                    ) VALUES(0, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        str(label or "").strip(),
                        name,
                        int(api_id) if api_id is not None else None,
                        str(phone_hint or "").strip(),
                        position,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - normalized below
                raise DatabaseError(
                    f"Не удалось создать аккаунт с сессией {name!r}: {exc}"
                ) from exc
            created_id = int(cursor.lastrowid or 0)
        account = self.get_account(created_id)
        if account is None:
            raise DatabaseError("Аккаунт создан, но не читается")
        log.info(
            "Account row created: id=%s session=%s", account["id"], account["session_name"]
        )
        return account

    def update_account(self, account_row_id: int, **fields: Any) -> dict[str, Any]:
        allowed = {
            "telegram_account_id",
            "label",
            "api_id",
            "phone_hint",
            "proxy_enabled",
            "proxy_type",
            "proxy_host",
            "proxy_port",
            "position",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise DatabaseError(f"Неизвестные поля аккаунта: {sorted(unknown)}")
        if not fields:
            account = self.get_account(account_row_id)
            if account is None:
                raise DatabaseError(f"Аккаунт {account_row_id} не найден")
            return account
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [
            int(value) if name in {"proxy_enabled"} else value
            for name, value in fields.items()
        ]
        with self.get_connection() as conn:
            conn.execute(
                f"UPDATE accounts SET {assignments}, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (*values, int(account_row_id)),
            )
        account = self.get_account(account_row_id)
        if account is None:
            raise DatabaseError(f"Аккаунт {account_row_id} не найден")
        return account

    def set_account_enabled(self, account_row_id: int, enabled: bool) -> dict[str, Any]:
        """Turn one account's work on or off without touching its data."""

        with self.get_connection() as conn:
            conn.execute(
                "UPDATE accounts SET enabled = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (1 if enabled else 0, int(account_row_id)),
            )
        account = self.get_account(account_row_id)
        if account is None:
            raise DatabaseError(f"Аккаунт {account_row_id} не найден")
        log.info(
            "Account %s %s", account["id"], "enabled" if account["enabled"] else "disabled"
        )
        return account

    def delete_account(self, account_row_id: int) -> bool:
        """Remove a registry row.

        The account's own data is keyed by the Telegram account id and is left
        alone: deleting a row must not silently destroy history that another
        row could still be pointing at.
        """

        with self.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM accounts WHERE id = ?", (int(account_row_id),)
            )
            removed = int(cursor.rowcount or 0)
        return removed > 0

    def count_accounts(self, *, enabled_only: bool = False) -> int:
        query = "SELECT COUNT(*) FROM accounts"
        if enabled_only:
            query += " WHERE enabled = 1"
        with self.get_connection() as conn:
            row = conn.execute(query).fetchone()
        return int((row or [0])[0] or 0)

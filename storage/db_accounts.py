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

if TYPE_CHECKING:  # pragma: no cover
    from core.mixin_host import MixinHost as _MixinHost
else:
    class _MixinHost:
        pass


MAX_TELEGRAM_ACCOUNTS = MAX_REGISTERED_TELEGRAM_ACCOUNTS
ACCOUNT_STATES = frozenset(
    {
        "disconnected",
        "connecting",
        "connected",
        "running",
        "paused",
        "stopping",
        "stopped",
        "network_wait",
        "flood_wait",
        "restricted",
        "authorization_required",
        "error",
    }
)
SESSION_NAME_RE = re.compile(r"^(?:main|account_[1-9][0-9]*|pending_[a-f0-9]{16,64})$")
ACCOUNT_SETTING_PREFIXES = (
    "telegram.",
    "automation.",
    "commenting.",
    "openai.",
    "scheduler.",
)
SECRET_ACCOUNT_SETTING_KEYS = frozenset(
    {
        "telegram.api_hash",
        "telegram.phone",
        "telegram.proxy_username",
        "telegram.proxy_password",
        "openai.api_key",
    }
)


def _positive_account_id(value: object) -> int:
    try:
        parsed: int = int(cast(Any, value) or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DatabaseError(f"Invalid Telegram account id: {value!r}") from exc
    if parsed <= 0:
        raise DatabaseError("Telegram account id must be positive")
    return parsed


def _mask_phone(value: object) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if not digits:
        return ""
    tail = digits[-4:]
    country = f"+{digits[0]}" if digits else "+"
    return f"{country} *** ***-{tail[:2]}-{tail[2:]}" if len(tail) == 4 else f"{country} ***"


def _normalized_slots(values: list[str] | tuple[str, ...] | None) -> list[str]:
    result = [str(item or "").strip() for item in list(values or [])[:MAX_COMMENT_VARIANTS]]
    result += [""] * (MAX_COMMENT_VARIANTS - len(result))
    return result


def _active_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _fingerprint(values: list[str]) -> str:
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AccountRepositoryMixin(_MixinHost):
    """Durable registry and explicit cross-account operations."""

    MAX_TELEGRAM_ACCOUNTS = MAX_TELEGRAM_ACCOUNTS

    def _account_row(self, conn, account_id: int):
        return conn.execute(
            """SELECT id, telegram_account_id, session_name, display_name, username,
                      phone_masked, authorized, runtime_state, stopped, last_error,
                      last_activity_at, created_at, updated_at
               FROM telegram_accounts WHERE telegram_account_id=?""",
            (int(account_id),),
        ).fetchone()

    def count_telegram_accounts(self) -> int:
        try:
            with self.get_connection() as conn:
                return int(
                    conn.execute("SELECT COUNT(*) FROM telegram_accounts").fetchone()[0]
                    or 0
                )
        except Exception as exc:
            raise DatabaseError(f"Failed to count Telegram accounts: {exc}") from exc

    def list_telegram_accounts(self) -> list[dict[str, Any]]:
        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    """SELECT a.id, a.telegram_account_id, a.session_name,
                              a.display_name, a.username, a.phone_masked,
                              a.authorized, a.runtime_state, a.stopped, a.last_error,
                              a.last_activity_at, a.created_at, a.updated_at,
                              EXISTS(
                                  SELECT 1 FROM comment_campaigns c
                                   WHERE c.account_id=a.telegram_account_id
                                     AND c.status IN (
                                         'running','paused','network_wait','cycle_wait'
                                     )
                              ) AS comment_campaign_active,
                              EXISTS(
                                  SELECT 1 FROM join_campaigns j
                                   WHERE j.account_id=a.telegram_account_id
                                     AND j.status IN (
                                         'running','paused','network_wait'
                                     )
                              ) AS join_campaign_active
                       FROM telegram_accounts a
                       ORDER BY a.id ASC"""
                ).fetchall()
                selected = self.get_selected_account_id()
                previous = self.get_previous_selected_account_id()
                result = []
                for row in rows:
                    item = dict(row)
                    account_id = int(item["telegram_account_id"])
                    item["selected"] = account_id == selected
                    item["previous_selected"] = account_id == previous
                    item["campaign_active"] = bool(
                        item.get("comment_campaign_active")
                        or item.get("join_campaign_active")
                    )
                    item["authorized"] = bool(item.get("authorized"))
                    item["stopped"] = bool(item.get("stopped"))
                    result.append(item)
                return result
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to list Telegram accounts: {exc}") from exc

    def get_telegram_account(self, account_id: object) -> dict[str, Any] | None:
        owner = _positive_account_id(account_id)
        try:
            with self.get_connection() as conn:
                row = self._account_row(conn, owner)
                if row is None:
                    return None
                result = dict(row)
                result["authorized"] = bool(result.get("authorized"))
                result["stopped"] = bool(result.get("stopped"))
                return result
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read Telegram account: {exc}") from exc

    def get_selected_account_id(self) -> int:
        raw = self.get_setting(
            "ui.selected_account_id",
            self.get_setting("telegram.account_id", 0),
        )
        try:
            value = int(raw or 0)
        except (TypeError, ValueError, OverflowError):
            return 0
        return value if value > 0 else 0

    def get_previous_selected_account_id(self) -> int:
        raw = self.get_setting("ui.previous_selected_account_id", 0)
        try:
            value = int(raw or 0)
        except (TypeError, ValueError, OverflowError):
            return 0
        return value if value > 0 else 0

    def register_telegram_account(
        self,
        *,
        telegram_account_id: object,
        session_name: str,
        display_name: str,
        username: str | None = None,
        phone: str | None = None,
        authorized: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        owner = _positive_account_id(telegram_account_id)
        clean_session = str(session_name or "").strip()
        if not SESSION_NAME_RE.fullmatch(clean_session):
            raise DatabaseError("Unsafe Telegram session name")
        clean_name = " ".join(str(display_name or "Telegram Account").split())
        clean_name = clean_name[:160] or "Telegram Account"
        clean_username = str(username or "").strip().lstrip("@")[:64] or None
        phone_masked = _mask_phone(phone)
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                existing = self._account_row(conn, owner)
                if existing is not None:
                    conn.execute(
                        """UPDATE telegram_accounts
                           SET display_name=?, username=?, phone_masked=?,
                               authorized=?, runtime_state=CASE
                                   WHEN stopped=1 THEN 'stopped'
                                   WHEN ?=1 THEN 'connected'
                                   ELSE 'authorization_required'
                               END,
                               last_error=NULL, updated_at=CURRENT_TIMESTAMP
                           WHERE telegram_account_id=?""",
                        (
                            clean_name,
                            clean_username,
                            phone_masked,
                            1 if authorized else 0,
                            1 if authorized else 0,
                            owner,
                        ),
                    )
                    row = self._account_row(conn, owner)
                    return dict(row), False
                count = int(
                    conn.execute("SELECT COUNT(*) FROM telegram_accounts").fetchone()[0]
                    or 0
                )
                if count >= self.MAX_TELEGRAM_ACCOUNTS:
                    raise DatabaseError(
                        account_limit_message(self.MAX_TELEGRAM_ACCOUNTS)
                    )
                conn.execute(
                    """INSERT INTO telegram_accounts(
                           telegram_account_id, session_name, display_name, username,
                           phone_masked, authorized, runtime_state, stopped,
                           updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)""",
                    (
                        owner,
                        clean_session,
                        clean_name,
                        clean_username,
                        phone_masked,
                        1 if authorized else 0,
                        "connected" if authorized else "authorization_required",
                    ),
                )
                row = self._account_row(conn, owner)
                if row is None:
                    raise DatabaseError("Telegram account row was not created")
                return dict(row), True
        except DatabaseError:
            raise
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "telegram account limit reached" in message:
                raise DatabaseError(
                    account_limit_message(self.MAX_TELEGRAM_ACCOUNTS)
                ) from exc
            raise DatabaseError(f"Telegram account registration failed: {exc}") from exc
        except Exception as exc:
            raise DatabaseError(f"Telegram account registration failed: {exc}") from exc

    def update_account_session_name(self, account_id: object, session_name: str) -> None:
        owner = _positive_account_id(account_id)
        clean_session = str(session_name or "").strip()
        if not SESSION_NAME_RE.fullmatch(clean_session):
            raise DatabaseError("Unsafe Telegram session name")
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE telegram_accounts
                       SET session_name=?, updated_at=CURRENT_TIMESTAMP
                       WHERE telegram_account_id=?""",
                    (clean_session, owner),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError("Telegram account does not exist")
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to update account session: {exc}") from exc

    def rollback_new_telegram_account(
        self, account_id: object, *, expected_session_name: str
    ) -> bool:
        """Rollback only a just-created empty account after registration failure."""

        owner = _positive_account_id(account_id)
        clean_session = str(expected_session_name or "").strip()
        if not SESSION_NAME_RE.fullmatch(clean_session):
            raise DatabaseError("Unsafe Telegram session name")
        try:
            with self.get_connection() as conn:
                busy = conn.execute(
                    """SELECT
                           EXISTS(SELECT 1 FROM comment_campaigns WHERE account_id=?),
                           EXISTS(SELECT 1 FROM join_campaigns WHERE account_id=?),
                           EXISTS(SELECT 1 FROM tasks WHERE account_id=?),
                           EXISTS(SELECT 1 FROM channels WHERE account_id=?)""",
                    (owner, owner, owner, owner),
                ).fetchone()
                if busy is not None and any(bool(value) for value in busy):
                    return False
                cursor = conn.execute(
                    """DELETE FROM telegram_accounts
                       WHERE telegram_account_id=? AND session_name=?""",
                    (owner, clean_session),
                )
                return bool(cursor.rowcount == 1)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to rollback new Telegram account: {exc}"
            ) from exc

    def set_account_runtime_state(
        self,
        account_id: object,
        state: str,
        *,
        error: str | None = None,
    ) -> None:
        owner = _positive_account_id(account_id)
        normalized = str(state or "").strip().lower()
        if normalized not in ACCOUNT_STATES:
            raise DatabaseError(f"Unsupported account runtime state: {state!r}")
        stopped = 1 if normalized == "stopped" else 0
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE telegram_accounts
                       SET runtime_state=?, stopped=?,
                           last_error=?, last_activity_at=CURRENT_TIMESTAMP,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE telegram_account_id=?""",
                    (normalized, stopped, str(error or "") or None, owner),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError("Telegram account does not exist")
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to update account state: {exc}") from exc

    def mark_account_authorization_required(
        self,
        account_id: object,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Keep the account registry while revoking local Telegram authorization."""

        owner = _positive_account_id(account_id)
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """UPDATE telegram_accounts
                       SET authorized=0,
                           runtime_state='authorization_required',
                           stopped=1,
                           last_error=?,
                           last_activity_at=CURRENT_TIMESTAMP,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE telegram_account_id=?""",
                    (str(error or "") or None, owner),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError("Telegram account does not exist")

                selected_row = conn.execute(
                    "SELECT value FROM settings WHERE key='ui.selected_account_id'"
                ).fetchone()
                try:
                    selected = int(selected_row[0] or 0) if selected_row else 0
                except (TypeError, ValueError, OverflowError):
                    selected = 0
                if selected == owner:
                    conn.execute(
                        """INSERT INTO settings(key, value, updated_at)
                           VALUES('telegram.authorized', '0', CURRENT_TIMESTAMP)
                           ON CONFLICT(key) DO UPDATE SET
                               value='0', updated_at=CURRENT_TIMESTAMP"""
                    )

                row = self._account_row(conn, owner)
                if row is None:
                    raise DatabaseError("Telegram account does not exist")
                result = dict(row)
                result["authorized"] = False
                result["stopped"] = True
                return result
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to revoke Telegram account authorization: {exc}"
            ) from exc

    def select_telegram_account(
        self,
        account_id: object,
        *,
        allow_unauthorized: bool = False,
    ) -> dict[str, Any]:
        owner = _positive_account_id(account_id)
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                row = self._account_row(conn, owner)
                if row is None:
                    raise DatabaseError("Telegram account does not exist")
                if not bool(row["authorized"]) and not allow_unauthorized:
                    raise DatabaseError("Telegram account requires authorization")
                current_raw = conn.execute(
                    "SELECT value FROM settings WHERE key='ui.selected_account_id'"
                ).fetchone()
                current = int(current_raw[0] or 0) if current_raw else 0
                if current != owner:
                    conn.execute(
                        """INSERT INTO settings(key, value, updated_at)
                           VALUES('ui.previous_selected_account_id', ?, CURRENT_TIMESTAMP)
                           ON CONFLICT(key) DO UPDATE SET
                               value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                        (str(current) if current > 0 else "",),
                    )
                conn.execute(
                    """INSERT INTO settings(key, value, updated_at)
                       VALUES('ui.selected_account_id', ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(key) DO UPDATE SET
                           value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                    (str(owner),),
                )
                # Remove stale selected-account compatibility values before
                # projecting the newly selected account. Without this cleanup,
                # an account that never configured OpenAI/proxy could inherit
                # the previous account's public options through legacy readers.
                conn.execute(
                    """DELETE FROM settings
                       WHERE (
                           key LIKE 'telegram.%'
                           OR key LIKE 'automation.%'
                           OR key LIKE 'commenting.%'
                           OR key LIKE 'openai.%'
                           OR key LIKE 'scheduler.%'
                       )
                         AND key NOT IN (
                           'telegram.account_id',
                           'telegram.account_name',
                           'telegram.account_username',
                           'telegram.authorized'
                         )"""
                )
                compatibility = {
                    "telegram.account_id": str(owner),
                    "telegram.account_name": str(row["display_name"] or "Telegram Account"),
                    "telegram.account_username": str(row["username"] or ""),
                    "telegram.authorized": "1" if bool(row["authorized"]) else "0",
                }
                for compatibility_key, compatibility_value in compatibility.items():
                    conn.execute(
                        """INSERT INTO settings(key, value, updated_at)
                           VALUES(?, ?, CURRENT_TIMESTAMP)
                           ON CONFLICT(key) DO UPDATE SET
                               value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                        (compatibility_key, compatibility_value),
                    )
                account_rows = conn.execute(
                    "SELECT key, value FROM account_settings WHERE account_id=?",
                    (owner,),
                ).fetchall()
                for setting in account_rows:
                    key = str(setting["key"])
                    if (
                        key in SECRET_ACCOUNT_SETTING_KEYS
                        or not key.startswith(ACCOUNT_SETTING_PREFIXES)
                    ):
                        continue
                    conn.execute(
                        """INSERT INTO settings(key, value, updated_at)
                           VALUES(?, ?, CURRENT_TIMESTAMP)
                           ON CONFLICT(key) DO UPDATE SET
                               value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                        (key, setting["value"]),
                    )
                result = dict(row)
                result["selected"] = True
                return result
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to select Telegram account: {exc}") from exc

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

    def delete_telegram_account_data(
        self, account_id: object
    ) -> dict[str, Any]:
        """Delete every account-scoped database row without touching other accounts."""

        owner = _positive_account_id(account_id)
        safe_name = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                account = self._account_row(conn, owner)
                if account is None:
                    raise DatabaseError("Telegram account does not exist")
                session_name = str(account["session_name"] or "")
                selected = self.get_selected_account_id()
                previous = self.get_previous_selected_account_id()

                table_rows = conn.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
                ).fetchall()
                account_tables: list[str] = []
                tables = {str(row[0]) for row in table_rows}
                for table in sorted(tables):
                    if table == "telegram_accounts" or not safe_name.fullmatch(table):
                        continue
                    columns = {
                        str(column[1])
                        for column in conn.execute(f'PRAGMA table_info("{table}")')
                    }
                    if "account_id" in columns:
                        account_tables.append(table)

                # Schedule rows are account-owned through their campaigns rather
                # than through a direct account_id column.
                if {"comment_schedule", "comment_campaigns"} <= tables:
                    conn.execute(
                        """DELETE FROM comment_schedule
                           WHERE campaign_id IN (
                               SELECT id FROM comment_campaigns WHERE account_id=?
                           )""",
                        (owner,),
                    )
                if {"join_schedule", "join_campaigns"} <= tables:
                    conn.execute(
                        """DELETE FROM join_schedule
                           WHERE campaign_id IN (
                               SELECT id FROM join_campaigns WHERE account_id=?
                           )""",
                        (owner,),
                    )

                remaining = list(account_tables)
                while remaining:
                    deferred: list[str] = []
                    progress = False
                    for table in remaining:
                        try:
                            conn.execute(
                                f'DELETE FROM "{table}" WHERE account_id=?',
                                (owner,),
                            )
                            progress = True
                        except sqlite3.IntegrityError:
                            deferred.append(table)
                    if not deferred:
                        break
                    if not progress:
                        raise DatabaseError(
                            "Account data has unresolved foreign-key dependencies: "
                            + ", ".join(sorted(deferred))
                        )
                    remaining = deferred

                deleted = conn.execute(
                    """DELETE FROM telegram_accounts
                       WHERE telegram_account_id=?""",
                    (owner,),
                )
                if deleted.rowcount != 1:
                    raise DatabaseError("Telegram account row was not deleted")

                remaining_ids = [
                    int(row[0])
                    for row in conn.execute(
                        """SELECT telegram_account_id FROM telegram_accounts
                           ORDER BY id ASC"""
                    ).fetchall()
                ]
                selected_exists = selected in remaining_ids
                previous_exists = previous in remaining_ids
                next_selected = (
                    selected
                    if selected_exists
                    else previous
                    if previous_exists
                    else remaining_ids[0]
                    if remaining_ids
                    else 0
                )
                next_previous = (
                    previous
                    if previous_exists and previous != next_selected
                    else 0
                )
                for key, value in (
                    ("ui.selected_account_id", next_selected),
                    ("ui.previous_selected_account_id", next_previous),
                ):
                    conn.execute(
                        """INSERT INTO settings(key, value, updated_at)
                           VALUES(?, ?, CURRENT_TIMESTAMP)
                           ON CONFLICT(key) DO UPDATE SET
                               value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                        (key, str(value) if value > 0 else ""),
                    )

                if selected == owner:
                    conn.execute(
                        """DELETE FROM settings
                           WHERE key LIKE 'telegram.%'
                              OR key LIKE 'automation.%'
                              OR key LIKE 'commenting.%'
                              OR key LIKE 'openai.%'
                              OR key LIKE 'scheduler.%'"""
                    )
                    if next_selected > 0:
                        next_row = self._account_row(conn, next_selected)
                        if next_row is None:
                            raise DatabaseError(
                                "Replacement selected account disappeared"
                            )
                        compatibility = {
                            "telegram.account_id": str(next_selected),
                            "telegram.account_name": str(
                                next_row["display_name"] or "Telegram Account"
                            ),
                            "telegram.account_username": str(
                                next_row["username"] or ""
                            ),
                            "telegram.authorized": (
                                "1" if bool(next_row["authorized"]) else "0"
                            ),
                        }
                        for compatibility_key, compatibility_value in compatibility.items():
                            conn.execute(
                                """INSERT INTO settings(key, value, updated_at)
                                   VALUES(?, ?, CURRENT_TIMESTAMP)
                                   ON CONFLICT(key) DO UPDATE SET
                                       value=excluded.value,
                                       updated_at=CURRENT_TIMESTAMP""",
                                (compatibility_key, compatibility_value),
                            )
                        rows = conn.execute(
                            """SELECT key, value FROM account_settings
                               WHERE account_id=?""",
                            (next_selected,),
                        ).fetchall()
                        for setting in rows:
                            key = str(setting["key"])
                            if key.startswith(ACCOUNT_SETTING_PREFIXES):
                                conn.execute(
                                    """INSERT INTO settings(
                                           key, value, updated_at)
                                       VALUES(?, ?, CURRENT_TIMESTAMP)
                                       ON CONFLICT(key) DO UPDATE SET
                                           value=excluded.value,
                                           updated_at=CURRENT_TIMESTAMP""",
                                    (key, setting["value"]),
                                )

                return {
                    "deleted_account_id": owner,
                    "session_name": session_name,
                    "selected_account_id": next_selected,
                    "remaining_accounts": len(remaining_ids),
                }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to delete Telegram account data: {exc}"
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

    def account_accepts_new_work(self, account_id: object) -> bool:
        owner = _positive_account_id(account_id)
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT a.authorized, a.stopped, a.runtime_state,
                              EXISTS(
                                  SELECT 1 FROM account_restrictions r
                                   WHERE r.account_id=a.telegram_account_id
                                     AND r.active=1
                              ) AS restriction_active
                       FROM telegram_accounts a
                       WHERE a.telegram_account_id=?""",
                    (owner,),
                ).fetchone()
                if row is None:
                    return False
                return (
                    bool(row["authorized"])
                    and not bool(row["stopped"])
                    and not bool(row["restriction_active"])
                    and str(row["runtime_state"] or "") not in {
                    "stopping",
                    "stopped",
                    "restricted",
                    "authorization_required",
                    "error",
                    }
                )
        except Exception as exc:
            raise DatabaseError(f"Failed to validate account state: {exc}") from exc

    def begin_account_stop(self, account_id: object) -> dict[str, list[int]]:
        owner = _positive_account_id(account_id)
        result: dict[str, list[int]] = {
            "comment_campaign_ids": [],
            "join_campaign_ids": [],
            "task_ids": [],
            "running_task_ids": [],
        }
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                row = self._account_row(conn, owner)
                if row is None:
                    raise DatabaseError("Telegram account does not exist")
                conn.execute(
                    """UPDATE telegram_accounts
                       SET runtime_state='stopping', stopped=0,
                           last_error=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE telegram_account_id=?""",
                    (owner,),
                )
                comment_rows = conn.execute(
                    """SELECT id FROM comment_campaigns
                       WHERE account_id=?
                         AND status IN ('running','paused','network_wait','cycle_wait')""",
                    (owner,),
                ).fetchall()
                result["comment_campaign_ids"] = [int(row["id"]) for row in comment_rows]
                for campaign_id in result["comment_campaign_ids"]:
                    conn.execute(
                        """UPDATE comment_campaigns
                           SET status='stopped', continuous=0,
                               pause_reason='Работа аккаунта остановлена пользователем',
                               network_retry_at=NULL, updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND account_id=?""",
                        (campaign_id, owner),
                    )
                    conn.execute(
                        """UPDATE comment_schedule
                           SET status='cancelled', executed_at=CURRENT_TIMESTAMP,
                               result='Работа аккаунта остановлена пользователем'
                           WHERE campaign_id=?
                             AND status IN ('pending','queued')""",
                        (campaign_id,),
                    )
                join_rows = conn.execute(
                    """SELECT id FROM join_campaigns
                       WHERE account_id=?
                         AND status IN ('running','paused','network_wait')""",
                    (owner,),
                ).fetchall()
                result["join_campaign_ids"] = [int(row["id"]) for row in join_rows]
                for campaign_id in result["join_campaign_ids"]:
                    conn.execute(
                        """UPDATE join_campaigns
                           SET status='stopped',
                               pause_reason='Работа аккаунта остановлена пользователем',
                               network_retry_at=NULL, updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND account_id=?""",
                        (campaign_id, owner),
                    )
                    conn.execute(
                        """UPDATE join_schedule
                           SET status='cancelled', executed_at=CURRENT_TIMESTAMP,
                               result='Работа аккаунта остановлена пользователем'
                           WHERE campaign_id=?
                             AND status IN ('pending','queued')""",
                        (campaign_id,),
                    )
                task_rows = conn.execute(
                    """SELECT id, status FROM tasks
                       WHERE account_id=?
                         AND status IN ('pending','paused','running','processing')""",
                    (owner,),
                ).fetchall()
                for task in task_rows:
                    task_id = int(task["id"])
                    if str(task["status"]) in {"running", "processing"}:
                        result["running_task_ids"].append(task_id)
                        conn.execute(
                            """UPDATE tasks
                               SET status_text='Остановка аккаунта запрошена',
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (task_id,),
                        )
                    else:
                        result["task_ids"].append(task_id)
                        conn.execute(
                            """UPDATE tasks
                               SET status='cancelled',
                                   error='Работа аккаунта остановлена пользователем',
                                   not_before=NULL, updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (task_id,),
                        )
                conn.execute(
                    """INSERT INTO logs(account_id, level, message, created_at)
                       VALUES(?, 'WARNING',
                              '[Аккаунт] Начата безопасная остановка всей работы',
                              CURRENT_TIMESTAMP)""",
                    (owner,),
                )
                return result
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to begin account stop: {exc}") from exc

    def finish_account_stop(
        self, account_id: object, *, error: str | None = None
    ) -> None:
        owner = _positive_account_id(account_id)
        state = "error" if error else "stopped"
        try:
            with self.get_connection() as conn:
                conn.execute(
                    """UPDATE telegram_accounts
                       SET runtime_state=?, stopped=?,
                           last_error=?, updated_at=CURRENT_TIMESTAMP
                       WHERE telegram_account_id=?""",
                    (state, 0 if error else 1, str(error or "") or None, owner),
                )
                conn.execute(
                    """INSERT INTO logs(account_id, level, message, created_at)
                       VALUES(?, ?, ?, CURRENT_TIMESTAMP)""",
                    (
                        owner,
                        "ERROR" if error else "INFO",
                        "[Аккаунт] "
                        + (
                            f"Остановка завершилась ошибкой: {error}"
                            if error
                            else "Работа аккаунта остановлена. Telegram-сессия сохранена."
                        ),
                    ),
                )
        except Exception as exc:
            raise DatabaseError(f"Failed to finalize account stop: {exc}") from exc

    def resume_account_work(self, account_id: object) -> None:
        owner = _positive_account_id(account_id)
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE telegram_accounts
                       SET stopped=0, runtime_state='connected',
                           last_error=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE telegram_account_id=? AND authorized=1""",
                    (owner,),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError("Authorized Telegram account does not exist")
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to resume account: {exc}") from exc

    def import_comment_profile_between_accounts(
        self,
        *,
        source_account_id: object,
        target_account_id: object,
        mode: str,
    ) -> dict[str, Any]:
        source = _positive_account_id(source_account_id)
        target = _positive_account_id(target_account_id)
        if source == target:
            raise DatabaseError("Source and target accounts must be different")
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"replace", "fill"}:
            raise DatabaseError("Comment import mode must be replace or fill")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                active = conn.execute(
                    """SELECT 1 FROM comment_campaigns
                       WHERE account_id=?
                         AND status IN ('running','paused','network_wait','cycle_wait')
                       LIMIT 1""",
                    (target,),
                ).fetchone()
                if active is not None:
                    raise DatabaseError(
                        "Остановите кампанию целевого аккаунта перед импортом комментариев"
                    )
                source_row = conn.execute(
                    """SELECT text_1, text_2, text_3, text_4, text_5,
                              text_6, text_7, text_8, text_9, text_10
                       FROM account_comment_templates WHERE account_id=?""",
                    (source,),
                ).fetchone()
                if source_row is None:
                    raise DatabaseError("У предыдущего аккаунта нет комментариев")
                target_row = conn.execute(
                    """SELECT text_1, text_2, text_3, text_4, text_5,
                              text_6, text_7, text_8, text_9, text_10
                       FROM account_comment_templates WHERE account_id=?""",
                    (target,),
                ).fetchone()
                source_values = _normalized_slots(
                    [source_row[f"text_{index}"] for index in range(1, 11)]
                )
                target_values = _normalized_slots(
                    [target_row[f"text_{index}"] for index in range(1, 11)]
                    if target_row is not None
                    else []
                )
                if normalized_mode == "replace":
                    merged = list(source_values)
                else:
                    merged = list(target_values)
                    candidates = [
                        value
                        for value in source_values
                        if value and value not in set(merged)
                    ]
                    for index, value in enumerate(merged):
                        if not candidates:
                            break
                        if not value:
                            merged[index] = candidates.pop(0)
                active_values = _active_unique(merged)
                values = [value or None for value in merged]
                conn.execute(
                    """INSERT INTO account_comment_templates(
                           account_id, visible_count,
                           text_1, text_2, text_3, text_4, text_5,
                           text_6, text_7, text_8, text_9, text_10,
                           bag_fingerprint, bag_order_json, bag_position,
                           last_variant_index, last_used_at, updated_at)
                       VALUES(?, 10, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 0,
                              NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                       ON CONFLICT(account_id) DO UPDATE SET
                           visible_count=10,
                           text_1=excluded.text_1, text_2=excluded.text_2,
                           text_3=excluded.text_3, text_4=excluded.text_4,
                           text_5=excluded.text_5, text_6=excluded.text_6,
                           text_7=excluded.text_7, text_8=excluded.text_8,
                           text_9=excluded.text_9, text_10=excluded.text_10,
                           bag_fingerprint=excluded.bag_fingerprint,
                           bag_order_json='[]', bag_position=0,
                           last_variant_index=NULL,
                           last_used_at=CURRENT_TIMESTAMP,
                           updated_at=CURRENT_TIMESTAMP""",
                    (target, *values, _fingerprint(active_values)),
                )
                imported = sum(
                    1
                    for old, new in zip(target_values, merged)
                    if new and old != new
                )
                conn.execute(
                    """INSERT INTO logs(account_id, level, message, created_at)
                       VALUES(?, 'INFO', ?, CURRENT_TIMESTAMP)""",
                    (
                        target,
                        f"[Импорт] Комментарии скопированы из аккаунта {source}: "
                        f"режим={normalized_mode}, изменено={imported}",
                    ),
                )
                return {
                    "source_account_id": source,
                    "target_account_id": target,
                    "mode": normalized_mode,
                    "imported": imported,
                    "comments": merged,
                }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to import comments: {exc}") from exc

    def import_channels_between_accounts(
        self,
        *,
        source_account_id: object,
        target_account_id: object,
    ) -> dict[str, int]:
        source = _positive_account_id(source_account_id)
        target = _positive_account_id(target_account_id)
        if source == target:
            raise DatabaseError("Source and target accounts must be different")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                active = conn.execute(
                    """SELECT 1 FROM comment_campaigns
                       WHERE account_id=?
                         AND status IN ('running','paused','network_wait','cycle_wait')
                       LIMIT 1""",
                    (target,),
                ).fetchone()
                if active is not None:
                    raise DatabaseError(
                        "Остановите кампанию целевого аккаунта перед импортом каналов"
                    )
                source_rows = conn.execute(
                    """SELECT channel_id, username, title, target_kind, comment_mode,
                              linked_chat_id, linked_chat_title, access_hash, peer_type
                       FROM channels
                       WHERE account_id=? AND target_kind IN ('channel','group')
                       ORDER BY id""",
                    (source,),
                ).fetchall()
                imported = 0
                existing = 0
                skipped = 0
                for row in source_rows:
                    channel_id = int(row["channel_id"] or 0)
                    if channel_id == 0:
                        skipped += 1
                        continue
                    found = conn.execute(
                        """SELECT 1 FROM channels
                           WHERE account_id=? AND channel_id=?""",
                        (target, channel_id),
                    ).fetchone()
                    if found is not None:
                        existing += 1
                        continue
                    conn.execute(
                        """INSERT INTO channels(
                               account_id, channel_id, username, title, target_kind,
                               comment_mode, linked_chat_id, linked_chat_title,
                               link_status, link_checked_at, last_sync_at,
                               last_comment_check_at, access_hash, peer_type,
                               negative_status, negative_until,
                               local_ban_reason, local_ban_peer_id, local_banned_at,
                               created_at)
                           VALUES(?, ?, ?, ?, ?, 'pending', ?, ?,
                                  'Импортировано; требуется повторная проверка доступа',
                                  NULL, NULL, NULL, ?, ?, NULL, NULL,
                                  NULL, NULL, NULL, CURRENT_TIMESTAMP)""",
                        (
                            target,
                            channel_id,
                            row["username"],
                            row["title"],
                            row["target_kind"],
                            row["linked_chat_id"],
                            row["linked_chat_title"],
                            row["access_hash"],
                            row["peer_type"],
                        ),
                    )
                    imported += 1
                conn.execute(
                    """INSERT INTO logs(account_id, level, message, created_at)
                       VALUES(?, 'INFO', ?, CURRENT_TIMESTAMP)""",
                    (
                        target,
                        f"[Импорт] Каналы скопированы из аккаунта {source}: "
                        f"импортировано={imported}, существовало={existing}, "
                        f"пропущено={skipped}. Участие и доступ будут проверены заново.",
                    ),
                )
                return {
                    "imported": imported,
                    "existing": existing,
                    "skipped": skipped,
                }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to import channels: {exc}") from exc

    def for_account(self, account_id: object):
        from storage.account_database_view import AccountDatabaseView

        return AccountDatabaseView(
            cast(Any, self), _positive_account_id(account_id)
        )

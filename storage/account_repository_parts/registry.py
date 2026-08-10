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

class AccountRegistryRepositoryMixin:
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

                # saved_dialogs is a global Telegram peer catalog, but its
                # provenance must never retain the identity/phone of a deleted
                # account. Reassign provenance to another account that still has
                # a membership row for the peer, otherwise clear it.
                if {"saved_dialogs", "saved_dialog_memberships"} <= tables:
                    conn.execute(
                        """UPDATE saved_dialogs
                           SET source_account_id=(
                                   SELECT m.account_id
                                   FROM saved_dialog_memberships m
                                   WHERE m.saved_dialog_id=saved_dialogs.id
                                     AND m.account_id<>?
                                   ORDER BY m.updated_at DESC, m.account_id ASC
                                   LIMIT 1
                               ),
                               source_phone=NULL
                           WHERE source_account_id=?""",
                        (owner, owner),
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
    def for_account(self, account_id: object):
        from storage.account_database_view import AccountDatabaseView

        return AccountDatabaseView(
            cast(Any, self), _positive_account_id(account_id)
        )

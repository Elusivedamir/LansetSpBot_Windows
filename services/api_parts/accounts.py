from __future__ import annotations

import re
import secrets
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import TYPE_CHECKING, Any, cast

from core.account_limits import (
    MAX_REGISTERED_TELEGRAM_ACCOUNTS,
    account_limit_message,
)
from services.account_context import (
    SECRET_SETTING_KEYS,
    account_secret_key,
)
from services.account_sessions import (
    clear_account_lifecycle_journal,
    discard_pending_session,
    finalize_pending_session,
    replace_pending_session,
    rollback_finalized_session,
    stage_account_session_removal,
    update_account_lifecycle_journal,
    validate_session_name,
    write_account_lifecycle_journal,
)

if TYPE_CHECKING:  # pragma: no cover
    from core.mixin_host import MixinHost as _MixinHost
else:
    class _MixinHost:
        pass


PENDING_SESSION_RE = re.compile(r"^pending_[a-f0-9]{16,64}$")


class AccountsAPIMixin(_MixinHost):
    MAX_TELEGRAM_ACCOUNTS = MAX_REGISTERED_TELEGRAM_ACCOUNTS

    def list_telegram_accounts(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.database.list_telegram_accounts())

    def get_selected_account_id(self) -> int:
        return int(self.database.get_selected_account_id() or 0)

    def get_previous_selected_account_id(self) -> int:
        return int(self.database.get_previous_selected_account_id() or 0)

    def can_add_telegram_account(self) -> dict[str, Any]:
        count = int(self.database.count_telegram_accounts())
        return {
            "count": count,
            "limit": self.MAX_TELEGRAM_ACCOUNTS,
            "allowed": count < self.MAX_TELEGRAM_ACCOUNTS,
            "message": (
                ""
                if count < self.MAX_TELEGRAM_ACCOUNTS
                else account_limit_message(self.MAX_TELEGRAM_ACCOUNTS)
            ),
        }

    def select_telegram_account(self, account_id: int) -> dict[str, Any]:
        result = cast(
            dict[str, Any], self.database.select_telegram_account(account_id)
        )
        # TelegramAccountRuntimeManager owns one isolated Telethon runtime per
        # task account. Changing the GUI-selected account only changes the
        # compatibility/default context; it must not stop unrelated runtimes.
        return result

    def _strict_account_secret(
        self, account_id: int, key: str
    ) -> str | None:
        namespaced = account_secret_key(account_id, key)
        getter = getattr(type(self.secret_store), "get_strict_optional", None)
        if callable(getter):
            value = self.secret_store.get_strict_optional(namespaced)
        else:
            value = self.secret_store.get(namespaced, "") or None
        return None if value in (None, "") else str(value)

    def _set_account_secret(
        self, account_id: int, key: str, value: object
    ) -> None:
        self.secret_store.set(
            account_secret_key(account_id, key),
            None if value in (None, "") else str(value),
        )

    def get_account_settings(
        self, account_id: int | None = None
    ) -> dict[str, Any]:
        owner = int(account_id or self.get_selected_account_id() or 0)
        if owner <= 0:
            return {}
        account = self.database.get_telegram_account(owner)
        if not account:
            return {}
        values = cast(
            dict[str, Any], self.database.get_account_settings(owner)
        )
        for key in SECRET_SETTING_KEYS:
            values.pop(key, None)
        values.update(
            {
                "telegram.account_id": str(owner),
                "telegram.account_name": str(
                    account.get("display_name") or "Telegram Account"
                ),
                "telegram.account_username": str(account.get("username") or ""),
                "telegram.authorized": "1" if account.get("authorized") else "0",
                "telegram.session_name": str(account.get("session_name") or ""),
                "telegram.runtime_state": str(
                    account.get("runtime_state") or "disconnected"
                ),
            }
        )
        with self._secret_lock:
            for key in SECRET_SETTING_KEYS:
                value = self._strict_account_secret(owner, key)
                if value:
                    values[key] = value
        return values

    def save_account_settings(
        self,
        values: dict[str, Any],
        *,
        account_id: int | None = None,
    ) -> None:
        owner = int(account_id or self.get_selected_account_id() or 0)
        if owner <= 0:
            raise ValueError("Сначала выберите Telegram-аккаунт")
        if not self.database.get_telegram_account(owner):
            raise ValueError("Telegram-аккаунт не найден")
        public = dict(values)
        secret_updates = {
            key: public.pop(key)
            for key in SECRET_SETTING_KEYS
            if key in public
        }
        for key in (
            "telegram.account_id",
            "telegram.account_name",
            "telegram.account_username",
            "telegram.authorized",
            "telegram.session_name",
            "telegram.runtime_state",
        ):
            public.pop(key, None)
        with self._secret_lock:
            snapshots = {
                key: self._strict_account_secret(owner, key)
                for key in secret_updates
            }
            touched: list[str] = []
            try:
                for key, value in secret_updates.items():
                    touched.append(key)
                    self._set_account_secret(owner, key, value)
                if public:
                    self.database.set_account_settings(owner, public)
                if owner == self.get_selected_account_id():
                    # Preserve compatibility for current GUI methods while all
                    # worker handlers use AccountDatabaseView.
                    self.database.set_settings(public)
            except BaseException:
                for key in reversed(touched):
                    self._set_account_secret(owner, key, snapshots[key])
                raise

    def _split_authorized_account_settings(
        self, settings: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        public = dict(settings)
        secret_updates = {
            key: public.pop(key)
            for key in SECRET_SETTING_KEYS
            if key in public
        }
        for identity_key in (
            "telegram.account_id",
            "telegram.account_name",
            "telegram.account_username",
            "telegram.authorized",
            "telegram.session_name",
            "telegram.runtime_state",
        ):
            public.pop(identity_key, None)
        return public, secret_updates

    def _reauthorize_account(
        self,
        *,
        telegram_id: int,
        account: dict[str, Any],
        existing: dict[str, Any],
        pending: str,
        public: dict[str, Any],
        secret_updates: dict[str, Any],
    ) -> dict[str, Any]:
        safe_states = {
            "stopped",
            "authorization_required",
            "error",
            "disconnected",
        }
        if (
            not bool(existing.get("stopped"))
            and str(existing.get("runtime_state") or "") not in safe_states
        ):
            discard_pending_session(self.config.telegram.session_dir, pending)
            raise RuntimeError(
                "Сначала остановите работу аккаунта перед переподключением"
            )

        old_public = self.database.get_account_settings(telegram_id)
        with self._secret_lock:
            old_secrets = {
                key: self._strict_account_secret(telegram_id, key)
                for key in SECRET_SETTING_KEYS
            }
        swap_name = f".swap_account_{telegram_id}_{secrets.token_hex(12)}"
        journal = write_account_lifecycle_journal(
            self.secret_store,
            account_id=telegram_id,
            operation="reauthorize",
            pending_session_name=pending,
            final_session_name=f"account_{telegram_id}",
            swap_name=swap_name,
            old_account=dict(existing),
            old_public=dict(old_public),
            old_secrets=dict(old_secrets),
            selected_before=self.get_selected_account_id(),
            secret_keys=sorted(SECRET_SETTING_KEYS),
        )
        try:
            with replace_pending_session(
                self.config.telegram.session_dir,
                pending_session_name=pending,
                telegram_account_id=telegram_id,
                swap_name=swap_name,
            ) as final_name:
                journal = update_account_lifecycle_journal(
                    self.secret_store, journal, phase="session_swapped"
                )
                with self.database.get_connection():
                    self.database.register_telegram_account(
                        telegram_account_id=telegram_id,
                        session_name=final_name,
                        display_name=str(
                            account.get("name")
                            or existing.get("display_name")
                            or "Telegram Account"
                        ),
                        username=(
                            str(account.get("username") or "").strip() or None
                        ),
                        phone=str(account.get("phone") or ""),
                        authorized=True,
                    )
                    self.database.update_account_session_name(
                        telegram_id, final_name
                    )
                    self.database.replace_account_settings(
                        telegram_id, public
                    )
                    with self._secret_lock:
                        for key, value in secret_updates.items():
                            self._set_account_secret(telegram_id, key, value)
                    self.database.resume_account_work(telegram_id)
                    selected = cast(
                        dict[str, Any],
                        self.database.select_telegram_account(telegram_id),
                    )
                journal = update_account_lifecycle_journal(
                    self.secret_store, journal, phase="committed"
                )
        except BaseException:
            rollback_error = None
            try:
                self.database.replace_account_settings(
                    telegram_id, old_public
                )
            except Exception as exc:
                rollback_error = exc
            try:
                with self._secret_lock:
                    for key, value in old_secrets.items():
                        self._set_account_secret(telegram_id, key, value)
                discard_pending_session(
                    self.config.telegram.session_dir, pending
                )
            except Exception as exc:
                if rollback_error is None:
                    rollback_error = exc
            if rollback_error is None:
                clear_account_lifecycle_journal(
                    self.secret_store, telegram_id
                )
                raise
            raise RuntimeError(
                "Telegram account rollback is incomplete; "
                "startup recovery journal was retained"
            ) from rollback_error

        clear_account_lifecycle_journal(self.secret_store, telegram_id)
        selected["created"] = False
        selected["duplicate"] = True
        selected["reauthorized"] = True
        return selected

    def _register_new_authorized_account(
        self,
        *,
        telegram_id: int,
        account: dict[str, Any],
        pending: str,
        public: dict[str, Any],
        secret_updates: dict[str, Any],
    ) -> dict[str, Any]:
        check = self.can_add_telegram_account()
        if not bool(check["allowed"]):
            discard_pending_session(self.config.telegram.session_dir, pending)
            raise ValueError(str(check["message"]))

        final_name = f"account_{telegram_id}"
        journal = write_account_lifecycle_journal(
            self.secret_store,
            account_id=telegram_id,
            operation="register",
            pending_session_name=pending,
            final_session_name=final_name,
            selected_before=self.get_selected_account_id(),
            secret_keys=sorted(secret_updates),
        )
        moved = False
        created = False
        try:
            final_name = finalize_pending_session(
                self.database,
                self.config.telegram.session_dir,
                pending_session_name=pending,
                telegram_account_id=telegram_id,
            )
            moved = True
            journal = update_account_lifecycle_journal(
                self.secret_store, journal, phase="session_moved"
            )
            with self.database.get_connection():
                _row, created = self.database.register_telegram_account(
                    telegram_account_id=telegram_id,
                    session_name=final_name,
                    display_name=str(account.get("name") or "Telegram Account"),
                    username=str(account.get("username") or "").strip() or None,
                    phone=str(account.get("phone") or ""),
                    authorized=True,
                )
                self.database.update_account_session_name(
                    telegram_id, final_name
                )
                self.database.replace_account_settings(telegram_id, public)
                with self._secret_lock:
                    for key, value in secret_updates.items():
                        self._set_account_secret(telegram_id, key, value)
                selected = cast(
                    dict[str, Any],
                    self.database.select_telegram_account(telegram_id),
                )
            journal = update_account_lifecycle_journal(
                self.secret_store, journal, phase="committed"
            )
            clear_account_lifecycle_journal(self.secret_store, telegram_id)
            selected["created"] = created
            return selected
        except BaseException:
            if created:
                self.database.rollback_new_telegram_account(
                    telegram_id, expected_session_name=final_name
                )
            with self._secret_lock:
                for key in secret_updates:
                    self._set_account_secret(telegram_id, key, None)
            if moved:
                rollback_finalized_session(
                    self.config.telegram.session_dir,
                    pending_session_name=pending,
                    telegram_account_id=telegram_id,
                )
            discard_pending_session(
                self.config.telegram.session_dir, pending
            )
            clear_account_lifecycle_journal(self.secret_store, telegram_id)
            raise

    def register_authorized_account(
        self,
        account: dict[str, Any],
        settings: dict[str, Any],
        *,
        pending_session_name: str,
    ) -> dict[str, Any]:
        telegram_id = int(account.get("id") or 0)
        if telegram_id <= 0:
            raise ValueError("Telegram не вернул корректный account ID")
        pending = validate_session_name(pending_session_name)
        if not PENDING_SESSION_RE.fullmatch(pending):
            raise ValueError("Новый аккаунт должен использовать временную сессию")

        public, secret_updates = self._split_authorized_account_settings(settings)
        existing = self.database.get_telegram_account(telegram_id)
        if existing:
            return self._reauthorize_account(
                telegram_id=telegram_id,
                account=account,
                existing=existing,
                pending=pending,
                public=public,
                secret_updates=secret_updates,
            )
        return self._register_new_authorized_account(
            telegram_id=telegram_id,
            account=account,
            pending=pending,
            public=public,
            secret_updates=secret_updates,
        )

    def update_authorized_account_metadata(
        self, account: dict[str, Any]
    ) -> dict[str, Any]:
        telegram_id = int(account.get("id") or 0)
        existing = self.database.get_telegram_account(telegram_id)
        if not existing:
            raise ValueError("Telegram-аккаунт не зарегистрирован")
        raw_row, _created = self.database.register_telegram_account(
            telegram_account_id=telegram_id,
            session_name=str(existing["session_name"]),
            display_name=str(account.get("name") or existing["display_name"]),
            username=str(account.get("username") or "").strip() or None,
            phone=str(account.get("phone") or ""),
            authorized=True,
        )
        return cast(dict[str, Any], raw_row)


    def _begin_account_stop(self, owner: int, worker: Any) -> dict[str, Any]:
        scopes = [("account", owner)]

        def mutation():
            return self.database.begin_account_stop(owner)

        if worker is not None and worker.isRunning():
            return dict(worker.cancel_scopes_and_run(scopes, mutation))
        return dict(mutation())

    @staticmethod
    def _request_account_stop_cancellations(
        worker: Any, stopped: dict[str, Any]
    ) -> None:
        if worker is None:
            return
        for campaign_id in stopped.get("comment_campaign_ids", []):
            worker.request_scope_cancellation(
                "comment_campaign", int(campaign_id)
            )
        for campaign_id in stopped.get("join_campaign_ids", []):
            worker.request_scope_cancellation("join_campaign", int(campaign_id))
        for task_id in stopped.get("running_task_ids", []):
            worker.request_scope_cancellation("task", int(task_id))

    @staticmethod
    def _disconnect_account_runtime(
        owner: int,
        worker: Any,
        *,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any], str | None]:
        disconnect_result: dict[str, Any] = {
            "account_id": owner,
            "disconnected": False,
        }
        error: str | None = None
        try:
            if worker is not None and worker.isRunning():
                future = worker.submit_utility(
                    "stop_account_runtime", {"account_id": owner}
                )
                disconnect_result = dict(
                    future.result(timeout=max(1.0, float(timeout_seconds))) or {}
                )
        except FutureTimeoutError:
            error = (
                "Telegram runtime не завершился за отведённое время; "
                "потенциально начатые отправки требуют ручной проверки"
            )
        except Exception as exc:
            error = str(exc)
        return disconnect_result, error

    def stop_telegram_account(
        self, account_id: int, *, timeout_seconds: float = 20.0
    ) -> dict[str, Any]:
        owner = int(account_id)
        if owner <= 0:
            raise ValueError("Некорректный Telegram-аккаунт")
        worker = self.queue_worker
        stopped = self._begin_account_stop(owner, worker)
        self._request_account_stop_cancellations(worker, stopped)
        disconnect_result, error = self._disconnect_account_runtime(
            owner,
            worker,
            timeout_seconds=timeout_seconds,
        )
        self.database.finish_account_stop(owner, error=error)
        if error:
            raise RuntimeError(error)
        result = dict(stopped)
        result.update(disconnect_result)
        result["message"] = (
            "Работа аккаунта остановлена. Telegram-сессия сохранена."
        )
        return result

    def resume_telegram_account(self, account_id: int) -> dict[str, Any]:
        owner = int(account_id)
        self.database.resume_account_work(owner)
        worker = self.queue_worker
        if worker is not None:
            worker.clear_scope_cancellation("account", owner)
        return self.database.get_telegram_account(owner) or {}


    def _prepare_account_deletion(
        self, owner: int, account: dict[str, Any]
    ) -> tuple[str, dict[str, str | None], str, dict[str, Any]]:
        session_name = validate_session_name(
            account.get("session_name") or f"account_{owner}"
        )
        old_public = self.database.get_account_settings(owner)
        with self._secret_lock:
            secret_snapshots = {
                key: self._strict_account_secret(owner, key)
                for key in SECRET_SETTING_KEYS
            }
        tombstone_name = f".delete_{session_name}_{secrets.token_hex(12)}"
        journal = write_account_lifecycle_journal(
            self.secret_store,
            account_id=owner,
            operation="delete",
            final_session_name=session_name,
            tombstone_name=tombstone_name,
            old_account=dict(account),
            old_public=dict(old_public),
            old_secrets=dict(secret_snapshots),
            selected_before=self.get_selected_account_id(),
            secret_keys=sorted(SECRET_SETTING_KEYS),
        )
        return session_name, secret_snapshots, tombstone_name, journal

    def _execute_account_deletion(
        self,
        *,
        owner: int,
        session_name: str,
        tombstone_name: str,
        secret_snapshots: dict[str, str | None],
        journal: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, object]]:
        try:
            with stage_account_session_removal(
                self.config.telegram.session_dir,
                session_name=session_name,
                tombstone_name=tombstone_name,
            ) as cleanup_state:
                journal = update_account_lifecycle_journal(
                    self.secret_store, journal, phase="session_staged"
                )
                with self._secret_lock:
                    for key in SECRET_SETTING_KEYS:
                        self._set_account_secret(owner, key, None)
                result = cast(
                    dict[str, Any],
                    self.database.delete_telegram_account_data(owner),
                )
                journal = update_account_lifecycle_journal(
                    self.secret_store, journal, phase="committed"
                )
        except BaseException:
            with self._secret_lock:
                for key, value in secret_snapshots.items():
                    self._set_account_secret(owner, key, value)
            clear_account_lifecycle_journal(self.secret_store, owner)
            raise
        return result, cleanup_state

    def _finalize_account_deletion(
        self,
        owner: int,
        result: dict[str, Any],
        cleanup_state: dict[str, object],
    ) -> dict[str, Any]:
        clear_account_lifecycle_journal(self.secret_store, owner)
        worker = self.queue_worker
        if worker is not None:
            worker.clear_scope_cancellation("account", owner)
        warning = str(cleanup_state.get("warning") or "")
        result["session_cleanup_warning"] = warning
        result["message"] = (
            "Аккаунт и все его локальные данные безвозвратно удалены."
            + (
                " Остаточный скрытый файл session не удалось удалить; "
                "перезапустите приложение и повторите очистку профиля."
                if warning
                else ""
            )
        )
        return result

    def delete_telegram_account(
        self, account_id: int, *, timeout_seconds: float = 20.0
    ) -> dict[str, Any]:
        owner = int(account_id)
        account = self.database.get_telegram_account(owner)
        if not account:
            raise ValueError("Telegram-аккаунт не найден")
        self.stop_telegram_account(
            owner, timeout_seconds=timeout_seconds
        )
        account = self.database.get_telegram_account(owner) or account

        session_name, secret_snapshots, tombstone_name, journal = (
            self._prepare_account_deletion(owner, account)
        )
        result, cleanup_state = self._execute_account_deletion(
            owner=owner,
            session_name=session_name,
            tombstone_name=tombstone_name,
            secret_snapshots=secret_snapshots,
            journal=journal,
        )
        return self._finalize_account_deletion(
            owner, result, cleanup_state
        )

    def check_telegram_account_runtime(
        self, account_id: int, *, timeout_seconds: float = 20.0
    ) -> dict[str, Any]:
        owner = int(account_id)
        worker = self.queue_worker
        if worker is None:
            raise RuntimeError("Фоновый обработчик не создан")
        self.database.resume_account_work(owner)
        worker.clear_scope_cancellation("account", owner)
        if not worker.isRunning():
            self.start_queue()
        future = worker.submit_utility(
            "check_account_runtime", {"account_id": owner}
        )
        return dict(
            future.result(timeout=max(1.0, float(timeout_seconds))) or {}
        )

    def import_comments_from_previous_account(
        self, *, mode: str
    ) -> dict[str, Any]:
        target = self.get_selected_account_id()
        source = self.get_previous_selected_account_id()
        if target <= 0 or source <= 0 or source == target:
            raise ValueError(
                "Сначала переключитесь с другого подключённого аккаунта."
            )
        return cast(
            dict[str, Any],
            self.database.import_comment_profile_between_accounts(
                source_account_id=source,
                target_account_id=target,
                mode=mode,
            ),
        )

    def import_channels_from_previous_account(self) -> dict[str, int]:
        target = self.get_selected_account_id()
        source = self.get_previous_selected_account_id()
        if target <= 0 or source <= 0 or source == target:
            raise ValueError(
                "Сначала переключитесь с другого подключённого аккаунта."
            )
        return cast(
            dict[str, int],
            self.database.import_channels_between_accounts(
                source_account_id=source,
                target_account_id=target,
            ),
        )

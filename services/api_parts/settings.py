from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import logging

from core.activity_schedule import (
    QUIET_END_KEY,
    QUIET_START_KEY,
    SCHEDULE_ENABLED_KEY,
    SCHEDULE_SETTINGS_PREFIX,
    TIMEZONE_KEY,
    format_clock,
    normalize_bool,
    parse_clock,
    validate_timezone_name,
)

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:
    class _MixinHost:
        pass


class SettingsAPIMixin(_MixinHost):
    def _strict_secret_snapshot(
        self, key: str, *, account_id: int | None = None
    ) -> str | None:
        owner = int(account_id or 0)
        if owner > 0 and hasattr(self, "_strict_account_secret"):
            return cast(
                str | None,
                self._strict_account_secret(owner, key),
            )
        getter = getattr(type(self.secret_store), "get_strict_optional", None)
        if callable(getter):
            value = self.secret_store.get_strict_optional(key)
            return None if value is None else str(value)
        value = self.secret_store.get(key, "")
        return None if value in (None, "") else str(value)

    def _restore_secret_snapshot(
        self,
        key: str,
        value: str | None,
        *,
        account_id: int | None = None,
    ) -> None:
        owner = int(account_id or 0)
        if owner > 0 and hasattr(self, "_set_account_secret"):
            self._set_account_secret(owner, key, value)
            return
        if value is None:
            self.secret_store.delete(key)
        else:
            self.secret_store.set(key, value)

    def save_settings(self, values: dict[str, Any]) -> None:
        """Persist settings in the selected account without affecting runtimes."""

        if not isinstance(values, dict):
            raise ValueError("Settings must be an object")
        public = dict(values)

        if SCHEDULE_ENABLED_KEY in public:
            public[SCHEDULE_ENABLED_KEY] = (
                "1" if normalize_bool(public[SCHEDULE_ENABLED_KEY]) else "0"
            )
        if TIMEZONE_KEY in public:
            public[TIMEZONE_KEY] = validate_timezone_name(public[TIMEZONE_KEY])
        if QUIET_START_KEY in public:
            public[QUIET_START_KEY] = format_clock(
                parse_clock(public[QUIET_START_KEY], default="22:00")
            )
        if QUIET_END_KEY in public:
            public[QUIET_END_KEY] = format_clock(
                parse_clock(public[QUIET_END_KEY], default="07:00")
            )

        owner = self.get_current_account_id()
        target_db = self.database.for_account(owner) if owner > 0 else self.database

        if QUIET_START_KEY in public or QUIET_END_KEY in public:
            stored = target_db.get_settings(SCHEDULE_SETTINGS_PREFIX)
            enabled_raw = public.get(
                SCHEDULE_ENABLED_KEY, stored.get(SCHEDULE_ENABLED_KEY)
            )
            if normalize_bool(enabled_raw):
                start_value = public.get(
                    QUIET_START_KEY, stored.get(QUIET_START_KEY)
                )
                end_value = public.get(
                    QUIET_END_KEY, stored.get(QUIET_END_KEY)
                )
                start = parse_clock(start_value, default="22:00")
                end = parse_clock(end_value, default="07:00")
                if start == end:
                    raise ValueError(
                        "Начало и конец тихих часов совпадают: активного окна не "
                        "останется и отправка никогда не возобновится. "
                        "Задайте разное время или отключите расписание."
                    )

        secret_updates = {
            key: public.pop(key)
            for key in self.SECRET_SETTING_KEYS
            if key in public
        }
        identity_keys = {
            "telegram.account_id",
            "telegram.account_name",
            "telegram.account_username",
            "telegram.authorized",
            "telegram.session_name",
            "telegram.runtime_state",
        }
        for key in identity_keys:
            public.pop(key, None)

        if not secret_updates:
            if public:
                target_db.set_settings(public)
                if owner > 0:
                    # Current global keys remain a selected-account compatibility
                    # mirror; workers never read them directly after v31.
                    self.database.set_settings(public)
            return

        with self._secret_lock:
            snapshots = {
                key: self._strict_secret_snapshot(key, account_id=owner or None)
                for key in secret_updates
            }
            touched: list[str] = []
            try:
                with self.database.get_connection():
                    for key, value in secret_updates.items():
                        touched.append(key)
                        if owner > 0:
                            self._set_account_secret(owner, key, value)
                        else:
                            self.secret_store.set(key, value)
                            self.database.delete_setting(key)
                    if public:
                        target_db.set_settings(public)
                        if owner > 0:
                            self.database.set_settings(public)
                if not any(
                    self.database.get_setting(key, "")
                    for key in self.SECRET_SETTING_KEYS
                ):
                    self._secret_migration_required.clear()
            except BaseException as exc:
                rollback_errors: list[str] = []
                for key in reversed(touched):
                    try:
                        self._restore_secret_snapshot(
                            key, snapshots[key], account_id=owner or None
                        )
                    except Exception as rollback_exc:
                        rollback_errors.append(f"{key}: {rollback_exc}")
                if rollback_errors:
                    raise RuntimeError(
                        f"Настройки не сохранены: {exc}; откат защищённых данных "
                        f"также завершился ошибкой: {'; '.join(rollback_errors)}"
                    ) from exc
                raise

    def get_current_account_id(self) -> int:
        getter = getattr(self.database, "get_selected_account_id", None)
        if callable(getter):
            value = int(getter() or 0)
            if value > 0:
                return value
        try:
            return max(
                0,
                int(self.database.get_setting("telegram.account_id", 0) or 0),
            )
        except (TypeError, ValueError, OverflowError):
            return 0

    def get_settings(self, prefix: str | None = None) -> dict[str, Any]:
        owner = self.get_current_account_id()
        if owner > 0 and hasattr(self, "get_account_settings"):
            values = dict(self.get_account_settings(owner))
            if prefix:
                values = {
                    key: value
                    for key, value in values.items()
                    if key.startswith(prefix)
                }
            return cast(dict[str, Any], values)

        values = self.database.get_settings(prefix)
        with self._secret_lock:
            for key in self.SECRET_SETTING_KEYS:
                if prefix is None or key.startswith(prefix):
                    secret = self._strict_secret_snapshot(key)
                    if secret:
                        values[key] = secret
        return cast(dict[str, Any], values)

    def save_comment_template(self, comments: list[str]) -> None:
        normalized = [str(item).strip() for item in comments[:10]]
        normalized += [""] * (10 - len(normalized))
        self.database.save_account_comment_profile(
            normalized,
            visible_count=10,
            account_id=self.get_current_account_id() or None,
        )

    def get_main_comments(self) -> list[str]:
        profile = self.database.get_account_comment_profile(
            account_id=self.get_current_account_id() or None,
            touch=True,
        )
        comments = list(profile.get("comments") or [])[:10]
        comments += [""] * (10 - len(comments))
        return comments

    def save_comment_profile(
        self,
        comments: list[str],
        *,
        visible_count: int,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        owner = int(account_id or self.get_current_account_id() or 0)
        return cast(
            dict[str, Any],
            self.database.save_account_comment_profile(
                comments,
                visible_count=visible_count,
                account_id=owner or None,
            ),
        )

    def get_comment_profile(
        self, account_id: int | None = None
    ) -> dict[str, Any]:
        owner = int(account_id or self.get_current_account_id() or 0)
        return cast(
            dict[str, Any],
            self.database.get_account_comment_profile(
                account_id=owner or None,
                touch=True,
            ),
        )

    def import_previous_comment_profile(
        self, account_id: int | None = None
    ) -> dict[str, Any] | None:
        del account_id
        return cast(
            dict[str, Any] | None,
            self.import_comments_from_previous_account(mode="replace"),
        )

    def get_comment_history(
        self,
        task_id: int | None = None,
        limit: int = 100,
        campaign_id: int | None = None,
        account_id: int | None = None,
    ):
        owner = int(account_id or self.get_current_account_id() or 0)
        return self.database.get_comment_history(
            task_id=task_id,
            limit=limit,
            campaign_id=campaign_id,
            account_id=owner or None,
        )

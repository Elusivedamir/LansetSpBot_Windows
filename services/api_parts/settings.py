from __future__ import annotations

from typing import TYPE_CHECKING

import logging

from core.activity_schedule import (
    QUIET_END_KEY,
    QUIET_START_KEY,
    SCHEDULE_ENABLED_KEY,
    TIMEZONE_KEY,
    format_clock,
    normalize_bool,
    parse_clock,
    validate_timezone_name,
)
from core.profile_backup import create_profile_backup, inspect_profile_backup
from services.telegram_session import TelegramSessionMixin
from pathlib import Path
from typing import Any, cast


log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class SettingsAPIMixin(_MixinHost):
    def _strict_secret_snapshot(self, key: str) -> str | None:
        getter = getattr(type(self.secret_store), "get_strict_optional", None)
        if callable(getter):
            value = self.secret_store.get_strict_optional(key)
            return None if value is None else str(value)
        value = self.secret_store.get(key, "")
        return None if value in (None, "") else str(value)

    def _restore_secret_snapshot(self, key: str, value: str | None) -> None:
        if value is None:
            self.secret_store.delete(key)
        else:
            self.secret_store.set(key, value)

    def save_settings(self, values: dict[str, Any]) -> None:
        """Persist public settings and protected credentials as one logical unit.

        SQLite and the local secret file cannot share a native transaction. Marlen
        therefore snapshots every affected credential, executes all SQLite writes
        in one transaction, and restores the local secret file if a credential write
        or SQLite commit fails.
        """

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

        session_backup_policy = public.get("telegram.session_backup_enabled")
        if session_backup_policy is not None:
            normalized_policy = str(session_backup_policy).strip().lower()
            if normalized_policy not in {
                "0",
                "1",
                "false",
                "true",
                "no",
                "yes",
                "off",
                "on",
            }:
                raise ValueError("Некорректная политика резервирования Telegram-сессии")
            enabled = normalized_policy in {"1", "true", "yes", "on"}
            public["telegram.session_backup_enabled"] = "1" if enabled else "0"
            if not enabled:
                session_file = self.database.path.parent / "sessions" / "main.session"
                TelegramSessionMixin.purge_session_backups(session_file)
        secret_updates = {
            key: public.pop(key) for key in self.SECRET_SETTING_KEYS if key in public
        }
        if not secret_updates:
            if public:
                self.database.set_settings(public)
            return

        with self._secret_lock:
            snapshots = {
                key: self._strict_secret_snapshot(key) for key in secret_updates
            }
            touched: list[str] = []
            try:
                # Nested repository calls reuse this outer SQLite transaction.
                # A commit failure is raised from this context and triggers the
                # same local-secret rollback as an in-body database failure.
                with self.database.get_connection():
                    for key, value in secret_updates.items():
                        touched.append(key)
                        self.secret_store.set(key, value)
                        self.database.delete_setting(key)
                    if public:
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
                        self._restore_secret_snapshot(key, snapshots[key])
                    except Exception as rollback_exc:  # noqa: BLE001
                        rollback_errors.append(f"{key}: {rollback_exc}")
                if rollback_errors:
                    raise RuntimeError(
                        f"Настройки не сохранены: {exc}; откат защищённых данных "
                        f"также завершился ошибкой: {'; '.join(rollback_errors)}"
                    ) from exc
                raise

    def get_current_account_id(self) -> int:
        try:
            return max(
                0,
                int(self.database.get_setting("telegram.account_id", 0) or 0),
            )
        except (TypeError, ValueError, OverflowError):
            return 0

    def get_settings(self, prefix: str | None = None) -> dict[str, Any]:
        values = self.database.get_settings(prefix)
        with self._secret_lock:
            for key in self.SECRET_SETTING_KEYS:
                if prefix is None or key.startswith(prefix):
                    secret = self._strict_secret_snapshot(key)
                    if secret:
                        values[key] = secret
        return cast(dict[str, Any], values)

    def create_profile_backup(
        self, destination: str | Path, *, include_sessions: bool = False
    ) -> dict[str, Any]:
        """Create a verified profile backup without blocking the GUI thread."""

        # Backup format v2 is intentionally DB-only. Credentials and live
        # Telegram sessions are never exported to an unencrypted ZIP.
        result = create_profile_backup(
            database_path=self.database.path,
            session_dir=self.database.path.parent / "sessions",
            secret_snapshot={},
            destination=Path(destination),
            include_sessions=False,
        )
        return {
            "path": str(result.path),
            "schema_version": int(result.schema_version),
            "file_count": int(result.file_count),
            "contains_sessions": bool(result.contains_sessions),
        }

    def inspect_profile_backup(self, archive_path: str | Path) -> dict[str, Any]:
        """Validate and migrate a disposable copy before scheduling restore."""

        info = inspect_profile_backup(Path(archive_path))
        return {
            "path": str(info.path),
            "schema_version": int(info.schema_version),
            "created_at": info.created_at,
            "file_count": int(info.file_count),
            "contains_sessions": bool(info.contains_sessions),
            "app_version": info.app_version,
        }

    def save_comment_template(self, comments: list[str]) -> None:
        # Compatibility entry point: current Marlen always stores ten fields.
        normalized = [str(item).strip() for item in comments[:10]]
        normalized += [""] * (10 - len(normalized))
        self.database.save_account_comment_profile(
            normalized,
            visible_count=10,
        )

    def get_main_comments(self) -> list[str]:
        profile = self.database.get_account_comment_profile(touch=True)
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
        return cast(
            dict[str, Any],
            self.database.save_account_comment_profile(
                comments,
                visible_count=visible_count,
                account_id=account_id,
            ),
        )

    def get_comment_profile(self, account_id: int | None = None) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.database.get_account_comment_profile(
                account_id=account_id,
                touch=True,
            ),
        )

    def import_previous_comment_profile(
        self, account_id: int | None = None
    ) -> dict[str, Any] | None:
        result = self.database.import_previous_account_comment_profile(
            account_id=account_id
        )
        return cast(dict[str, Any] | None, result)

    def get_comment_history(
        self,
        task_id: int | None = None,
        limit: int = 100,
        campaign_id: int | None = None,
        account_id: int | None = None,
    ):
        return self.database.get_comment_history(
            task_id=task_id,
            limit=limit,
            campaign_id=campaign_id,
            account_id=account_id,
        )

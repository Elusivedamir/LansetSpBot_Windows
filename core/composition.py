from __future__ import annotations

import logging
from typing import Any

from core.account_state import reconcile_pending_account_state
from core.config import Config, TelegramSettings
from core.factory_reset import FactoryResetResult, reset_local_state
from core.factory_reset_runtime import REQUIRED_PROFILE_TABLES, initialize_empty_profile
from core.rate_limiter import RateLimiter
from core.secret_store import SecretStore
from gui.gui_service_adapter import GUIServiceAdapter
from services.api import ServiceAPI
from services.comment_service import CommentService
from services.import_service import ImportService
from services.linked_chat_service import LinkedChatService
from services.legacy_proxy_cleanup import purge_removed_proxy_credentials
from services.telegram_service import TelegramService
from services.account_sessions import (
    migrate_legacy_account_secrets,
    migrate_legacy_main_session,
    recover_account_lifecycle,
    recover_interrupted_session_moves,
    recover_session_residues,
)
from services.account_runtime_manager import create_multiaccount_handlers
from storage.database import Database
from workers.queue_worker import QueueWorker
from workers.handler_registry import create_worker_handlers

log = logging.getLogger(__name__)


class ApplicationContainer:
    """Own long-lived GUI services and create async services inside the worker loop."""

    _REQUIRED_PROFILE_TABLES = REQUIRED_PROFILE_TABLES

    def __init__(self, config: Config) -> None:
        self.config = config
        RateLimiter.configure_process_interval(config.rate_limit)
        self.database = Database(config.database_path, busy_timeout_ms=1_000)
        reconcile_pending_account_state(self.database)
        self.secret_store = SecretStore()
        try:
            self.removed_proxy_cleanup = purge_removed_proxy_credentials(
                self.database, self.secret_store
            )
        except Exception:
            log.exception(
                "Could not complete obsolete proxy credential cleanup; "
                "network proxy remains disabled and cleanup will retry"
            )
            self.removed_proxy_cleanup = {
                "completed": False,
                "removed": 0,
            }
        self.session_recovery = recover_interrupted_session_moves(
            config.telegram.session_dir
        )
        self.account_lifecycle_recovery = recover_account_lifecycle(
            self.database,
            self.secret_store,
            config.telegram.session_dir,
        )
        self.session_residue_recovery = recover_session_residues(
            self.database,
            config.telegram.session_dir,
        )
        self.session_migration = migrate_legacy_main_session(
            self.database, config.telegram.session_dir
        )
        self.account_secret_migration = migrate_legacy_account_secrets(
            self.database, self.secret_store
        )
        self.queue_worker = QueueWorker(
            handler_factory=self._create_worker_handlers,
            max_retries=config.queue.max_retries,
            database_path=config.database_path,
            persistent_idle=True,
        )
        self.api = ServiceAPI(
            self.database,
            self.queue_worker,
            max_channels_per_run=config.max_channels_per_run,
            secret_store=self.secret_store,
            max_joins_per_hour=config.max_joins_per_hour,
            campaign_hours=config.campaign_hours,
            config=config,
            secret_migration_verified=True,
        )
        self.adapter = GUIServiceAdapter(self.api)

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
        strict_method = getattr(type(self.secret_store), "get_strict_optional", None)
        if callable(strict_method):
            return self.secret_store.get_strict_optional(key)
        # Compatibility for test doubles; production SecretStore uses the
        # strict local-file method and never masks storage failures here.
        value = self.secret_store.get(key, "")
        return None if value in (None, "") else str(value)

    def _telegram_settings(self, db: Database) -> TelegramSettings:
        saved = db.get_settings("telegram.")
        api_id = self._as_int(saved.get("telegram.api_id"), self.config.telegram.api_id)
        api_hash_value = self._strict_secret_value("telegram.api_hash")
        api_hash = str(api_hash_value or self.config.telegram.api_hash or "").strip()
        phone = (
            str(
                self._strict_secret_value("telegram.phone")
                or self.config.telegram.phone
                or ""
            ).strip()
            or None
        )
        proxy_port = self._as_int(saved.get("telegram.proxy_port"), 0) or None
        return TelegramSettings(
            api_id=api_id,
            api_hash=api_hash,
            session_dir=self.config.telegram.session_dir,
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
            expected_account_id=self._as_int(saved.get("telegram.account_id"), 0)
            or None,
        )

    def _create_worker_handlers(self):
        # Pass module-level factories explicitly so test doubles and future
        # dependency injection keep the same public seam after extraction.
        return create_multiaccount_handlers(
            self,
            create_worker_handlers=create_worker_handlers,
            TelegramService=TelegramService,
            ImportService=ImportService,
            LinkedChatService=LinkedChatService,
            CommentService=CommentService,
        )

    def finalize_shutdown(self) -> bool:
        """Release final GUI-thread ownership without performing a blocking wait.

        The coordinated Qt shutdown path calls this only after it has observed that
        the queue worker, authentication worker, background calls and secret
        migration have stopped.  Keeping this method non-blocking is important on
        The final close happens while the Qt event loop can still repaint and
        hide the window.
        """

        if self.queue_worker.isRunning():
            return False
        wait_migration = getattr(self.api, "wait_for_secret_migration", None)
        migration_stopped = (
            bool(wait_migration(0)) if callable(wait_migration) else True
        )
        if not migration_stopped:
            return False
        self.database.finalize_shutdown()
        return True

    def shutdown(self, timeout_ms: int = 45_000) -> bool:
        """Cooperatively stop worker and local migration before closing SQLite."""
        self.api.prepare_shutdown()
        worker_stopped = True
        if self.queue_worker.isRunning():
            worker_stopped = bool(self.queue_worker.stop(timeout_ms))
            if not worker_stopped:
                log.error(
                    "Queue worker did not stop within %.1f seconds", timeout_ms / 1000
                )

        wait_migration = getattr(self.api, "wait_for_secret_migration", None)
        migration_stopped = (
            bool(wait_migration(timeout_ms)) if callable(wait_migration) else True
        )
        if not migration_stopped:
            log.error(
                "Local secret migration did not stop within %.1f seconds",
                timeout_ms / 1000,
            )

        if not (worker_stopped and migration_stopped):
            return False
        self.database.finalize_shutdown()
        return True

    def factory_reset_local_data(self) -> FactoryResetResult:
        """Erase persisted state and atomically recreate an empty valid profile."""
        if self.queue_worker.isRunning():
            raise RuntimeError("Нельзя сбросить данные, пока работает очередь")
        self.database.close_thread_connection()
        return reset_local_state(
            database_path=self.config.database_path,
            paths=self.config.paths,
            secret_path=self.secret_store.fallback_path,
            post_reset_initializer=lambda: (
                ApplicationContainer._initialize_empty_local_profile(self)
            ),
        )

    def _initialize_empty_local_profile(self) -> None:
        """Create and verify the complete empty filesystem/SQLite runtime layout."""

        initialize_empty_profile(self.config)

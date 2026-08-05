from __future__ import annotations

from typing import cast

import logging
import threading

from PySide6.QtCore import QObject, QThreadPool, QTimer, Slot

from core.config import DEFAULT_MAX_CHANNELS_PER_RUN
from core.secret_store import SecretStore
from services.account_sessions import migrate_legacy_account_secrets
from gui.background import BackgroundCall
from services.api_parts import (
    AccountRestrictionAPIMixin,
    AccountsAPIMixin,
    CommentCampaignAPIMixin,
    JoinCampaignAPIMixin,
    OpenAICommentAPIMixin,
    SettingsAPIMixin,
    TaskQueueAPIMixin,
)
from storage.database import Database

log = logging.getLogger(__name__)


class ServiceAPI(
    QObject,
    AccountsAPIMixin,
    TaskQueueAPIMixin,
    SettingsAPIMixin,
    AccountRestrictionAPIMixin,
    CommentCampaignAPIMixin,
    JoinCampaignAPIMixin,
    OpenAICommentAPIMixin,
):
    """Single synchronous facade used by the Qt GUI."""

    COMMENT_DAILY_LIMIT_SETTING = "commenting.daily_limit"
    COMMENT_CHANNEL_COOLDOWN_HOURS = 24
    DELIVERY_RECONCILIATION_INTERVAL_MS = 5 * 60 * 1000
    MAINTENANCE_CHECK_INTERVAL_MS = 60 * 60 * 1000
    SECRET_SETTING_KEYS = frozenset(
        {
            "telegram.api_hash",
            "telegram.phone",
            "telegram.proxy_username",
            "telegram.proxy_password",
            "openai.api_key",
        }
    )

    ALLOWED_TASK_TYPES = frozenset(
        {
            "noop",
            "sync_channels",
            "link_channels",
            "auto_comment",
            "auto_comment_slot",
            "comment",
            "import",
            "sync_saved_dialogs",
            "join_saved_slot",
            "parse_audience",
        }
    )
    ACCOUNT_BOUND_TASK_TYPES = frozenset(
        {
            "sync_channels",
            "link_channels",
            "auto_comment",
            "auto_comment_slot",
            "comment",
            "sync_saved_dialogs",
            "join_saved_slot",
            "parse_audience",
        }
    )
    NON_IDEMPOTENT_TASK_TYPES = frozenset(
        {
            "comment",
            "auto_comment",
            "auto_comment_slot",
            "join_saved_slot",
        }
    )

    def __init__(
        self,
        database: Database,
        queue_worker=None,
        *,
        max_channels_per_run: int = DEFAULT_MAX_CHANNELS_PER_RUN,
        secret_store: SecretStore | None = None,
        max_joins_per_hour: int = 40,
        campaign_hours: int = 24,
        config=None,
        secret_migration_verified: bool = False,
    ) -> None:
        super().__init__()
        self.database = database
        self.queue_worker = queue_worker
        self.config = config
        self.max_channels_per_run = max(1, int(max_channels_per_run))
        self.max_joins_per_hour = max(1, int(max_joins_per_hour))
        self.campaign_hours = max(1, min(168, int(campaign_hours)))
        self.secret_store = secret_store or SecretStore()
        self._secret_lock = threading.RLock()
        self._secret_migration_required = threading.Event()
        self._secret_migration_thread = threading.Thread(
            target=lambda: None,
            name="marlen-secret-migration-complete",
            daemon=False,
        )
        if secret_migration_verified:
            # ApplicationContainer performs the migration synchronously before
            # the queue exists. Do not start a second racing migration thread.
            self._secret_migration_required.clear()
        else:
            # Direct/test construction remains fail-closed until its own
            # compatibility migration verifies that no SQLite secret copies remain.
            self._secret_migration_required.set()
            self._secret_migration_thread = threading.Thread(
                target=type(self)._migrate_legacy_secrets,
                args=(
                    self.database,
                    self.secret_store,
                    self.SECRET_SETTING_KEYS,
                    self._secret_lock,
                    self._secret_migration_required,
                ),
                name="marlen-secret-migration",
                daemon=False,
            )
            self._secret_migration_thread.start()
        self._secret_migration_retry_at = 0.0
        self._scheduler_failures = 0
        self._scheduler_error_present = bool(
            self.database.get_setting("scheduler.comment_error", "")
        )
        self._queue_lock = threading.RLock()
        self._restart_requested = False
        self._shutdown_requested = False
        self._last_worker_error = None
        self._auth_in_progress = False
        self._maintenance_job: BackgroundCall | None = None
        if self.queue_worker is not None:
            self.queue_worker.finished.connect(self._on_worker_finished)
            self.queue_worker.worker_error.connect(self._on_worker_error)

        self._campaign_timer = QTimer(self)
        self._campaign_timer.setInterval(10_000)
        self._campaign_timer.timeout.connect(self._campaign_tick)
        self._campaign_timer.start()

        self._delivery_recovery_timer = QTimer(self)
        self._delivery_recovery_timer.setInterval(
            self.DELIVERY_RECONCILIATION_INTERVAL_MS
        )
        self._delivery_recovery_timer.timeout.connect(self._reconcile_stale_deliveries)
        self._delivery_recovery_timer.start()

        self._maintenance_timer = QTimer(self)
        self._maintenance_timer.setInterval(self.MAINTENANCE_CHECK_INTERVAL_MS)
        self._maintenance_timer.timeout.connect(self._run_daily_maintenance)
        self._maintenance_timer.start()

        QTimer.singleShot(750, self._campaign_tick)
        QTimer.singleShot(1_500, self._run_daily_maintenance)

    @Slot()
    def _reconcile_stale_deliveries(self) -> None:
        if self._shutdown_requested:
            return
        try:
            result = self.database.recover_stale_deliveries()
            recovered = int(cast(int, result.get("total", 0)) or 0)
            if recovered <= 0:
                return
            message = (
                "Восстановлены зависшие доставки после аварийного завершения: "
                f"{recovered}. Статус изменён на uncertain; автоматический повтор "
                "отключён. Проверьте результат вручную."
            )
            log.warning("%s Details: %s", message, result)
            account_counts = result.get("accounts")
            if not isinstance(account_counts, dict):
                account_counts = {}
            for raw_account_id, raw_counts in account_counts.items():
                try:
                    account_id = max(0, int(raw_account_id or 0))
                except (TypeError, ValueError, OverflowError):
                    account_id = 0
                counts = raw_counts if isinstance(raw_counts, dict) else {}
                account_total = max(0, int(counts.get("total", 0) or 0))
                if account_total <= 0:
                    continue
                account_message = (
                    "Восстановлены зависшие доставки этого аккаунта после "
                    f"аварийного завершения: {account_total}. Статус изменён на "
                    "uncertain; автоматический повтор отключён. Проверьте результат "
                    "вручную."
                )
                try:
                    self.database.insert_log(
                        "WARNING", account_message, account_id=account_id
                    )
                except Exception:
                    log.exception(
                        "Could not persist stale-delivery recovery notice for account %s",
                        account_id,
                    )
        except Exception:
            log.exception("Stale delivery reconciliation failed")

    def _daily_maintenance_background(self) -> dict[str, object]:
        result = self.database.run_daily_maintenance()
        wal_size = self.database.log_wal_size_if_large()
        return {"retention": result, "wal_size": wal_size}

    @Slot()
    def _run_daily_maintenance(self) -> None:
        if self._shutdown_requested or self._maintenance_job is not None:
            return
        job = BackgroundCall(
            self._daily_maintenance_background,
            cleanup=self.database.close_thread_connection,
        )
        self._maintenance_job = job
        job.signals.succeeded.connect(self._daily_maintenance_succeeded)
        job.signals.failed.connect(self._daily_maintenance_failed)
        job.signals.finished.connect(self._daily_maintenance_finished)
        QThreadPool.globalInstance().start(job)

    @Slot(object)
    def _daily_maintenance_succeeded(self, payload: object) -> None:
        if not isinstance(payload, dict):
            log.error("Daily SQLite retention returned an invalid payload")
            return
        result = payload.get("retention")
        wal_size = max(0, int(payload.get("wal_size") or 0))
        if isinstance(result, dict) and any(
            int(value or 0) for value in result.values()
        ):
            log.info("Daily SQLite retention completed: %s", result)
        log.debug("SQLite WAL size after maintenance: %s bytes", wal_size)

    @Slot(str)
    def _daily_maintenance_failed(self, message: str) -> None:
        log.error("Daily SQLite retention failed: %s", message)

    @Slot()
    def _daily_maintenance_finished(self) -> None:
        self._maintenance_job = None

    @staticmethod
    def _migrate_legacy_secrets(
        database,
        secret_store,
        keys,
        secret_lock,
        migration_required: threading.Event | None = None,
    ) -> None:
        """Move every legacy SQLite secret copy into protected storage."""
        unresolved = False
        try:
            migrate_legacy_account_secrets(
                database,
                secret_store,
                keys=keys,
                secret_lock=secret_lock,
            )
        except Exception:
            unresolved = True
            log.exception("Could not complete protected account-secret migration")
        finally:
            if migration_required is not None:
                if unresolved:
                    migration_required.set()
                else:
                    migration_required.clear()
            close_connection = getattr(database, "close_thread_connection", None)
            if callable(close_connection):
                try:
                    close_connection()
                except Exception:
                    log.exception("Could not close secret-migration SQLite connection")

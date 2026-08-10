from __future__ import annotations
import secrets
from PySide6.QtCore import QTime, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QInputDialog,
    QLayout,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)
from core.activity_schedule import (
    QUIET_END_KEY,
    QUIET_START_KEY,
    SCHEDULE_ENABLED_KEY,
    TIMEZONE_KEY,
    validate_timezone_name,
)
from core.version import APP_NAME
from gui.auth_worker import TelegramAuthWorker
from gui.account_manager_panel import AccountManagerPanel
from gui.background import BackgroundCall, connect_lifecycle_safe
from gui.views.account_health_card import AccountHealthCard
from services.proxy_validation import normalize_proxy_config

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from gui.views.account_view import AccountView

class AccountViewAccountOpsMixin:
    def _load_account_catalog(self) -> None:
        self._account_catalog_generation += 1
        generation = self._account_catalog_generation

        def applied(accounts) -> None:
            if generation != self._account_catalog_generation:
                return
            selected = self.adapter.get_selected_account_id()
            previous = self.adapter.get_previous_selected_account_id()
            self._durable_selected_account_id = int(selected or 0)
            self.account_manager.reload(
                list(accounts or []),
                selected_account_id=selected,
                previous_account_id=previous,
            )

        def failed(message: str) -> None:
            if generation == self._account_catalog_generation:
                self.account_label.setText(message)

        self._run_background(
            lambda: self.adapter.list_telegram_accounts(),
            on_success=applied,
            on_error=failed,
        )
    def _select_account(self, account_id: int) -> None:
        account_id = int(account_id or 0)
        if account_id <= 0:
            return
        if self._account_blocking_jobs:
            self.account_manager.cancel_pending_selection()
            QMessageBox.warning(
                self,
                APP_NAME,
                "Дождитесь завершения операции с Telegram-аккаунтом",
            )
            return
        self.account_selection_busy.emit(True)
        self._account_selection_generation += 1
        generation = self._account_selection_generation
        self._pending_account_selection = (account_id, generation)
        # Any settings/catalog callback already in flight belongs to the
        # account that was visible before this latest intent.
        self._settings_load_generation += 1
        self._account_catalog_generation += 1
        self.status_label.setText("Переключение аккаунта…")
        if not self._account_selection_in_flight:
            self._start_pending_account_selection()
    def _start_pending_account_selection(self) -> None:
        request = self._pending_account_selection
        if request is None or self._account_selection_in_flight:
            return
        self._pending_account_selection = None
        account_id, generation = request
        self._account_selection_in_flight = True

        def selected(_result) -> None:
            # The database selection transaction has committed even when a
            # newer GUI intent makes this callback stale. Remember the real
            # durable owner before deciding whether to repaint this result.
            self._durable_selected_account_id = account_id
            if generation != self._account_selection_generation:
                self._finish_account_selection()
                return
            self._adding_account = False
            self._pending_session_name = ""
            self.account_manager.set_selected_account_id(account_id)
            self.account_changed.emit()
            self.load_settings(on_finished=self._finish_account_selection)

        def failed(message: str) -> None:
            if generation != self._account_selection_generation:
                self._finish_account_selection()
                return
            # A previous queued selection may already have committed. Render
            # that durable owner before re-enabling any account-bound UI.
            durable = int(
                self._durable_selected_account_id
                or self.account_manager._selected_account_id
                or 0
            )
            self.account_manager.set_selected_account_id(durable)
            self.account_changed.emit()
            QMessageBox.warning(self, "Аккаунт", message)
            self.load_settings(on_finished=self._finish_account_selection)

        try:
            self._run_background(
                lambda: self.adapter.select_telegram_account(account_id),
                on_success=selected,
                on_error=failed,
            )
        except BaseException:
            self._account_selection_in_flight = False
            if generation == self._account_selection_generation:
                self.account_manager.cancel_pending_selection()
            self._finish_account_selection()
            raise
    def _finish_account_selection(self) -> None:
        self._account_selection_in_flight = False
        if self._pending_account_selection is not None:
            self._start_pending_account_selection()
            return
        self.account_selection_busy.emit(False)
    def _begin_add_account(self) -> None:
        state = self.adapter.can_add_telegram_account()
        if not state.get("allowed"):
            QMessageBox.warning(
                self, "Лимит аккаунтов", state.get("message") or ""
            )
            return
        if self.auth_worker is not None and self.auth_worker.isRunning():
            QMessageBox.warning(self, APP_NAME, "Авторизация уже выполняется")
            return
        self._adding_account = True
        self._reauthorizing_account_id = 0
        self._auth_settings_snapshot = {}
        self._pending_session_name = f"pending_{secrets.token_hex(16)}"
        self.api_id.clear()
        self.api_hash.clear()
        self.phone.clear()
        self.proxy_enabled.setChecked(False)
        self.proxy_host.clear()
        self.proxy_port.clear()
        self.proxy_login.clear()
        self.proxy_password.clear()
        self._set_code_card_visible(False)
        self.status_label.setText("Добавление Telegram-аккаунта")
        self.account_label.setText(
            "Заполните API ID, API Hash, телефон и отдельный proxy при необходимости"
        )
        self.connect_button.setText("Отправить код Telegram")
        self._set_auth_identity_fields_enabled(True)
        self.api_id.setFocus()
        self._refresh_dynamic_layout(self.api_id)
    def _stop_account(self, account_id: int) -> None:
        answer = QMessageBox.question(
            self,
            "Остановить всю работу аккаунта?",
            "Будут принудительно завершены все кампании, вступления, связки и "
            "запланированные отправки этого аккаунта. Другие аккаунты продолжат "
            "работу. Telegram-сессия останется сохранённой.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.status_label.setText("Остановка работы аккаунта…")

        def stopped(result) -> None:
            QMessageBox.information(
                self,
                "Работа остановлена",
                str(result.get("message") or "Работа аккаунта остановлена."),
            )
            self._load_account_catalog()
            self.account_changed.emit()

        self._run_background(
            lambda: self.adapter.stop_telegram_account(account_id),
            on_success=stopped,
            on_error=lambda message: QMessageBox.warning(
                self, "Остановка аккаунта", message
            ),
        )
    def _resume_account(self, account_id: int) -> None:
        def resumed(_result) -> None:
            self._load_account_catalog()
            self.account_changed.emit()

        self._run_background(
            lambda: self.adapter.resume_telegram_account(account_id),
            on_success=resumed,
            on_error=lambda message: QMessageBox.warning(
                self, "Аккаунт", message
            ),
        )
    def _disconnect_account(self, account_id: int) -> None:
        owner = int(account_id or 0)
        if owner <= 0:
            return
        if self.auth_worker is not None and self.auth_worker.isRunning():
            QMessageBox.warning(self, APP_NAME, "Авторизация уже выполняется")
            return
        if self._account_blocking_jobs:
            QMessageBox.warning(
                self,
                APP_NAME,
                "Дождитесь завершения операции с Telegram-аккаунтом",
            )
            return
        answer = QMessageBox.question(
            self,
            "Выйти из Telegram-аккаунта?",
            "Работа выбранного аккаунта будет остановлена, а его локальная "
            "Telegram-сессия удалена. Каналы, комментарии, история и настройки "
            "останутся. После выхода можно изменить proxy и войти заново.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.status_label.setText("Выход из Telegram-аккаунта…")

        def disconnected(result) -> None:
            self._adding_account = True
            self._reauthorizing_account_id = owner
            self._pending_session_name = f"pending_{secrets.token_hex(16)}"
            self._auth_settings_snapshot = {}
            self._set_code_card_visible(False)
            self.load_settings()
            self.account_changed.emit()
            QMessageBox.information(
                self,
                "Выход выполнен",
                str(result.get("message") or "Локальная Telegram-сессия удалена."),
            )

        self._run_background(
            lambda: self.adapter.disconnect_telegram_account(owner),
            on_success=disconnected,
            on_error=lambda message: QMessageBox.warning(
                self, "Выход из аккаунта", message
            ),
            blocks_account_change=True,
        )
    def _delete_account(self, account_id: int) -> None:
        owner = int(account_id or 0)
        if owner <= 0:
            return
        if self.auth_worker is not None and self.auth_worker.isRunning():
            QMessageBox.warning(self, APP_NAME, "Авторизация уже выполняется")
            return
        if self._account_blocking_jobs:
            QMessageBox.warning(
                self,
                APP_NAME,
                "Дождитесь завершения операции с Telegram-аккаунтом",
            )
            return
        answer = QMessageBox.question(
            self,
            "Удалить Telegram-аккаунт?",
            "Будут остановлены задачи этого аккаунта и безвозвратно удалены его "
            "локальная session, proxy/API-секреты, каналы, кампании, расписания, "
            "история и журналы. Другие аккаунты не изменятся.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.status_label.setText("Удаление Telegram-аккаунта…")

        def deleted(result) -> None:
            self._adding_account = False
            self._reauthorizing_account_id = 0
            self._pending_session_name = ""
            self._auth_settings_snapshot = {}
            self._set_code_card_visible(False)
            self.load_settings()
            self.account_changed.emit()
            QMessageBox.information(
                self,
                "Аккаунт удалён",
                str(result.get("message") or "Локальные данные аккаунта удалены."),
            )

        self._run_background(
            lambda: self.adapter.delete_telegram_account(owner),
            on_success=deleted,
            on_error=lambda message: QMessageBox.warning(
                self, "Удаление аккаунта", message
            ),
            blocks_account_change=True,
        )
    def _capture_previous_account_transfer_ids(
        self,
    ) -> tuple[int, int] | None:
        if (
            self._account_selection_in_flight
            or self._pending_account_selection is not None
            or self._account_blocking_jobs
        ):
            QMessageBox.warning(
                self,
                APP_NAME,
                "Дождитесь завершения переключения или другой операции "
                "с Telegram-аккаунтом",
            )
            return None
        source = int(self.adapter.get_previous_selected_account_id() or 0)
        target = int(self.adapter.get_selected_account_id() or 0)
        if source <= 0 or target <= 0 or source == target:
            QMessageBox.warning(
                self,
                APP_NAME,
                "Сначала переключитесь с другого подключённого аккаунта.",
            )
            return None
        return source, target
    def _import_comments_from_previous(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Импорт комментариев")
        box.setText(
            "Комментарии будут скопированы из непосредственно предыдущего "
            "выбранного аккаунта. Источник не изменится."
        )
        replace_button = box.addButton(
            "Заменить текущие комментарии",
            QMessageBox.ButtonRole.AcceptRole,
        )
        fill_button = box.addButton(
            "Добавить только в свободные позиции",
            QMessageBox.ButtonRole.ActionRole,
        )
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is replace_button:
            mode = "replace"
        elif clicked is fill_button:
            mode = "fill"
        else:
            return

        transfer = self._capture_previous_account_transfer_ids()
        if transfer is None:
            return
        source_account_id, target_account_id = transfer

        self._run_background(
            lambda: self.adapter.import_comments_from_previous_account(
                mode=mode,
                source_account_id=source_account_id,
                target_account_id=target_account_id,
            ),
            on_success=lambda result: QMessageBox.information(
                self,
                "Импорт завершён",
                f"Изменено позиций: {int(result.get('imported') or 0)}",
            ),
            on_error=lambda message: QMessageBox.warning(
                self, "Импорт комментариев", message
            ),
            blocks_account_change=True,
        )
    def _import_channels_from_previous(self) -> None:
        transfer = self._capture_previous_account_transfer_ids()
        if transfer is None:
            return
        source_account_id, target_account_id = transfer

        self._run_background(
            lambda: self.adapter.import_channels_from_previous_account(
                source_account_id=source_account_id,
                target_account_id=target_account_id,
            ),
            on_success=lambda result: QMessageBox.information(
                self,
                "Импорт каналов завершён",
                "Импортировано: {imported}\nУже существовали: {existing}\n"
                "Пропущено: {skipped}".format(**result),
            ),
            on_error=lambda message: QMessageBox.warning(
                self, "Импорт каналов", message
            ),
            blocks_account_change=True,
        )
    def _check_selected_account(self) -> None:
        account_id = int(self.adapter.get_selected_account_id() or 0)
        if account_id <= 0:
            QMessageBox.warning(self, "Telegram", "Сначала выберите аккаунт")
            return
        self.status_label.setText("Проверка подключения…")

        def checked(_result) -> None:
            self.status_label.setText("Аккаунт подключён")
            self._load_account_catalog()

        self._run_background(
            lambda: self.adapter.check_telegram_account_runtime(account_id),
            on_success=checked,
            on_error=lambda message: QMessageBox.warning(
                self, "Telegram", message
            ),
        )
    def _set_account_controls_busy(self, busy: bool) -> None:
        enabled = not busy
        self.connect_button.setEnabled(enabled)
        self.confirm_button.setEnabled(enabled)
        self.logout_button.setEnabled(enabled)
        self.account_manager.setEnabled(enabled)
        self._set_auth_identity_fields_enabled(enabled)
        # Factory reset is independent from account/campaign ownership. It may
        # be requested while authorization or a local save is finishing; the
        # application controller will stop/wait for those jobs before deletion.
        safe_enabled = enabled and not self._factory_reset_pending
        self.reset_database_button.setEnabled(safe_enabled)
        self.schedule_enabled.setEnabled(safe_enabled)
        schedule_fields_enabled = safe_enabled and self.schedule_enabled.isChecked()
        self.timezone_name.setEnabled(schedule_fields_enabled)
        self.quiet_start.setEnabled(schedule_fields_enabled)
        self.quiet_end.setEnabled(schedule_fields_enabled)
        self.save_schedule_button.setEnabled(safe_enabled)
    def _restore_account_controls_if_idle(self) -> None:
        worker_running = bool(
            self.auth_worker is not None and self.auth_worker.isRunning()
        )
        busy = worker_running or bool(self._account_blocking_jobs)
        self._set_account_controls_busy(busy)
        if not busy:
            self.adapter.set_auth_in_progress(False)
            if self.phone_code_hash:
                self._set_auth_identity_fields_enabled(False)
                self.connect_button.setEnabled(False)
                self.logout_button.setEnabled(False)
                self.confirm_button.setEnabled(True)
            else:
                authorized = str(
                    self._cached_account_values.get("telegram.authorized") or "0"
                ).strip().lower() in {"1", "true", "yes", "on"}
                if authorized and not self._adding_account:
                    self._set_auth_identity_fields_enabled(False)
                else:
                    self._set_auth_identity_fields_enabled(True)
    def _save_account_state(self, values, *, on_success) -> None:
        """Serialize durable account identity with the Telethon session state."""

        self._account_state_generation += 1
        generation = self._account_state_generation

        def apply_if_current(_result) -> None:
            if generation == self._account_state_generation:
                on_success(values)

        def fail_if_current(message: str) -> None:
            if generation == self._account_state_generation:
                self._settings_save_failed(message)

        self._run_background(
            lambda: self.adapter.save_settings(values),
            on_success=apply_if_current,
            on_error=fail_if_current,
            blocks_account_change=True,
        )
    def _automation_owns_account(self) -> bool:
        """Return True while a persistent campaign still belongs to this account."""
        try:
            comment = self.adapter.get_comment_campaign_state()
            joining = self.adapter.get_join_campaign_state()
        except Exception:
            # Failing closed is safer than switching the Telethon session while
            # the scheduler state cannot be verified.
            return True
        active = {"running", "paused", "network_wait", "cycle_wait"}
        return bool(
            (comment and str(comment.get("status") or "") in active)
            or (joining and str(joining.get("status") or "") in active)
        )
    def _ensure_account_change_allowed(self) -> bool:
        if self.adapter.is_queue_running():
            QMessageBox.warning(
                self, APP_NAME, "Сначала дождитесь завершения текущей операции"
            )
            return False
        if self._automation_owns_account():
            QMessageBox.warning(
                self,
                APP_NAME,
                "Сначала остановите активную кампанию комментирования или вступлений. "
                "Расписание привязано к текущему Telegram-аккаунту.",
            )
            return False
        if self.auth_worker is not None and self.auth_worker.isRunning():
            QMessageBox.warning(self, APP_NAME, "Авторизация уже выполняется")
            return False
        if self._account_blocking_jobs:
            QMessageBox.warning(
                self,
                APP_NAME,
                "Дождитесь завершения сохранения состояния Telegram-аккаунта",
            )
            return False
        return True
    def logout_account(self):
        """Backward-compatible action used by older UI bindings."""
        owner = int(self.adapter.get_selected_account_id() or 0)
        if owner > 0:
            self._disconnect_account(owner)
    def _apply_disconnected_account(self, _values=None) -> None:
        self._adding_account = False
        self._reauthorizing_account_id = 0
        self._pending_session_name = ""
        self._cached_account_values = {}
        self._set_status_dot(False)
        self.status_label.setText("Аккаунт отключён")
        self.account_label.setText("Введите данные нового Telegram-аккаунта")
        self.connect_button.setText("Подключить аккаунт")
        self._set_auth_identity_fields_enabled(True)
        self._set_code_card_visible(False)
        self.account_changed.emit()

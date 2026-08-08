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

class AccountViewAuthFlowMixin:
    def _reauthorize_account(self, account_id: int) -> None:
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
                "Дождитесь завершения сохранения состояния Telegram-аккаунта",
            )
            return
        if owner != int(self.adapter.get_selected_account_id() or 0):
            QMessageBox.warning(
                self,
                "Переподключение",
                "Сначала дождитесь завершения переключения на выбранный аккаунт.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Переподключить Telegram-аккаунт?",
            "Работа только этого аккаунта будет остановлена. После ввода кода "
            "новая подтверждённая session заменит старую. Остальные аккаунты "
            "продолжат работу.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.status_label.setText("Подготовка переподключения…")

        def stopped(_result) -> None:
            self._adding_account = True
            self._reauthorizing_account_id = owner
            self._auth_settings_snapshot = {}
            self._pending_session_name = f"pending_{secrets.token_hex(16)}"
            self._set_code_card_visible(False)
            self.status_label.setText("Переподключение Telegram-аккаунта")
            self.account_label.setText(
                "Проверьте API, телефон и proxy, затем запросите новый код Telegram"
            )
            self.connect_button.setText("Отправить код Telegram")
            self._set_auth_identity_fields_enabled(True)
            self.api_id.setFocus()
            self._refresh_dynamic_layout(self.api_id)

        self._run_background(
            lambda: self.adapter.stop_telegram_account(owner),
            on_success=stopped,
            on_error=lambda message: QMessageBox.warning(
                self, "Переподключение", message
            ),
            blocks_account_change=True,
        )
    def _set_auth_identity_fields_enabled(self, enabled: bool) -> None:
        active = bool(enabled)
        for widget in (
            self.api_id,
            self.api_hash,
            self.phone,
            self.proxy_enabled,
            self.proxy_type,
            self.proxy_host,
            self.proxy_port,
            self.proxy_login,
            self.proxy_password,
        ):
            widget.setEnabled(active)
        self.proxy_details_button.setEnabled(
            active and self.proxy_enabled.isChecked()
        )
    def _show_pending_code_request(self) -> None:
        """Show the challenge immediately instead of waiting on the network."""

        self._set_auth_identity_fields_enabled(False)
        self.code.setEnabled(False)
        self.two_fa.setEnabled(False)
        self.confirm_button.setEnabled(False)
        self.code.setPlaceholderText("Ожидаем отправку кода Telegram…")
        self._set_code_card_visible(True, focus_widget=self.code)
    def _activate_code_entry(self) -> None:
        self._set_auth_identity_fields_enabled(False)
        self.code.setEnabled(True)
        self.two_fa.setEnabled(True)
        self.code.setPlaceholderText("Код из сообщения Telegram")
        self.confirm_button.setEnabled(True)
        self._refresh_dynamic_layout(self.code)
    def request_code(self):
        if not self._adding_account and self.adapter.get_selected_account_id():
            self._check_selected_account()
            return
        if self.auth_worker is not None and self.auth_worker.isRunning():
            QMessageBox.warning(self, APP_NAME, "Авторизация уже выполняется")
            return
        if not self._pending_session_name:
            state = self.adapter.can_add_telegram_account()
            if not state.get("allowed"):
                QMessageBox.warning(
                    self, "Лимит аккаунтов", state.get("message") or ""
                )
                return
            self._adding_account = True
            self._reauthorizing_account_id = 0
            self._pending_session_name = f"pending_{secrets.token_hex(16)}"
        try:
            settings = self._settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Проверьте данные", str(exc))
            return
        self._auth_settings_snapshot = dict(settings)
        self.connect_button.setEnabled(False)
        self._show_pending_code_request()
        self.status_label.setText("Запрос кода Telegram…")
        self._start_worker("request_code", settings)
    def confirm_login(self):
        if self.auth_worker is not None and self.auth_worker.isRunning():
            QMessageBox.warning(self, APP_NAME, "Авторизация уже выполняется")
            return
        settings = dict(self._auth_settings_snapshot)
        if not settings:
            QMessageBox.warning(
                self,
                "Авторизация",
                "Запросите новый код Telegram перед подтверждением входа.",
            )
            return
        self._start_worker(
            "sign_in",
            settings,
            code=self.code.text(),
            phone_code_hash=self.phone_code_hash,
            password=self.two_fa.text(),
        )
    def _start_worker(self, mode, settings, **kwargs):
        self._active_auth_mode = str(mode)
        self.adapter.set_auth_in_progress(True)
        self._set_account_controls_busy(True)
        self.status_label.setText("Подключение к Telegram…")
        self.account_label.setText(
            "Авторизация нового аккаунта не останавливает остальные кампании"
        )
        self._launch_auth_worker(mode, settings, **kwargs)
    def _launch_auth_worker(self, mode, settings, **kwargs):
        self.connect_button.setEnabled(False)
        self.confirm_button.setEnabled(False)
        self.status_label.setText("Подключение к Telegram…")
        self.account_label.setText("Не закрывайте программу во время авторизации")
        self.auth_worker = TelegramAuthWorker(
            mode=mode,
            settings=settings,
            session_dir=self.config.telegram.session_dir,
            database_path=self.config.database_path,
            session_name=self._pending_session_name or "main",
            persist_state=False,
            parent=self,
            **kwargs,
        )
        self.auth_worker.authorized.connect(self._authorized)
        self.auth_worker.code_sent.connect(self._code_sent)
        self.auth_worker.password_required.connect(self._password_required)
        self.auth_worker.failed.connect(self._failed)
        self.auth_worker.temporary_failed.connect(self._temporary_failed)
        self.auth_worker.finished.connect(self._worker_finished)
        self.auth_worker.start()
    def _code_sent(self, phone_code_hash: str):
        self.phone_code_hash = phone_code_hash
        self._set_code_card_visible(True, focus_widget=self.code)
        self._activate_code_entry()
        self.status_label.setText("Код отправлен")
        self.account_label.setText("Введите код, который пришёл в Telegram")
        self.code.setFocus()
    def _clear_temporary_auth_fields(self) -> None:
        """Remove one-time Telegram credentials after success or terminal failure."""

        self.code.clear()
        self.two_fa.clear()
        self.phone_code_hash = ""
        worker = self.auth_worker
        if worker is not None:
            worker.code = ""
            worker.password = ""
            worker.phone_code_hash = ""
    def _authorized(self, account: dict):
        self._clear_temporary_auth_fields()
        if not account.get("id"):
            self._failed("Telegram не вернул идентификатор аккаунта")
            return
        settings = dict(self._auth_settings_snapshot)
        if not settings:
            self._failed("Параметры авторизации потеряны. Запросите новый код Telegram")
            return
        pending_name = str(
            account.get("_session_name") or self._pending_session_name or ""
        )

        def registered(values) -> None:
            self._adding_account = False
            self._reauthorizing_account_id = 0
            self._pending_session_name = ""
            self._auth_settings_snapshot = {}
            compatibility = {
                "telegram.account_id": values.get("telegram_account_id"),
                "telegram.account_name": values.get("display_name"),
                "telegram.account_username": values.get("username") or "",
                "telegram.authorized": "1",
            }
            self._cached_account_values = dict(compatibility)
            self._set_authorized_ui(compatibility)
            self._set_code_card_visible(False)
            if self._onboarding_only:
                self.onboarding_completed.emit()
            else:
                self.load_settings()
            self.account_changed.emit()

        self.status_label.setText("Сохранение изолированного аккаунта…")

        def persist_and_register():
            # Persist the authorization snapshot and returned identity as one
            # blocking operation. Account actions must remain locked until the
            # selected account id is durable, even if final session catalog
            # registration subsequently reports an error.
            durable_settings = dict(settings)
            durable_settings.update(
                {
                    "telegram.account_id": str(int(account["id"])),
                    "telegram.account_name": str(account.get("name") or "Telegram Account"),
                    "telegram.account_username": str(account.get("username") or ""),
                    "telegram.authorized": "1",
                }
            )
            self.adapter.save_settings(durable_settings)
            return self.adapter.register_authorized_account(
                account,
                settings,
                pending_session_name=pending_name,
            )

        self._run_background(
            persist_and_register,
            on_success=registered,
            on_error=self._failed,
            blocks_account_change=True,
        )
    def _apply_authorized_account(self, values) -> None:
        self._cached_account_values = dict(values)
        self._set_authorized_ui(values)
        self._set_code_card_visible(False)
        self.account_changed.emit()
    def _set_authorization_required_ui(self, values) -> None:
        name = values.get("telegram.account_name") or "Telegram Account"
        username = values.get("telegram.account_username") or ""
        owner = int(values.get("telegram.account_id") or 0)
        self._adding_account = True
        self._reauthorizing_account_id = owner
        if not self._pending_session_name:
            self._pending_session_name = f"pending_{secrets.token_hex(16)}"
        self._auth_settings_snapshot = {}
        self._set_status_dot(False)
        self.status_label.setText("Требуется вход в Telegram")
        identity = f"{name}" + (f"  ·  @{username}" if username else "")
        self.account_label.setText(
            identity + "  ·  измените proxy при необходимости и запросите новый код"
        )
        self.connect_button.setText("Отправить код Telegram")
        self._set_auth_identity_fields_enabled(True)
        self._set_code_card_visible(False)
        self._load_account_catalog()
    def _set_authorized_ui(self, values):
        name = values.get("telegram.account_name") or "Telegram Account"
        username = values.get("telegram.account_username") or ""
        self._adding_account = False
        self._reauthorizing_account_id = 0
        self._pending_session_name = ""
        self._set_status_dot(True)
        self.status_label.setText("Аккаунт подключён")
        self.account_label.setText(
            f"{name}" + (f"  ·  @{username}" if username else "")
        )
        self.connect_button.setText("Проверить подключение")
        self._set_auth_identity_fields_enabled(False)
        self._load_account_catalog()
    def _temporary_failed(self, message: str) -> None:
        """Keep a cached authorized session usable after a transient check failure."""
        if self._cached_account_values.get("telegram.account_name"):
            self._set_authorized_ui(self._cached_account_values)
            name = str(
                self._cached_account_values.get("telegram.account_name")
                or "Telegram Account"
            )
            username = str(
                self._cached_account_values.get("telegram.account_username") or ""
            )
            identity = name + (f"  ·  @{username}" if username else "")
            self.status_label.setText("Аккаунт подключён")
            self.account_label.setText(
                identity + "  ·  Telegram временно не ответил; сессия сохранена"
            )
            return
        self._failed(message)
    def is_authentication_running(self) -> bool:
        worker = self.auth_worker
        return bool(worker is not None and worker.isRunning())
    def request_auth_stop(self) -> bool:
        """Cooperatively cancel authorization before Qt destroys this view."""
        worker = self.auth_worker
        if worker is None or not worker.isRunning():
            return False
        worker.request_stop()
        return True
    def _worker_finished(self):
        worker = self.sender()
        if worker is self.auth_worker:
            self.auth_worker = None
            self._active_auth_mode = ""
        self._restore_account_controls_if_idle()
        if worker is not None:
            worker.deleteLater()

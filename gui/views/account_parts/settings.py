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

class AccountViewSettingsMixin:
    def _update_proxy_status_card(self, *_args) -> None:
        if not self.proxy_enabled.isChecked():
            self.proxy_status_value.setText("Прокси: не подключён")
            return
        proxy_type = str(self.proxy_type.currentText() or "SOCKS5").upper()
        host = self.proxy_host.text().strip()
        port = self.proxy_port.text().strip()
        login = self.proxy_login.text().strip()
        if not host or not port:
            self.proxy_status_value.setText(
                f"Прокси: {proxy_type} включён · заполните адрес и порт"
            )
            return
        value = f"Прокси: {proxy_type} · {host}:{port}"
        if login:
            value += f" · логин {login}"
        self.proxy_status_value.setText(value)
    def _toggle_proxy(self, enabled: bool):
        active = bool(enabled)
        self.proxy_enabled.setText(
            "Прокси подключён" if active else "Подключить прокси"
        )
        self.proxy_details_button.setEnabled(active)
        if active:
            self.proxy_details_button.setChecked(True)
        else:
            self.proxy_details_button.setChecked(False)
        self._toggle_proxy_details(
            self.proxy_details_button.isChecked()
        )
        self._sync_proxy_type_fields(self.proxy_type.currentText())
        self._update_proxy_status_card()
        self._refresh_dynamic_layout()
    def _toggle_proxy_details(self, expanded: bool) -> None:
        visible = bool(expanded and self.proxy_enabled.isChecked())
        self.proxy_box.setVisible(visible)
        self.proxy_details_button.setText(
            "Настройки прокси ▾" if visible else "Настройки прокси ▸"
        )
        self._refresh_dynamic_layout()
    def _toggle_schedule(self, enabled: bool) -> None:
        active = bool(enabled)
        self.schedule_enabled.setText(
            "Режим тишины · включён"
            if active
            else "Режим тишины · выключен"
        )
        self.timezone_name.setEnabled(active)
        self.quiet_start.setEnabled(active)
        self.quiet_end.setEnabled(active)
        if hasattr(self, "root_layout"):
            self._refresh_dynamic_layout()
    def _sync_proxy_type_fields(self, _proxy_type: str) -> None:
        for widget in (
            self.proxy_login_label,
            self.proxy_login,
            self.proxy_password_label,
            self.proxy_password,
        ):
            widget.setVisible(True)
        self.proxy_port.setPlaceholderText("1080")
        if self.proxy_enabled.isChecked():
            self._refresh_dynamic_layout()
    def set_factory_reset_pending(self, pending: bool) -> None:
        self._factory_reset_pending = bool(pending)
        if self._factory_reset_pending:
            self.reset_database_button.setEnabled(False)
        else:
            self._restore_account_controls_if_idle()
    def _run_background(
        self,
        callback,
        *,
        on_success=None,
        on_error=None,
        blocks_account_change: bool = False,
    ):
        job = BackgroundCall(
            callback,
            cleanup=self.adapter.close_thread_connection,
        )
        self._background_jobs.add(job)
        if blocks_account_change:
            self._account_blocking_jobs.add(job)
            self.adapter.set_auth_in_progress(True)
            self._set_account_controls_busy(True)

        adapter = self.adapter

        def release(view: AccountView) -> None:
            view._background_jobs.discard(job)
            view._account_blocking_jobs.discard(job)
            view._restore_account_controls_if_idle()

        def orphaned_release() -> None:
            if blocks_account_change:
                adapter.set_auth_in_progress(False)

        def succeeded(_view: AccountView, result) -> None:
            if on_success is not None:
                on_success(result)

        def failed(_view: AccountView, message: str) -> None:
            if on_error is not None:
                on_error(message)

        connect_lifecycle_safe(
            job,
            self,
            succeeded=succeeded if on_success is not None else None,
            failed=failed if on_error is not None else None,
            finished=release,
            orphaned_finished=orphaned_release,
        )
        QThreadPool.globalInstance().start(job)
        return job
    def load_settings(self):
        self._settings_load_generation += 1
        generation = self._settings_load_generation
        self._load_account_catalog()
        self.connect_button.setEnabled(False)

        def applied(values) -> None:
            if generation == self._settings_load_generation:
                self._apply_settings(values)

        def failed(message: str) -> None:
            if generation == self._settings_load_generation:
                self._settings_load_failed(message)

        self._run_background(
            lambda: self.adapter.get_settings(),
            on_success=applied,
            on_error=failed,
        )
    def _settings_load_failed(self, message: str) -> None:
        self.connect_button.setEnabled(True)
        self.status_label.setText("Не удалось загрузить настройки")
        self.account_label.setText(message)
    def _apply_settings(self, values):
        values = values or {}
        self.api_id.setText(str(values.get("telegram.api_id") or ""))
        self.api_hash.setText(str(values.get("telegram.api_hash") or ""))
        self.phone.setText(str(values.get("telegram.phone") or ""))
        enabled = str(values.get("telegram.proxy_enabled") or "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        proxy_type = str(values.get("telegram.proxy_type") or "SOCKS5").upper()
        if proxy_type not in {"SOCKS5", "SOCKS4", "HTTP"}:
            enabled = False
            proxy_type = "SOCKS5"
        self.proxy_enabled.setChecked(enabled)
        self._toggle_proxy(enabled)
        index = self.proxy_type.findText(proxy_type)
        self.proxy_type.setCurrentIndex(max(0, index))
        self.proxy_host.setText(str(values.get("telegram.proxy_host") or ""))
        self.proxy_port.setText(str(values.get("telegram.proxy_port") or ""))
        self.proxy_login.setText(str(values.get("telegram.proxy_username") or ""))
        self.proxy_password.setText(str(values.get("telegram.proxy_password") or ""))
        self._sync_proxy_type_fields(self.proxy_type.currentText())
        self._update_proxy_status_card()
        self._applying_settings = True
        try:
            schedule_enabled = str(
                values.get(SCHEDULE_ENABLED_KEY) or "0"
            ).strip().lower() in {"1", "true", "yes", "on"}
            self.schedule_enabled.setChecked(schedule_enabled)
            self.timezone_name.setText(str(values.get(TIMEZONE_KEY) or "UTC"))
            start = QTime.fromString(
                str(values.get(QUIET_START_KEY) or "22:00"), "HH:mm"
            )
            end = QTime.fromString(
                str(values.get(QUIET_END_KEY) or "07:00"), "HH:mm"
            )
            self.quiet_start.setTime(start if start.isValid() else QTime(22, 0))
            self.quiet_end.setTime(end if end.isValid() else QTime(7, 0))
            self._toggle_schedule(schedule_enabled)
        finally:
            self._applying_settings = False
        account_name = values.get("telegram.account_name")
        authorized = str(values.get("telegram.authorized") or "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if account_name and authorized:
            self._cached_account_values = dict(values)
            self._set_authorized_ui(values)
        elif account_name:
            self._cached_account_values = dict(values)
            self._set_authorization_required_ui(values)
        else:
            self._adding_account = False
            self._reauthorizing_account_id = 0
            self._pending_session_name = ""
            self._cached_account_values = {}
            self._set_status_dot(False)
            self.status_label.setText("Аккаунт не подключён")
            self.account_label.setText("Введите данные нового Telegram-аккаунта")
            self.connect_button.setText("Подключить аккаунт")
            self._set_auth_identity_fields_enabled(True)
            self._set_code_card_visible(False)
        self.connect_button.setEnabled(True)
    def _schedule_settings(self) -> dict[str, str]:
        timezone_name = validate_timezone_name(self.timezone_name.text())
        return {
            SCHEDULE_ENABLED_KEY: "1" if self.schedule_enabled.isChecked() else "0",
            TIMEZONE_KEY: timezone_name,
            QUIET_START_KEY: self.quiet_start.time().toString("HH:mm"),
            QUIET_END_KEY: self.quiet_end.time().toString("HH:mm"),
        }
    def save_activity_schedule(self) -> None:
        try:
            values = self._schedule_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Проверьте расписание", str(exc))
            return

        self.save_schedule_button.setEnabled(False)

        def saved(_result) -> None:
            self.save_schedule_button.setEnabled(True)
            QMessageBox.information(
                self,
                "Расписание сохранено",
                "Локальные тихие часы будут применяться к автоматическим "
                "комментариям до создания delivery reservation.",
            )

        def failed(message: str) -> None:
            self.save_schedule_button.setEnabled(True)
            QMessageBox.warning(
                self,
                "Расписание не сохранено",
                message,
            )

        self._run_background(
            lambda: self.adapter.save_settings(values),
            on_success=saved,
            on_error=failed,
        )
    def _settings(self, *, require_phone: bool = True):
        try:
            api_id = int(self.api_id.text().strip())
        except ValueError as exc:
            raise ValueError("API ID должен состоять из цифр") from exc
        api_hash = self.api_hash.text().strip()
        phone = self.phone.text().strip()
        if api_id <= 0 or not api_hash:
            raise ValueError("Заполните API ID и API Hash")
        if require_phone and not phone:
            raise ValueError("Заполните номер телефона")
        proxy = None
        if self.proxy_enabled.isChecked():
            proxy = normalize_proxy_config(
                self.proxy_type.currentText(),
                self.proxy_host.text(),
                self.proxy_port.text(),
                self.proxy_login.text(),
                self.proxy_password.text(),
            )
        values = {
            "telegram.api_id": api_id,
            "telegram.api_hash": api_hash,
            "telegram.phone": phone,
            "telegram.proxy_enabled": "1" if self.proxy_enabled.isChecked() else "0",
            "telegram.proxy_type": proxy.proxy_type
            if proxy
            else self.proxy_type.currentText(),
            "telegram.proxy_host": proxy.host
            if proxy
            else self.proxy_host.text().strip(),
            "telegram.proxy_port": str(proxy.port)
            if proxy
            else self.proxy_port.text().strip(),
            "telegram.proxy_username": proxy.username
            if proxy
            else self.proxy_login.text().strip(),
            "telegram.proxy_password": proxy.password
            if proxy
            else self.proxy_password.text(),
        }
        values.update(self._schedule_settings())
        return values
    def reset_database(self) -> None:
        first = QMessageBox.question(
            self,
            "Полный сброс LansetSpBot",
            "Будут безвозвратно удалены все локальные данные программы, включая "
            "историю отправок и ограничение повторной обработки группы за 24 часа. "
            "Telegram-аккаунт потребуется подключить заново. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if first != QMessageBox.StandardButton.Yes:
            return

        confirmation, accepted = QInputDialog.getText(
            self,
            "Подтверждение заводского сброса",
            "Для подтверждения введите слово СБРОСИТЬ:",
        )
        if not accepted:
            return
        if confirmation.strip().upper() != "СБРОСИТЬ":
            QMessageBox.warning(
                self,
                "Сброс отменён",
                "Контрольное слово введено неверно. Данные не изменены.",
            )
            return

        self.set_factory_reset_pending(True)
        QMessageBox.information(
            self,
            "Заводской сброс",
            "Программа автоматически остановит кампании, безопасно завершит "
            "фоновые процессы, удалит все локальные данные и закроется. "
            "Следующий запуск будет полностью чистым.",
        )
        self.factory_reset_requested.emit()
    def _settings_save_failed(self, message: str) -> None:
        if not self.phone_code_hash:
            self._set_code_card_visible(False)
        self.status_label.setText("Не удалось сохранить настройки")
        self.account_label.setText(message)
        QMessageBox.warning(self, "Настройки", message)
    def _password_required(self):
        self._set_code_card_visible(True, focus_widget=self.two_fa)
        self._activate_code_entry()
        self.status_label.setText("Нужен пароль 2FA")
        self.account_label.setText("Введите пароль двухэтапной аутентификации")
        self.two_fa.setFocus()
    def _set_status_dot(self, online: bool) -> None:
        self.status_dot.setObjectName(
            "statusDotOnline" if online else "statusDotOffline"
        )
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)
    def _failed(self, message: str):
        failed_mode = self._active_auth_mode
        had_code_request = bool(self.phone_code_hash)
        self._clear_temporary_auth_fields()
        self._auth_settings_snapshot = {}
        if failed_mode == "request_code" and not had_code_request:
            self._set_code_card_visible(False)
        elif self.code_card.isVisible():
            self._activate_code_entry()
        # Backward-safe fallback in case an older worker emits the transient error
        # through the generic signal. A cached session must not be invalidated or
        # accompanied by a blocking false alarm.
        if (
            self._cached_account_values.get("telegram.account_name")
            and "Request was unsuccessful" in message
        ):
            self._temporary_failed(message)
            return
        self.status_label.setText("Не удалось подключиться")
        self.account_label.setText(message)
        QMessageBox.warning(self, "Telegram", message)

from __future__ import annotations


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
from gui.background import BackgroundCall, connect_lifecycle_safe
from services.proxy_validation import normalize_proxy_config


class AccountView(QWidget):
    account_changed = Signal()
    factory_reset_requested = Signal()

    def __init__(self, adapter, config):
        super().__init__()
        self.adapter = adapter
        self.config = config
        self.auth_worker = None
        self.phone_code_hash = ""
        self._cached_account_values: dict[str, object] = {}
        self._active_auth_mode = ""
        self._background_jobs: set[BackgroundCall] = set()
        self._account_blocking_jobs: set[BackgroundCall] = set()
        self._account_state_generation = 0
        self._factory_reset_pending = False
        self._applying_settings = False
        self._dynamic_layout_focus_widget: QWidget | None = None
        self._dynamic_layout_queued_timer = QTimer(self)
        self._dynamic_layout_queued_timer.setSingleShot(True)
        self._dynamic_layout_queued_timer.timeout.connect(
            self._run_queued_dynamic_layout
        )
        self._dynamic_layout_settle_timer = QTimer(self)
        self._dynamic_layout_settle_timer.setSingleShot(True)
        self._dynamic_layout_settle_timer.timeout.connect(
            self._run_queued_dynamic_layout
        )

        title = QLabel("Аккаунт Telegram")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "Подключите аккаунт один раз — сессия сохранится локально на этом компьютере."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)

        self.status_card = QFrame()
        self.status_card.setObjectName("statusCard")
        status_layout = QHBoxLayout(self.status_card)
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDotOffline")
        self.status_label = QLabel("Аккаунт не подключён")
        self.status_label.setObjectName("statusTitle")
        self.account_label = QLabel("Введите данные Telegram API")
        self.account_label.setObjectName("mutedText")
        status_text = QVBoxLayout()
        status_text.addWidget(self.status_label)
        status_text.addWidget(self.account_label)
        status_layout.addWidget(self.status_dot)
        status_layout.addLayout(status_text)
        status_layout.addStretch(1)

        form_card = QFrame()
        self.form_card = form_card
        form_card.setObjectName("card")
        form_layout = QFormLayout(form_card)
        self.form_layout = form_layout
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setHorizontalSpacing(24)
        form_layout.setVerticalSpacing(14)

        self.api_id = QLineEdit()
        self.api_id.setPlaceholderText("Например: 12345678")
        self.api_hash = QLineEdit()
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_hash.setPlaceholderText("API Hash с my.telegram.org")
        self.phone = QLineEdit()
        self.phone.setPlaceholderText("+79990000000")
        form_layout.addRow("API ID", self.api_id)
        form_layout.addRow("API Hash", self.api_hash)
        form_layout.addRow("Телефон", self.phone)

        self.proxy_enabled = QCheckBox("Использовать proxy")
        self.proxy_enabled.toggled.connect(self._toggle_proxy)
        form_layout.addRow("", self.proxy_enabled)

        self.proxy_box = QFrame()
        proxy_grid = QGridLayout(self.proxy_box)
        self.proxy_grid = proxy_grid
        proxy_grid.setContentsMargins(0, 0, 0, 0)
        self.proxy_type = QComboBox()
        self.proxy_type.addItems(["SOCKS5", "SOCKS4", "HTTP", "MTPROXY"])
        self.proxy_type.currentTextChanged.connect(self._sync_proxy_type_fields)
        self.proxy_host = QLineEdit()
        self.proxy_host.setPlaceholderText("127.0.0.1")
        self.proxy_port = QLineEdit()
        self.proxy_port.setPlaceholderText("1080")
        self.proxy_login = QLineEdit()
        self.proxy_login.setPlaceholderText("Необязательно")
        self.proxy_password = QLineEdit()
        self.proxy_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.proxy_password.setPlaceholderText("Необязательно")
        self.proxy_secret = QLineEdit()
        self.proxy_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.proxy_secret.setPlaceholderText("Secret из настроек MTProxy")
        self.proxy_secret.setToolTip(
            "Вставьте Secret из Telegram: обычный, DD или Fake TLS EE. "
            "Поддерживаются hex и Base64URL (например, ключ, начинающийся с 7). "
            "Secret хранится локально и скрывается в логах."
        )
        proxy_grid.addWidget(QLabel("Тип"), 0, 0)
        proxy_grid.addWidget(self.proxy_type, 0, 1)
        proxy_grid.addWidget(QLabel("Адрес"), 1, 0)
        proxy_grid.addWidget(self.proxy_host, 1, 1)
        proxy_grid.addWidget(QLabel("Порт"), 1, 2)
        proxy_grid.addWidget(self.proxy_port, 1, 3)
        self.proxy_login_label = QLabel("Логин")
        self.proxy_password_label = QLabel("Пароль")
        self.proxy_secret_label = QLabel("Secret")
        proxy_grid.addWidget(self.proxy_login_label, 2, 0)
        proxy_grid.addWidget(self.proxy_login, 2, 1)
        proxy_grid.addWidget(self.proxy_password_label, 2, 2)
        proxy_grid.addWidget(self.proxy_password, 2, 3)
        proxy_grid.addWidget(self.proxy_secret_label, 2, 0)
        proxy_grid.addWidget(self.proxy_secret, 2, 1, 1, 3)
        self._sync_proxy_type_fields(self.proxy_type.currentText())
        form_layout.addRow("", self.proxy_box)

        self.schedule_enabled = QCheckBox(
            "Не отправлять автоматические комментарии в тихие часы"
        )
        self.schedule_enabled.setToolTip(
            "Детерминированное локальное расписание для контроля времени работы. "
            "Оно не заменяет Telegram FloodWait и ограничения аккаунта."
        )
        self.schedule_enabled.toggled.connect(self._toggle_schedule)
        form_layout.addRow("", self.schedule_enabled)

        self.schedule_box = QFrame()
        schedule_grid = QGridLayout(self.schedule_box)
        schedule_grid.setContentsMargins(0, 0, 0, 0)
        self.timezone_name = QLineEdit()
        self.timezone_name.setPlaceholderText("Europe/Berlin")
        self.timezone_name.setToolTip(
            "Имя часового пояса IANA, например Europe/Berlin или Asia/Jakarta"
        )
        self.quiet_start = QTimeEdit(QTime(22, 0))
        self.quiet_start.setDisplayFormat("HH:mm")
        self.quiet_end = QTimeEdit(QTime(7, 0))
        self.quiet_end.setDisplayFormat("HH:mm")
        schedule_grid.addWidget(QLabel("Часовой пояс"), 0, 0)
        schedule_grid.addWidget(self.timezone_name, 0, 1, 1, 3)
        schedule_grid.addWidget(QLabel("Начало тишины"), 1, 0)
        schedule_grid.addWidget(self.quiet_start, 1, 1)
        schedule_grid.addWidget(QLabel("Окончание"), 1, 2)
        schedule_grid.addWidget(self.quiet_end, 1, 3)
        self.save_schedule_button = QPushButton("Сохранить расписание")
        self.save_schedule_button.setObjectName("secondaryButton")
        self.save_schedule_button.clicked.connect(self.save_activity_schedule)
        schedule_grid.addWidget(self.save_schedule_button, 2, 0, 1, 4)
        form_layout.addRow("", self.schedule_box)
        self.schedule_box.show()
        self._toggle_schedule(False)

        self.connect_button = QPushButton("Подключить аккаунт")
        self.connect_button.setObjectName("primaryButton")
        self.connect_button.clicked.connect(self.request_code)
        button_row = QHBoxLayout()
        button_row.addWidget(self.connect_button)
        self.logout_button = QPushButton("Сменить аккаунт")
        self.logout_button.setObjectName("secondaryButton")
        self.logout_button.clicked.connect(self.logout_account)
        button_row.addWidget(self.logout_button)
        form_layout.addRow("", button_row)

        # Keep the Telegram challenge inside the main account form.  A separate
        # sibling card could be painted underneath the factory-reset card by
        # QScrollArea while the toolkit was still processing the asynchronous
        # show event.  As a spanning QFormLayout row, the parent card must grow
        # before the following reset card can be positioned.
        self.code_card = QFrame(form_card)
        self.code_card.setObjectName("authChallengeCard")
        self.code_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        code_layout = QFormLayout(self.code_card)
        self.code_layout = code_layout
        code_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        code_layout.setHorizontalSpacing(24)
        code_layout.setVerticalSpacing(14)
        self.code = QLineEdit()
        self.code.setPlaceholderText("Код из сообщения Telegram")
        self.two_fa = QLineEdit()
        self.two_fa.setEchoMode(QLineEdit.EchoMode.Password)
        self.two_fa.setPlaceholderText("Только если включена двухэтапная защита")
        self.confirm_button = QPushButton("Подтвердить вход")
        self.confirm_button.setObjectName("primaryButton")
        self.confirm_button.clicked.connect(self.confirm_login)
        code_layout.addRow("Код", self.code)
        code_layout.addRow("Пароль 2FA", self.two_fa)
        code_layout.addRow("", self.confirm_button)
        self.code_card.hide()
        form_layout.addRow(self.code_card)

        self.reset_card = QFrame()
        self.reset_card.setObjectName("dangerCard")
        reset_layout = QVBoxLayout(self.reset_card)
        reset_layout.setContentsMargins(20, 18, 20, 18)
        reset_layout.setSpacing(10)
        reset_title = QLabel("Заводской сброс")
        reset_title.setObjectName("dangerTitle")
        reset_text = QLabel(
            "Удаляет только локальные файлы профиля LansetSpBot: базу, настройки, "
            "каналы, связки, кампании, историю отправок, 24-часовые ограничения, "
            "Telegram-сессию и локальный файл секретов. "
            "Отменить действие нельзя."
        )
        reset_text.setObjectName("dangerText")
        reset_text.setWordWrap(True)
        self.reset_database_button = QPushButton("Сбросить базу данных")
        self.reset_database_button.setObjectName("dangerButton")
        self.reset_database_button.clicked.connect(self.reset_database)
        reset_layout.addWidget(reset_title)
        reset_layout.addWidget(reset_text)
        reset_layout.addWidget(
            self.reset_database_button, 0, Qt.AlignmentFlag.AlignLeft
        )

        layout = QVBoxLayout(self)
        self.root_layout = layout
        # The page lives inside a resizable QScrollArea.  SetMinimumSize lets the
        # viewport become smaller than the content and scroll, without imposing
        # a stale maximum size after dynamic authorization controls are shown.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(34, 28, 34, 28)
        layout.setSpacing(18)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(self.status_card)
        layout.addWidget(form_card)
        layout.addWidget(self.reset_card)
        layout.addStretch(1)
        self.load_settings()

    def _containing_scroll_area(self) -> QScrollArea | None:
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                return parent
            parent = parent.parentWidget()
        return None

    def _sync_dynamic_layout(self, focus_widget: QWidget | None = None) -> None:
        """Reflow dynamic account controls and keep the challenge reachable."""

        for candidate in (self.code_layout, self.form_layout, self.root_layout):
            candidate.invalidate()
            candidate.activate()

        # QScrollArea(widgetResizable=True) may keep its child at the old viewport
        # height after a widget is shown from a queued signal.  Publish an
        # explicit content minimum based on the now-active layouts.  This makes
        # overlap geometrically impossible: the reset card is a later sibling and
        # the scroll area must allocate the form's complete size hint first.
        required_height = self.root_layout.sizeHint().height()
        scroll = self._containing_scroll_area()
        if scroll is not None:
            required_height = max(required_height, scroll.viewport().height())
        self.setMinimumHeight(required_height)
        self.form_card.updateGeometry()
        self.code_card.updateGeometry()
        self.reset_card.updateGeometry()
        self.updateGeometry()

        if scroll is not None:
            scroll_widget = scroll.widget()
            if scroll_widget is not None:
                scroll_widget.updateGeometry()
            if focus_widget is not None and focus_widget.isVisible():
                scroll.ensureWidgetVisible(focus_widget, 36, 36)

    def _run_queued_dynamic_layout(self) -> None:
        """Run only while this AccountView and its child timers still exist."""
        try:
            self._sync_dynamic_layout(self._dynamic_layout_focus_widget)
        except RuntimeError as exc:
            # A nested widget can be deleted by an account-view rebuild between
            # event turns. Child QTimers are destroyed with this view; this guard
            # handles only that narrow nested-object race.
            if "already deleted" not in str(exc).lower():
                raise

    def _refresh_dynamic_layout(self, focus_widget: QWidget | None = None) -> None:
        # Qt can defer QFormLayout geometry through more than one event turn.
        # Owned single-shot timers are automatically cancelled when this view is
        # destroyed, instead of context-free delayed callbacks.
        self._dynamic_layout_focus_widget = focus_widget
        self._sync_dynamic_layout(focus_widget)
        self._dynamic_layout_queued_timer.start(0)
        self._dynamic_layout_settle_timer.start(60)

    def _set_code_card_visible(
        self, visible: bool, *, focus_widget: QWidget | None = None
    ) -> None:
        self.code_card.setVisible(visible)
        if not visible:
            self.setMinimumHeight(0)
        self._refresh_dynamic_layout(focus_widget if visible else None)

    def set_compact_mode(self, compact: bool) -> None:
        self.form_layout.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapAllRows
            if compact
            else QFormLayout.RowWrapPolicy.DontWrapRows
        )
        root_layout = self.layout()
        if root_layout is not None:
            root_layout.setContentsMargins(
                20 if compact else 34,
                22 if compact else 28,
                20 if compact else 34,
                22 if compact else 28,
            )
        self._refresh_dynamic_layout()

    def _toggle_proxy(self, enabled: bool):
        self.proxy_box.setVisible(enabled)
        self._sync_proxy_type_fields(self.proxy_type.currentText())
        self._refresh_dynamic_layout()

    def _toggle_schedule(self, enabled: bool) -> None:
        active = bool(enabled)
        self.timezone_name.setEnabled(active)
        self.quiet_start.setEnabled(active)
        self.quiet_end.setEnabled(active)
        if hasattr(self, "root_layout"):
            self._refresh_dynamic_layout()

    def _sync_proxy_type_fields(self, proxy_type: str) -> None:
        is_mtproxy = str(proxy_type or "").strip().upper() in {
            "MTPROXY",
            "MTPROTO",
        }
        for widget in (
            self.proxy_login_label,
            self.proxy_login,
            self.proxy_password_label,
            self.proxy_password,
        ):
            widget.setVisible(not is_mtproxy)
        self.proxy_secret_label.setVisible(is_mtproxy)
        self.proxy_secret.setVisible(is_mtproxy)
        self.proxy_port.setPlaceholderText("443" if is_mtproxy else "1080")
        if self.proxy_enabled.isChecked():
            self._refresh_dynamic_layout()

    def _set_account_controls_busy(self, busy: bool) -> None:
        enabled = not busy
        self.connect_button.setEnabled(enabled)
        self.confirm_button.setEnabled(enabled)
        self.logout_button.setEnabled(enabled)
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

    def set_factory_reset_pending(self, pending: bool) -> None:
        self._factory_reset_pending = bool(pending)
        if self._factory_reset_pending:
            self.reset_database_button.setEnabled(False)
        else:
            self._restore_account_controls_if_idle()

    def _restore_account_controls_if_idle(self) -> None:
        worker_running = bool(
            self.auth_worker is not None and self.auth_worker.isRunning()
        )
        busy = worker_running or bool(self._account_blocking_jobs)
        self._set_account_controls_busy(busy)
        if not busy:
            self.adapter.set_auth_in_progress(False)

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

    def load_settings(self):
        self.connect_button.setEnabled(False)
        self._run_background(
            lambda: self.adapter.get_settings(),
            on_success=self._apply_settings,
            on_error=self._settings_load_failed,
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
        self.proxy_enabled.setChecked(enabled)
        self._toggle_proxy(enabled)
        proxy_type = str(values.get("telegram.proxy_type") or "SOCKS5")
        index = self.proxy_type.findText(proxy_type)
        self.proxy_type.setCurrentIndex(max(0, index))
        self.proxy_host.setText(str(values.get("telegram.proxy_host") or ""))
        self.proxy_port.setText(str(values.get("telegram.proxy_port") or ""))
        self.proxy_login.setText(str(values.get("telegram.proxy_username") or ""))
        self.proxy_password.setText(str(values.get("telegram.proxy_password") or ""))
        self.proxy_secret.setText(str(values.get("telegram.proxy_secret") or ""))
        self._sync_proxy_type_fields(self.proxy_type.currentText())
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
        if values.get("telegram.account_name"):
            self._cached_account_values = dict(values)
            self._set_authorized_ui(values)
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
                self.proxy_secret.text(),
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
            "telegram.proxy_secret": proxy.secret
            if proxy
            else self.proxy_secret.text(),
        }
        values.update(self._schedule_settings())
        return values

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

    def logout_account(self):
        if not self._ensure_account_change_allowed():
            return
        try:
            settings = self._settings(require_phone=False)
        except ValueError as exc:
            QMessageBox.warning(self, "Проверьте данные", str(exc))
            return
        answer = QMessageBox.question(
            self,
            "Сменить аккаунт",
            "Текущая Telegram-сессия будет завершена. Сохранённый список каналов и групп останется в программе. Продолжить?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_worker("logout", settings)

    def _show_pending_code_request(self) -> None:
        """Show the challenge immediately instead of waiting on the network."""

        self.code.setEnabled(False)
        self.two_fa.setEnabled(False)
        self.confirm_button.setEnabled(False)
        self.code.setPlaceholderText("Ожидаем отправку кода Telegram…")
        self._set_code_card_visible(True, focus_widget=self.code)

    def _activate_code_entry(self) -> None:
        self.code.setEnabled(True)
        self.two_fa.setEnabled(True)
        self.code.setPlaceholderText("Код из сообщения Telegram")
        self.confirm_button.setEnabled(True)
        self._refresh_dynamic_layout(self.code)

    def request_code(self):
        if not self._ensure_account_change_allowed():
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Проверьте данные", str(exc))
            return
        self.connect_button.setEnabled(False)
        self._show_pending_code_request()
        self.status_label.setText("Сохранение защищённых настроек…")
        self._run_background(
            lambda: self.adapter.save_settings(settings),
            on_success=lambda _result: self._start_worker("request_code", settings),
            on_error=self._settings_save_failed,
            blocks_account_change=True,
        )

    def confirm_login(self):
        if not self._ensure_account_change_allowed():
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Проверьте данные", str(exc))
            return
        self._start_worker(
            "sign_in",
            settings,
            code=self.code.text(),
            phone_code_hash=self.phone_code_hash,
            password=self.two_fa.text(),
        )

    def _settings_save_failed(self, message: str) -> None:
        if not self.phone_code_hash:
            self._set_code_card_visible(False)
        self.status_label.setText("Не удалось сохранить настройки")
        self.account_label.setText(message)
        QMessageBox.warning(self, "Настройки", message)

    def _start_worker(self, mode, settings, **kwargs):
        self._active_auth_mode = str(mode)
        self.adapter.set_auth_in_progress(True)
        self._set_account_controls_busy(True)
        self.status_label.setText("Завершение фонового подключения…")
        self.account_label.setText(
            "LansetSpBot безопасно освобождает Telegram-сессию перед авторизацией"
        )

        def launch(prepared) -> None:
            if not prepared:
                self._failed(
                    "Не удалось освободить Telegram-сессию. "
                    "Дождитесь завершения текущей операции и повторите попытку"
                )
                return
            self._launch_auth_worker(mode, settings, **kwargs)

        self._run_background(
            lambda: self.adapter.prepare_account_change(),
            on_success=launch,
            on_error=self._failed,
            blocks_account_change=True,
        )

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

    def _password_required(self):
        self._set_code_card_visible(True, focus_widget=self.two_fa)
        self._activate_code_entry()
        self.status_label.setText("Нужен пароль 2FA")
        self.account_label.setText("Введите пароль двухэтапной аутентификации")
        self.two_fa.setFocus()

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
        persisted = bool(account.get("_persisted"))
        if not account.get("id"):
            values = {
                "telegram.account_id": "",
                "telegram.account_name": "",
                "telegram.account_username": "",
                "telegram.authorized": "0",
            }
            if persisted:
                self._apply_disconnected_account(values)
            else:
                self._save_account_state(
                    values, on_success=self._apply_disconnected_account
                )
            return
        values = {
            "telegram.account_id": account.get("id") or "",
            "telegram.account_name": account.get("name") or "Telegram Account",
            "telegram.account_username": account.get("username") or "",
            "telegram.authorized": "1",
        }
        if persisted:
            self._apply_authorized_account(values)
        else:
            self._save_account_state(values, on_success=self._apply_authorized_account)

    def _set_status_dot(self, online: bool) -> None:
        self.status_dot.setObjectName(
            "statusDotOnline" if online else "statusDotOffline"
        )
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)

    def _apply_disconnected_account(self, _values=None) -> None:
        self._cached_account_values = {}
        self._set_status_dot(False)
        self.status_label.setText("Аккаунт отключён")
        self.account_label.setText("Введите данные нового Telegram-аккаунта")
        self.connect_button.setText("Подключить аккаунт")
        self._set_code_card_visible(False)
        self.account_changed.emit()

    def _apply_authorized_account(self, values) -> None:
        self._cached_account_values = dict(values)
        self._set_authorized_ui(values)
        self._set_code_card_visible(False)
        self.account_changed.emit()

    def _set_authorized_ui(self, values):
        name = values.get("telegram.account_name") or "Telegram Account"
        username = values.get("telegram.account_username") or ""
        self._set_status_dot(True)
        self.status_label.setText("Аккаунт подключён")
        self.account_label.setText(
            f"{name}" + (f"  ·  @{username}" if username else "")
        )
        self.connect_button.setText("Проверить подключение")

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

    def _failed(self, message: str):
        failed_mode = self._active_auth_mode
        had_code_request = bool(self.phone_code_hash)
        self._clear_temporary_auth_fields()
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

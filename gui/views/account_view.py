# OBSERVABILITY-PACKAGE-V3
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


class AccountView(QWidget):
    account_changed = Signal()
    onboarding_completed = Signal()
    factory_reset_requested = Signal()

    def __init__(self, adapter, config, *, onboarding_only: bool = False):
        super().__init__()
        self.adapter = adapter
        self.config = config
        self._onboarding_only = bool(onboarding_only)
        self.auth_worker = None
        self.phone_code_hash = ""
        self._cached_account_values: dict = {}
        self._adding_account = False
        self._pending_session_name = ""
        self._reauthorizing_account_id = 0
        self._auth_settings_snapshot: dict = {}
        self._account_catalog_generation = 0
        self._active_auth_mode = ""
        self._background_jobs: set[BackgroundCall] = set()
        self._account_blocking_jobs: set[BackgroundCall] = set()
        self._account_state_generation = 0
        self._account_selection_generation = 0
        self._account_selection_in_flight = False
        self._pending_account_selection: tuple[int, int] | None = None
        self._settings_load_generation = 0
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

        self.theme_selector = QComboBox()
        self.theme_selector.setObjectName("themeSelector")
        self.theme_selector.setToolTip("Выбрать оформление программы")
        self.theme_selector.setMinimumContentsLength(18)
        self.theme_selector.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )

        title_column = QVBoxLayout()
        title_column.setSpacing(2)
        title_column.addWidget(title)
        title_column.addWidget(subtitle)
        header_layout = QHBoxLayout()
        header_layout.setSpacing(16)
        header_layout.addLayout(title_column, 1)
        header_layout.addWidget(
            self.theme_selector, 0, Qt.AlignmentFlag.AlignTop
        )

        self.status_card = QFrame()
        self.status_card.setObjectName("statusCard")
        status_layout = QHBoxLayout(self.status_card)
        self.status_layout = status_layout
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDotOffline")
        self.status_label = QLabel("Аккаунт не подключён")
        self.status_label.setObjectName("statusTitle")
        self.account_label = QLabel("Введите данные Telegram API")
        self.account_label.setObjectName("mutedText")
        self.account_label.setMinimumWidth(530)
        status_text = QVBoxLayout()
        status_text.addWidget(self.status_label)
        status_text.addWidget(self.account_label)
        status_layout.addWidget(self.status_dot)
        status_layout.addLayout(status_text)
        status_layout.addStretch(1)

        self.proxy_status_card = QFrame()
        self.proxy_status_card.setObjectName("infoCard")
        proxy_status_layout = QHBoxLayout(self.proxy_status_card)
        proxy_status_layout.setContentsMargins(18, 14, 18, 14)
        proxy_status_layout.setSpacing(12)
        proxy_status_title = QLabel("Прокси текущего аккаунта")
        proxy_status_title.setObjectName("cardTitle")
        self.proxy_status_value = QLabel("Прокси: не подключён")
        self.proxy_status_value.setObjectName("mutedText")
        self.proxy_status_value.setWordWrap(True)
        proxy_status_layout.addWidget(proxy_status_title)
        proxy_status_layout.addWidget(self.proxy_status_value, 1)

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

        self.proxy_enabled = QCheckBox("Подключить прокси")
        self.proxy_enabled.setObjectName("proxyToggle")
        self.proxy_enabled.toggled.connect(self._toggle_proxy)
        form_layout.addRow("", self.proxy_enabled)

        self.proxy_details_button = QPushButton("Настройки прокси ▸")
        self.proxy_details_button.setObjectName("secondaryButton")
        self.proxy_details_button.setCheckable(True)
        self.proxy_details_button.setEnabled(False)
        self.proxy_details_button.toggled.connect(self._toggle_proxy_details)
        form_layout.addRow("", self.proxy_details_button)

        self.proxy_box = QFrame()
        self.proxy_box.setObjectName("proxyCard")
        proxy_grid = QGridLayout(self.proxy_box)
        self.proxy_grid = proxy_grid
        proxy_grid.setContentsMargins(0, 0, 0, 0)
        self.proxy_type = QComboBox()
        self.proxy_type.addItems(["SOCKS5", "SOCKS4", "HTTP"])
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
        self.proxy_type.currentTextChanged.connect(self._update_proxy_status_card)
        self.proxy_host.textChanged.connect(self._update_proxy_status_card)
        self.proxy_port.textChanged.connect(self._update_proxy_status_card)
        self.proxy_login.textChanged.connect(self._update_proxy_status_card)
        proxy_grid.addWidget(QLabel("Тип"), 0, 0)
        proxy_grid.addWidget(self.proxy_type, 0, 1)
        proxy_grid.addWidget(QLabel("Адрес"), 1, 0)
        proxy_grid.addWidget(self.proxy_host, 1, 1)
        proxy_grid.addWidget(QLabel("Порт"), 1, 2)
        proxy_grid.addWidget(self.proxy_port, 1, 3)
        self.proxy_login_label = QLabel("Логин")
        self.proxy_password_label = QLabel("Пароль")
        proxy_grid.addWidget(self.proxy_login_label, 2, 0)
        proxy_grid.addWidget(self.proxy_login, 2, 1)
        proxy_grid.addWidget(self.proxy_password_label, 2, 2)
        proxy_grid.addWidget(self.proxy_password, 2, 3)
        self._sync_proxy_type_fields(self.proxy_type.currentText())
        form_layout.addRow("", self.proxy_box)
        self.proxy_box.hide()

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
        self.logout_button = QPushButton("Добавить аккаунт")
        self.logout_button.setObjectName("secondaryButton")
        self.logout_button.clicked.connect(self._begin_add_account)
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
        layout.addLayout(header_layout)
        layout.addWidget(self.status_card)
        layout.addWidget(self.proxy_status_card)

        self.account_manager = AccountManagerPanel()
        self.account_manager.account_selected.connect(self._select_account)
        self.account_manager.add_requested.connect(self._begin_add_account)
        self.account_manager.stop_requested.connect(self._stop_account)
        self.account_manager.resume_requested.connect(self._resume_account)
        self.account_manager.reauthorize_requested.connect(
            self._reauthorize_account
        )
        self.account_manager.delete_requested.connect(self._delete_account)
        self.account_manager.import_comments_requested.connect(
            self._import_comments_from_previous
        )
        self.account_manager.import_channels_requested.connect(
            self._import_channels_from_previous
        )
        # Keep one real delete control. Reparent the already wired button from
        # the selector row into the top status card requested by the operator.
        self.account_manager.selector_row.removeWidget(
            self.account_manager.delete_button
        )
        self.status_layout.addWidget(
            self.account_manager.delete_button,
            0,
            Qt.AlignmentFlag.AlignTop,
        )
        layout.addWidget(self.account_manager)
        self.account_health_card = AccountHealthCard(self.adapter, self)
        self.account_changed.connect(self.account_health_card.refresh)
        layout.addWidget(self.account_health_card)
        layout.addWidget(form_card)
        layout.addWidget(self.reset_card)
        layout.addStretch(1)

        if self._onboarding_only:
            self.theme_selector.hide()
            self.proxy_status_card.hide()
            self.account_manager.hide()
            self.account_health_card.hide()
            self.reset_card.hide()
            self.schedule_enabled.hide()
            self.schedule_box.hide()
            self.logout_button.hide()
            self.root_layout.setContentsMargins(0, 0, 0, 0)
            self.root_layout.setSpacing(12)
            self.status_label.setText("Добавление аккаунта для прогрева")
            self.account_label.setText(
                "Откройте блок и заполните Telegram API, телефон и proxy"
            )
        else:
            self.load_settings()

    def begin_onboarding(self) -> None:
        """Prepare the shared account form for one new Telegram account."""
        if not self._onboarding_only:
            return
        worker = self.auth_worker
        if worker is not None and worker.isRunning():
            QMessageBox.warning(self, APP_NAME, "Авторизация уже выполняется")
            return
        if not self._adding_account:
            self._begin_add_account()

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

    def _load_account_catalog(self) -> None:
        self._account_catalog_generation += 1
        generation = self._account_catalog_generation

        def applied(accounts) -> None:
            if generation != self._account_catalog_generation:
                return
            selected = self.adapter.get_selected_account_id()
            previous = self.adapter.get_previous_selected_account_id()
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
            try:
                if generation != self._account_selection_generation:
                    return
                self._adding_account = False
                self._pending_session_name = ""
                self.account_manager.set_selected_account_id(account_id)
                self.load_settings()
                self.account_changed.emit()
            finally:
                self._finish_account_selection()

        def failed(message: str) -> None:
            try:
                if generation != self._account_selection_generation:
                    return
                # Restore the selector and fields from the durable account that
                # remained selected after the failed request.
                self.load_settings()
                QMessageBox.warning(self, "Аккаунт", message)
            finally:
                self._finish_account_selection()

        try:
            self._run_background(
                lambda: self.adapter.select_telegram_account(account_id),
                on_success=selected,
                on_error=failed,
            )
        except BaseException:
            self._account_selection_in_flight = False
            raise

    def _finish_account_selection(self) -> None:
        self._account_selection_in_flight = False
        if self._pending_account_selection is not None:
            self._start_pending_account_selection()

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
        self.api_id.setFocus()

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
            self.api_id.setFocus()

        self._run_background(
            lambda: self.adapter.stop_telegram_account(owner),
            on_success=stopped,
            on_error=lambda message: QMessageBox.warning(
                self, "Переподключение", message
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
        self._run_background(
            lambda: self.adapter.import_comments_from_previous_account(
                mode=mode
            ),
            on_success=lambda result: QMessageBox.information(
                self,
                "Импорт завершён",
                f"Изменено позиций: {int(result.get('imported') or 0)}",
            ),
            on_error=lambda message: QMessageBox.warning(
                self, "Импорт комментариев", message
            ),
        )

    def _import_channels_from_previous(self) -> None:
        self._run_background(
            lambda: self.adapter.import_channels_from_previous_account(),
            on_success=lambda result: QMessageBox.information(
                self,
                "Импорт каналов завершён",
                "Импортировано: {imported}\nУже существовали: {existing}\n"
                "Пропущено: {skipped}".format(**result),
            ),
            on_error=lambda message: QMessageBox.warning(
                self, "Импорт каналов", message
            ),
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

    def _set_auth_identity_fields_enabled(self, enabled: bool) -> None:
        for widget in (
            self.api_id,
            self.api_hash,
            self.phone,
            self.proxy_enabled,
            self.proxy_details_button,
            self.proxy_type,
            self.proxy_host,
            self.proxy_port,
            self.proxy_login,
            self.proxy_password,
        ):
            widget.setEnabled(bool(enabled))

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
            if self.phone_code_hash:
                self._set_auth_identity_fields_enabled(False)
                self.connect_button.setEnabled(False)
                self.logout_button.setEnabled(False)
                self.confirm_button.setEnabled(True)

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
        if values.get("telegram.account_name"):
            self._cached_account_values = dict(values)
            self._set_authorized_ui(values)
        else:
            self._cached_account_values = {}
            self._set_status_dot(False)
            self.status_label.setText("Аккаунт не подключён")
            self.account_label.setText("Введите данные нового Telegram-аккаунта")
            self.connect_button.setText("Подключить аккаунт")
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

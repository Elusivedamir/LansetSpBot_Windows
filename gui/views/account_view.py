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


from gui.views.account_parts.auth_flow import AccountViewAuthFlowMixin
from gui.views.account_parts.account_ops import AccountViewAccountOpsMixin
from gui.views.account_parts.settings import AccountViewSettingsMixin

class AccountView(AccountViewAuthFlowMixin, AccountViewAccountOpsMixin, AccountViewSettingsMixin, QWidget):
    account_changed = Signal()
    account_selection_busy = Signal(bool)
    onboarding_completed = Signal()
    factory_reset_requested = Signal()

    def __init__(self, adapter, config, *, onboarding_only: bool = False):
        super().__init__()
        self.adapter = adapter
        self.config = config
        self._onboarding_only = bool(onboarding_only)
        self.auth_worker: TelegramAuthWorker | None = None
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
        self._durable_selected_account_id = 0
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
        self.account_manager.disconnect_requested.connect(
            self._disconnect_account
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

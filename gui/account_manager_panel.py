from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStyle,
    QVBoxLayout,
)

from core.account_limits import (
    MAX_REGISTERED_TELEGRAM_ACCOUNTS,
    account_limit_message,
)


STATE_LABELS = {
    "disconnected": "Отключён",
    "connecting": "Подключается",
    "connected": "Подключён",
    "running": "Работает",
    "paused": "Пауза",
    "stopping": "Останавливается",
    "stopped": "Остановлен",
    "network_wait": "Нет сети",
    "flood_wait": "FloodWait",
    "restricted": "Ограничен",
    "authorization_required": "Нужна авторизация",
    "error": "Ошибка",
}


class AccountManagerPanel(QFrame):
    account_selected = Signal(int)
    add_requested = Signal()
    stop_requested = Signal(int)
    resume_requested = Signal(int)
    reauthorize_requested = Signal(int)
    delete_requested = Signal(int)
    import_comments_requested = Signal()
    import_channels_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("accountManagerCard")
        self._accounts: dict[int, dict] = {}
        self._selected_account_id = 0
        self._previous_account_id = 0

        title = QLabel("Выберите аккаунт")
        title.setObjectName("cardTitle")

        self.counter = QLabel(
            f"Подключено 0 из {MAX_REGISTERED_TELEGRAM_ACCOUNTS} аккаунтов"
        )
        self.counter.setObjectName("mutedText")

        self.search = QLineEdit()
        self.search.setObjectName("accountSearch")
        self.search.setPlaceholderText("Поиск по имени, @username, телефону или Telegram ID")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._rebuild_selector)

        self.selector = QComboBox()
        self.selector.setObjectName("accountSelector")
        self.selector.setToolTip(
            "Выбор меняет только отображаемый аккаунт. "
            "Фоновые кампании остальных аккаунтов продолжаются."
        )
        self.selector.currentIndexChanged.connect(self._selection_changed)

        self.state_dot = QLabel("●")
        self.state_dot.setObjectName("accountStateDisconnected")
        self.state_text = QLabel("Аккаунт не выбран")
        self.state_text.setObjectName("accountStateText")
        self.details = QLabel("")
        self.details.setObjectName("mutedText")
        self.details.setWordWrap(True)

        state_row = QHBoxLayout()
        state_row.addWidget(self.state_dot)
        state_row.addWidget(self.state_text)
        state_row.addStretch(1)

        self.add_button = QPushButton("Добавить аккаунт")
        self.add_button.setObjectName("primaryButton")
        self.add_button.clicked.connect(self.add_requested.emit)

        self.stop_button = QPushButton("Остановить работу")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.clicked.connect(self._stop_clicked)

        self.resume_button = QPushButton("Возобновить работу")
        self.resume_button.setObjectName("secondaryButton")
        self.resume_button.clicked.connect(self._resume_clicked)

        self.reauthorize_button = QPushButton("Переподключить")
        self.reauthorize_button.setObjectName("secondaryButton")
        self.reauthorize_button.clicked.connect(self._reauthorize_clicked)

        self.delete_button = QPushButton()
        self.delete_button.setObjectName("accountDeleteButton")
        self.delete_button.setToolTip("Удалить выбранный аккаунт")
        self.delete_button.setAccessibleName("Удалить выбранный аккаунт")
        self.delete_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon)
        )
        self.delete_button.setFixedWidth(48)
        self.delete_button.clicked.connect(self._delete_clicked)

        self.import_comments_button = QPushButton(
            "Импортировать комментарии из предыдущего аккаунта"
        )
        self.import_comments_button.setObjectName("secondaryButton")
        self.import_comments_button.clicked.connect(
            self.import_comments_requested.emit
        )

        self.import_channels_button = QPushButton(
            "Импортировать каналы из предыдущего аккаунта"
        )
        self.import_channels_button.setObjectName("secondaryButton")
        self.import_channels_button.clicked.connect(
            self.import_channels_requested.emit
        )

        self.selector_row = QHBoxLayout()
        self.selector_row.setSpacing(10)
        self.selector_row.addWidget(self.selector, 1)
        self.selector_row.addWidget(self.delete_button, 0)

        actions = QGridLayout()
        self.actions_layout = actions
        actions.addWidget(self.add_button, 0, 0)
        actions.addWidget(self.stop_button, 0, 1)
        actions.addWidget(self.resume_button, 0, 2)
        actions.addWidget(self.reauthorize_button, 1, 0)
        actions.setColumnStretch(3, 1)

        imports = QVBoxLayout()
        imports.addWidget(self.import_comments_button)
        imports.addWidget(self.import_channels_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(self.counter)
        layout.addWidget(self.search)
        layout.addLayout(self.selector_row)
        layout.addLayout(state_row)
        layout.addWidget(self.details)
        layout.addLayout(actions)
        layout.addLayout(imports)

        self.reload([], selected_account_id=0, previous_account_id=0)

    @staticmethod
    def _display(account: dict) -> str:
        name = str(account.get("display_name") or "Telegram Account")
        username = str(account.get("username") or "").strip()
        phone = str(account.get("phone_masked") or "").strip()
        parts = [name]
        if username:
            parts.append(f"@{username}")
        if phone:
            parts.append(phone)
        return " · ".join(parts)

    @staticmethod
    def _search_text(account_id: int, account: dict) -> str:
        return " ".join(
            (
                str(account_id),
                str(account.get("display_name") or ""),
                str(account.get("username") or ""),
                str(account.get("phone_masked") or ""),
            )
        ).casefold()

    def reload(
        self,
        accounts: list[dict],
        *,
        selected_account_id: int,
        previous_account_id: int,
    ) -> None:
        self._accounts = {
            int(item.get("telegram_account_id") or 0): dict(item)
            for item in accounts
            if int(item.get("telegram_account_id") or 0) > 0
        }
        requested = int(selected_account_id or 0)
        self._selected_account_id = (
            requested
            if requested in self._accounts
            else next(iter(self._accounts), 0)
        )
        self._previous_account_id = int(previous_account_id or 0)

        count = len(self._accounts)
        limit = MAX_REGISTERED_TELEGRAM_ACCOUNTS
        self.counter.setText(f"Подключено {count} из {limit} аккаунтов")
        allowed = count < limit
        self.add_button.setEnabled(allowed)
        self.add_button.setToolTip("" if allowed else account_limit_message(limit))
        self._rebuild_selector()
        self._sync_import_buttons()
        self._render_selected()

    def _rebuild_selector(self, _text: str | None = None) -> None:
        query = self.search.text().strip().casefold()
        self.selector.blockSignals(True)
        self.selector.clear()
        for account_id, account in self._accounts.items():
            if query and query not in self._search_text(account_id, account):
                continue
            state = STATE_LABELS.get(
                str(account.get("runtime_state") or ""), "Неизвестно"
            )
            campaign = " · Кампания активна" if account.get("campaign_active") else ""
            self.selector.addItem(
                f"{self._display(account)} — {state}{campaign}",
                account_id,
            )
        index = self.selector.findData(self._selected_account_id)
        self.selector.setCurrentIndex(index if index >= 0 else -1)
        self.selector.blockSignals(False)

    def _sync_import_buttons(self) -> None:
        previous_available = (
            self._previous_account_id in self._accounts
            and self._previous_account_id != self._selected_account_id
        )
        tooltip = (
            ""
            if previous_available
            else "Сначала переключитесь с другого подключённого аккаунта."
        )
        for button in (
            self.import_comments_button,
            self.import_channels_button,
        ):
            button.setEnabled(previous_available)
            button.setToolTip(tooltip)

    def _selection_changed(self, _index: int) -> None:
        account_id = int(self.selector.currentData() or 0)
        if account_id > 0:
            self.account_selected.emit(account_id)

    def set_selected_account_id(self, account_id: int) -> None:
        self._selected_account_id = int(account_id or 0)
        self._rebuild_selector()
        self._sync_import_buttons()
        self._render_selected()

    def _render_selected(self) -> None:
        account = self._accounts.get(self._selected_account_id)
        if not account:
            self.state_text.setText("Аккаунт не выбран")
            self.details.setText("")
            self.state_dot.setObjectName("accountStateDisconnected")
            self.stop_button.setEnabled(False)
            self.resume_button.setEnabled(False)
            self.reauthorize_button.setEnabled(False)
            self.delete_button.setEnabled(False)
            return
        state = str(account.get("runtime_state") or "disconnected")
        label = STATE_LABELS.get(state, state)
        self.state_text.setText(label)
        self.details.setText(f"Telegram ID: {self._selected_account_id}")
        self.state_dot.setObjectName(
            {
                "connected": "accountStateOnline",
                "running": "accountStateOnline",
                "paused": "accountStatePaused",
                "stopping": "accountStateWarning",
                "network_wait": "accountStateWarning",
                "flood_wait": "accountStateWarning",
                "restricted": "accountStateError",
                "error": "accountStateError",
                "stopped": "accountStateStopped",
                "authorization_required": "accountStateError",
            }.get(state, "accountStateDisconnected")
        )
        self.state_dot.style().unpolish(self.state_dot)
        self.state_dot.style().polish(self.state_dot)
        stopped = bool(account.get("stopped")) or state == "stopped"
        self.stop_button.setEnabled(not stopped)
        self.resume_button.setEnabled(stopped)
        self.reauthorize_button.setEnabled(True)
        self.delete_button.setEnabled(True)

    def _stop_clicked(self) -> None:
        if self._selected_account_id > 0:
            self.stop_requested.emit(self._selected_account_id)

    def _resume_clicked(self) -> None:
        if self._selected_account_id > 0:
            self.resume_requested.emit(self._selected_account_id)

    def _reauthorize_clicked(self) -> None:
        if self._selected_account_id > 0:
            self.reauthorize_requested.emit(self._selected_account_id)

    def _delete_clicked(self) -> None:
        if self._selected_account_id > 0:
            self.delete_requested.emit(self._selected_account_id)

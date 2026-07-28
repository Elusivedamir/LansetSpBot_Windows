from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
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
    import_comments_requested = Signal()
    import_channels_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("accountManagerCard")
        self._accounts: dict[int, dict] = {}
        self._selected_account_id = 0

        title = QLabel("Подключённые аккаунты")
        title.setObjectName("cardTitle")

        self.counter = QLabel("Подключено 0 из 5 аккаунтов")
        self.counter.setObjectName("mutedText")

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

        actions = QHBoxLayout()
        actions.addWidget(self.add_button)
        actions.addWidget(self.stop_button)
        actions.addWidget(self.resume_button)
        actions.addStretch(1)

        imports = QVBoxLayout()
        imports.addWidget(self.import_comments_button)
        imports.addWidget(self.import_channels_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(self.counter)
        layout.addWidget(self.selector)
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
        self._selected_account_id = int(selected_account_id or 0)
        self.selector.blockSignals(True)
        self.selector.clear()
        for account_id, account in self._accounts.items():
            state = STATE_LABELS.get(
                str(account.get("runtime_state") or ""), "Неизвестно"
            )
            campaign = " · Кампания активна" if account.get("campaign_active") else ""
            self.selector.addItem(
                f"{self._display(account)} — {state}{campaign}",
                account_id,
            )
        index = self.selector.findData(self._selected_account_id)
        self.selector.setCurrentIndex(index if index >= 0 else (0 if self._accounts else -1))
        self.selector.blockSignals(False)
        if self.selector.currentIndex() >= 0:
            self._selected_account_id = int(self.selector.currentData() or 0)

        count = len(self._accounts)
        self.counter.setText(f"Подключено {count} из 5 аккаунтов")
        allowed = count < 5
        self.add_button.setEnabled(allowed)
        self.add_button.setToolTip(
            ""
            if allowed
            else "Достигнут лимит: можно подключить не более 5 Telegram-аккаунтов."
        )
        previous_available = (
            int(previous_account_id or 0) in self._accounts
            and int(previous_account_id or 0) != self._selected_account_id
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
        self._render_selected()

    def _selection_changed(self, _index: int) -> None:
        account_id = int(self.selector.currentData() or 0)
        if account_id <= 0:
            self._render_selected()
            return
        # Signals are blocked for every programmatic reload/set. Therefore an
        # actual index change here is always a user intent, including switching
        # back to the currently committed account while another selection is
        # still queued.
        self.account_selected.emit(account_id)

    def set_selected_account_id(self, account_id: int) -> None:
        self._selected_account_id = int(account_id or 0)
        index = self.selector.findData(self._selected_account_id)
        if index >= 0:
            self.selector.blockSignals(True)
            self.selector.setCurrentIndex(index)
            self.selector.blockSignals(False)
        self._render_selected()

    def _render_selected(self) -> None:
        account = self._accounts.get(self._selected_account_id)
        if not account:
            self.state_text.setText("Аккаунт не выбран")
            self.details.setText("")
            self.state_dot.setObjectName("accountStateDisconnected")
            self.stop_button.setEnabled(False)
            self.resume_button.setEnabled(False)
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

    def _stop_clicked(self) -> None:
        if self._selected_account_id > 0:
            self.stop_requested.emit(self._selected_account_id)

    def _resume_clicked(self) -> None:
        if self._selected_account_id > 0:
            self.resume_requested.emit(self._selected_account_id)

from __future__ import annotations

import re
import unicodedata

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
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
    "flood_wait": "Telegram FloodWait",
    "restricted": "Ограничен",
    "authorization_required": "Нужна авторизация",
    "error": "Ошибка",
}

_TECHNICAL_NAME_PATTERNS = (
    re.compile(r"^telegram\s*account$", re.IGNORECASE),
    re.compile(r"^(?:pending_|account_|session_)[a-z0-9_.-]+$", re.IGNORECASE),
    re.compile(r"^[a-f0-9]{24,}$", re.IGNORECASE),
    re.compile(r"^-?\d{10,}:[a-z0-9+/=_-]{12,}$", re.IGNORECASE),
)


def _clean_text(value: object) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = " ".join(text.replace("\ufffd", "").split())
    return "".join(ch for ch in text if ch.isprintable()).strip()


def _safe_name(account: dict) -> str:
    first = _clean_text(account.get("first_name"))
    last = _clean_text(account.get("last_name"))
    explicit = " ".join(part for part in (first, last) if part)
    candidate = explicit or _clean_text(account.get("display_name"))
    if not candidate:
        return ""
    if any(pattern.fullmatch(candidate) for pattern in _TECHNICAL_NAME_PATTERNS):
        return ""
    lowered = candidate.casefold()
    if any(token in lowered for token in ("access_hash", "auth_key", "string_session")):
        return ""
    return candidate[:160]


def _safe_username(account: dict) -> str:
    username = _clean_text(account.get("username")).lstrip("@")
    if not username or not re.fullmatch(r"[A-Za-z0-9_]{3,64}", username):
        return ""
    return username


def format_account_identity(account_id: int, account: dict | None) -> str:
    """Return only stable human identity fields, never session internals."""

    owner = max(0, int(account_id or 0))
    row = account or {}
    name = _safe_name(row)
    username = _safe_username(row)
    telegram_id = f"Telegram ID {owner}" if owner > 0 else "Telegram ID не определён"
    if name and username:
        return f"{name} · @{username}"
    if username:
        return f"@{username} · {telegram_id}"
    if name:
        return f"{name} · {telegram_id}"
    return telegram_id if owner > 0 else "Telegram-аккаунт не выбран"


def account_search_text(account_id: int, account: dict) -> str:
    first = _clean_text(account.get("first_name"))
    last = _clean_text(account.get("last_name"))
    full = " ".join(part for part in (first, last) if part)
    username = _safe_username(account)
    phone = _clean_text(account.get("phone") or account.get("phone_masked"))
    values = (
        str(account_id),
        first,
        last,
        full,
        _safe_name(account),
        username,
        f"@{username}" if username else "",
        phone,
    )
    return " ".join(values).casefold()


class AccountManagerPanel(QFrame):
    """Compact selector/actions panel; selected identity is rendered above it."""

    account_selected = Signal(int)
    add_requested = Signal()
    stop_requested = Signal(int)
    resume_requested = Signal(int)
    reauthorize_requested = Signal(int)
    delete_requested = Signal(int)  # compatibility; visible trash lives in AccountView
    import_comments_requested = Signal()
    import_channels_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("accountManagerCard")
        self._accounts: dict[int, dict] = {}
        self._selected_account_id = 0
        self._previous_account_id = 0

        title = QLabel("Управление аккаунтом")
        title.setObjectName("cardTitle")
        self.counter = QLabel(
            f"Подключено 0 из {MAX_REGISTERED_TELEGRAM_ACCOUNTS} аккаунтов"
        )
        self.counter.setObjectName("mutedText")

        self.search = QLineEdit()
        self.search.setObjectName("accountSearch")
        self.search.setPlaceholderText(
            "Поиск по имени, @username, телефону или Telegram ID"
        )
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._rebuild_selector)

        self.selector = QComboBox()
        self.selector.setObjectName("accountSelector")
        self.selector.setToolTip(
            "Аккаунт переключается только после явного выбора результата. "
            "Фоновые кампании других аккаунтов продолжаются."
        )
        self.selector.currentIndexChanged.connect(self._selection_changed)

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

        self.import_comments_button = QPushButton(
            "Импортировать комментарии из другого аккаунта"
        )
        self.import_comments_button.setObjectName("secondaryButton")
        self.import_comments_button.clicked.connect(self.import_comments_requested.emit)
        self.import_channels_button = QPushButton(
            "Импортировать каналы из другого аккаунта"
        )
        self.import_channels_button.setObjectName("secondaryButton")
        self.import_channels_button.clicked.connect(self.import_channels_requested.emit)

        actions = QGridLayout()
        self.actions_layout = actions
        actions.setHorizontalSpacing(8)
        actions.setVerticalSpacing(8)
        actions.addWidget(self.add_button, 0, 0)
        actions.addWidget(self.stop_button, 0, 1)
        actions.addWidget(self.resume_button, 0, 2)
        actions.addWidget(self.reauthorize_button, 1, 0)
        actions.setColumnStretch(3, 1)

        imports = QGridLayout()
        imports.setHorizontalSpacing(8)
        imports.addWidget(self.import_comments_button, 0, 0)
        imports.addWidget(self.import_channels_button, 0, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(self.counter)
        layout.addWidget(self.search)
        layout.addWidget(self.selector)
        layout.addLayout(actions)
        layout.addLayout(imports)

        # Retained as invisible compatibility attributes for old tests/callers.
        self.state_dot = QLabel()
        self.state_text = QLabel()
        self.details = QLabel()
        for widget in (self.state_dot, self.state_text, self.details):
            widget.hide()

        self.reload([], selected_account_id=0, previous_account_id=0)

    @staticmethod
    def _display(account_id: int, account: dict) -> str:
        identity = format_account_identity(account_id, account)
        state = STATE_LABELS.get(
            str(account.get("runtime_state") or "disconnected"), "Неизвестно"
        )
        campaign = " · кампания активна" if account.get("campaign_active") else ""
        return f"{identity} — {state}{campaign}"

    @staticmethod
    def _search_text(account_id: int, account: dict) -> str:
        return account_search_text(account_id, account)

    def reload(
        self,
        accounts: list[dict],
        *,
        selected_account_id: int,
        previous_account_id: int,
    ) -> None:
        self._accounts = {
            int(item.get("telegram_account_id") or item.get("id") or 0): dict(item)
            for item in accounts
            if int(item.get("telegram_account_id") or item.get("id") or 0) > 0
        }
        requested = int(selected_account_id or 0)
        self._selected_account_id = requested if requested in self._accounts else 0
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

    def set_data_counts(
        self,
        *,
        comment_counts: dict[int, int] | None = None,
        channel_counts: dict[int, int] | None = None,
    ) -> None:
        comments = comment_counts or {}
        channels = channel_counts or {}
        for account_id, account in self._accounts.items():
            account["_comment_import_count"] = max(0, int(comments.get(account_id, 0)))
            account["_channel_import_count"] = max(0, int(channels.get(account_id, 0)))
        self._sync_import_buttons()

    def source_accounts(self, kind: str) -> list[dict]:
        key = (
            "_comment_import_count"
            if str(kind) == "comments"
            else "_channel_import_count"
        )
        result: list[dict] = []
        for account_id, account in self._accounts.items():
            if account_id == self._selected_account_id:
                continue
            count = max(0, int(account.get(key) or 0))
            if count <= 0:
                continue
            row = dict(account)
            row["telegram_account_id"] = account_id
            row["import_count"] = count
            result.append(row)
        return sorted(
            result,
            key=lambda item: format_account_identity(
                int(item["telegram_account_id"]), item
            ).casefold(),
        )

    def selected_account(self) -> dict | None:
        row = self._accounts.get(self._selected_account_id)
        return dict(row) if row else None

    def selected_account_id(self) -> int:
        return int(self._selected_account_id or 0)

    def selected_identity(self) -> str:
        return format_account_identity(
            self._selected_account_id, self._accounts.get(self._selected_account_id)
        )

    def _rebuild_selector(self, _text: str | None = None) -> None:
        query = self.search.text().strip().casefold()
        selected_before = self._selected_account_id
        self.selector.blockSignals(True)
        self.selector.clear()
        for account_id, account in self._accounts.items():
            if query and query not in self._search_text(account_id, account):
                continue
            self.selector.addItem(self._display(account_id, account), account_id)
        index = self.selector.findData(selected_before)
        self.selector.setCurrentIndex(index if index >= 0 else -1)
        self.selector.blockSignals(False)

    def _sync_import_buttons(self) -> None:
        comment_available = bool(self.source_accounts("comments"))
        channel_available = bool(self.source_accounts("channels"))
        tooltip = "Нет другого аккаунта с данными для импорта"
        self.import_comments_button.setEnabled(comment_available)
        self.import_comments_button.setToolTip("" if comment_available else tooltip)
        self.import_channels_button.setEnabled(channel_available)
        self.import_channels_button.setToolTip("" if channel_available else tooltip)

    def _selection_changed(self, _index: int) -> None:
        account_id = int(self.selector.currentData() or 0)
        if account_id > 0 and account_id != self._selected_account_id:
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
            for button in (self.stop_button, self.resume_button, self.reauthorize_button):
                button.setEnabled(False)
            return
        state = str(account.get("runtime_state") or "disconnected")
        self.state_text.setText(STATE_LABELS.get(state, "Неизвестно"))
        self.details.setText(format_account_identity(self._selected_account_id, account))
        stopped = bool(account.get("stopped")) or state == "stopped"
        self.stop_button.setEnabled(not stopped)
        self.resume_button.setEnabled(stopped)
        self.reauthorize_button.setEnabled(True)

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

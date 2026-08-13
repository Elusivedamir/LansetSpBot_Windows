from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from gui.background import BackgroundCall, connect_lifecycle_safe
from services.observability import humanize_reason


class AccountHealthCard(QFrame):
    """Read-only account telemetry; it never sends or joins anything."""

    def __init__(self, adapter: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.adapter = adapter
        self._job: BackgroundCall | None = None
        self._refresh_pending = False
        self.setObjectName("infoCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("Состояние аккаунта")
        title.setObjectName("cardTitle")
        self.refresh_button = QPushButton("Обновить")
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.clicked.connect(self.refresh)
        self.diagnostic_button = QPushButton("Проверить аккаунт")
        self.diagnostic_button.setObjectName("secondaryButton")
        self.diagnostic_button.clicked.connect(self.run_diagnostics)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.refresh_button)
        header.addWidget(self.diagnostic_button)
        layout.addLayout(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(7)
        rows = (
            ("status", "Статус"),
            ("proxy", "Прокси"),
            ("current_task", "Текущая задача"),
            ("sent_24h", "Успешно за 24 часа"),
            ("errors_24h", "Ошибок за 24 часа"),
            ("flood_wait", "FloodWait"),
            ("safety_mode", "Режим безопасности"),
            ("safety_recovery", "До следующего снижения защиты"),
            ("last_success", "Последняя успешная операция"),
            ("last_error", "Последняя ошибка"),
        )
        self.values: dict[str, QLabel] = {}
        for row, (key, caption) in enumerate(rows):
            name = QLabel(caption)
            name.setObjectName("mutedText")
            value = QLabel("—")
            value.setObjectName("statusTitle" if key == "status" else "mutedText")
            value.setWordWrap(True)
            self.values[key] = value
            grid.addWidget(name, row, 0)
            grid.addWidget(value, row, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        self.diagnostic_result = QLabel(
            "Диагностика проверяет авторизацию и соединение, но ничего не отправляет."
        )
        self.diagnostic_result.setObjectName("mutedText")
        self.diagnostic_result.setWordWrap(True)
        layout.addWidget(self.diagnostic_result)
        # A static singleShot keeps the Python bound method alive even after
        # Qt has deleted this card.  A child timer is destroyed with the card,
        # so a queued first refresh cannot touch deleted widgets later.
        self._initial_refresh_timer = QTimer(self)
        self._initial_refresh_timer.setSingleShot(True)
        self._initial_refresh_timer.timeout.connect(self.refresh)
        self._initial_refresh_timer.start(0)

    def _account_id(self) -> int:
        for name in ("get_selected_account_id", "get_current_account_id"):
            method = getattr(self.adapter, name, None)
            if not callable(method):
                continue
            try:
                return max(0, int(method() or 0))
            except (TypeError, ValueError, OverflowError, RuntimeError):
                continue
        return 0

    def _set_busy(self, busy: bool) -> None:
        self.refresh_button.setEnabled(not busy)
        self.diagnostic_button.setEnabled(not busy and self._account_id() > 0)

    def refresh(self) -> None:
        if self._job is not None:
            self._refresh_pending = True
            return
        self._refresh_pending = False
        account_id = self._account_id()
        if account_id <= 0:
            self.values["status"].setText("Аккаунт не выбран")
            self.diagnostic_button.setEnabled(False)
            return
        self._set_busy(True)
        cleanup = getattr(self.adapter, "close_thread_connection", None)
        job = BackgroundCall(
            lambda: self.adapter.get_account_observability(account_id),
            cleanup=cleanup if callable(cleanup) else None,
        )
        self._job = job

        def succeeded(card: AccountHealthCard, result: object) -> None:
            if account_id != card._account_id() or not isinstance(result, Mapping):
                return
            for key, label in card.values.items():
                label.setText(str(result.get(key, "—")))

        def failed(card: AccountHealthCard, message: str) -> None:
            if account_id != card._account_id():
                return
            card.values["status"].setText("Не удалось обновить")
            card.values["last_error"].setText(humanize_reason(message))

        def finished(card: AccountHealthCard) -> None:
            if card._job is job:
                card._job = None
            card._set_busy(False)
            if card._refresh_pending:
                card._refresh_pending = False
                QTimer.singleShot(0, card.refresh)

        connect_lifecycle_safe(job, self, succeeded=succeeded, failed=failed, finished=finished)
        QThreadPool.globalInstance().start(job)

    def run_diagnostics(self) -> None:
        if self._job is not None:
            return
        account_id = self._account_id()
        if account_id <= 0:
            self.diagnostic_result.setText("Сначала выберите Telegram-аккаунт")
            return
        self._set_busy(True)
        self.diagnostic_result.setText("Проверяем авторизацию, прокси и Telegram API…")
        cleanup = getattr(self.adapter, "close_thread_connection", None)
        job = BackgroundCall(
            lambda: self.adapter.check_telegram_account_runtime(account_id),
            cleanup=cleanup if callable(cleanup) else None,
        )
        self._job = job

        def succeeded(card: AccountHealthCard, result: object) -> None:
            if account_id != card._account_id():
                return
            data = dict(result) if isinstance(result, Mapping) else {}
            authorized = data.get("authorized", data.get("ok"))
            connected = data.get("connected")
            parts = [
                "Авторизация: " + ("да" if authorized is True else "нет" if authorized is False else "проверена"),
                "Соединение: " + ("работает" if connected is True else "недоступно" if connected is False else "проверено"),
            ]
            detail = data.get("message") or data.get("status_text") or data.get("state")
            if detail:
                parts.append(humanize_reason(detail))
            parts.append("Отправка и вступление не выполнялись")
            card.diagnostic_result.setText(" · ".join(parts))
            QTimer.singleShot(0, card.refresh)

        def failed(card: AccountHealthCard, message: str) -> None:
            if account_id != card._account_id():
                return
            card.diagnostic_result.setText(
                f"Диагностика не пройдена · {humanize_reason(message)}"
            )

        def finished(card: AccountHealthCard) -> None:
            if card._job is job:
                card._job = None
            card._set_busy(False)
            if card._refresh_pending:
                card._refresh_pending = False
                QTimer.singleShot(0, card.refresh)

        connect_lifecycle_safe(job, self, succeeded=succeeded, failed=failed, finished=finished)
        QThreadPool.globalInstance().start(job)

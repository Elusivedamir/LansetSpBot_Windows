from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from gui.background import BackgroundCall, connect_lifecycle_safe

_DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


class AnalyticsView(QWidget):
    """Read-only operational analytics from local ledger/status evidence."""

    def __init__(self, adapter: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.adapter = adapter
        self._job: BackgroundCall | None = None
        self._page_active = False
        self._last_data: dict[str, Any] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(16)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Операционная аналитика")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Фактические ledger/status/FloodWait данные; спекулятивного ban-risk score нет.")
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        self.refresh_button = QPushButton("Обновить")
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.clicked.connect(self.refresh)
        self.json_button = QPushButton("JSON")
        self.json_button.clicked.connect(lambda: self._export("json"))
        self.csv_button = QPushButton("CSV")
        self.csv_button.clicked.connect(lambda: self._export("csv"))
        header.addWidget(self.refresh_button)
        header.addWidget(self.json_button)
        header.addWidget(self.csv_button)
        layout.addLayout(header)
        self.summary = QLabel("Данные ещё не загружены")
        self.summary.setObjectName("infoCard")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.accounts = self._card_table(layout, "Аккаунты и режим безопасности", 8,
            ["Аккаунт", "Статус", "Safety", "Восстановление", "Успех 24ч", "Ошибки 24ч", "FloodWait", "Прокси"])
        self.heatmap = self._card_table(layout, "Heatmap успешных комментариев · UTC · 7 дней", 24,
            [f"{hour:02d}" for hour in range(24)])
        self.heatmap.setRowCount(7)
        self.heatmap.setVerticalHeaderLabels(list(_DAYS))
        self.channels = self._card_table(layout, "Top channels · подтверждённые успехи", 3,
            ["Канал", "ID", "Успешно"])
        self.safety_events = self._card_table(layout, "Журнал Safety", 5,
            ["Время", "Аккаунт", "Событие", "Переход", "Код"])
        self.timer = QTimer(self)
        self.timer.setInterval(30_000)
        self.timer.timeout.connect(self.refresh)

    @staticmethod
    def _card_table(parent_layout: QVBoxLayout, title: str, columns: int,
                    headers: list[str]) -> QTableWidget:
        card = QFrame()
        card.setObjectName("infoCard")
        layout = QVBoxLayout(card)
        layout.addWidget(QLabel(title))
        table = QTableWidget(0, columns)
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        layout.addWidget(table)
        parent_layout.addWidget(card)
        return table

    def set_page_active(self, active: bool) -> None:
        self._page_active = bool(active)
        if self._page_active:
            self.timer.start()
            self.refresh()
        else:
            self.timer.stop()

    def set_compact_mode(self, _compact: bool) -> None:
        return

    def handle_account_changed(self, *_args: object) -> None:
        if self._page_active:
            self.refresh()

    def refresh(self) -> None:
        if self._job is not None:
            return
        self.refresh_button.setEnabled(False)
        cleanup = getattr(self.adapter, "close_thread_connection", None)
        job = BackgroundCall(self.adapter.get_operational_analytics,
            cleanup=cleanup if callable(cleanup) else None)
        self._job = job
        def succeeded(view: AnalyticsView, result: object) -> None:
            if isinstance(result, Mapping):
                view._last_data = dict(result)
                view._render(view._last_data)
        def failed(view: AnalyticsView, message: str) -> None:
            view.summary.setText(f"Не удалось обновить аналитику: {message}")
        def finished(view: AnalyticsView) -> None:
            if view._job is job:
                view._job = None
            view.refresh_button.setEnabled(True)
        connect_lifecycle_safe(job, self, succeeded=succeeded, failed=failed, finished=finished)
        QThreadPool.globalInstance().start(job)

    def _render(self, data: Mapping[str, Any]) -> None:
        totals = dict(data.get("totals") or {})
        self.summary.setText(" · ".join((
            f"Аккаунтов: {int(totals.get('accounts') or 0)}",
            f"Success 24ч: {int(totals.get('sent_24h') or 0)}",
            f"Ошибки/uncertain 24ч: {int(totals.get('errors_24h') or 0)}",
            f"Proxy coverage: {int(totals.get('proxy_coverage_percent') or 0)}%",
            f"Protective: {int(totals.get('protective_accounts') or 0)}",
            f"Conservative: {int(totals.get('conservative_accounts') or 0)}",
        )))
        accounts = list(data.get("accounts") or [])
        self.accounts.setRowCount(len(accounts))
        for r, value in enumerate(accounts):
            account_row = dict(value or {})
            account_values = (
                account_row.get("display_name") or account_row.get("account_id") or "—",
                account_row.get("status") or "—",
                str(account_row.get("safety_mode") or "normal").upper(),
                account_row.get("safety_recovery") or "—",
                account_row.get("sent_24h") or 0,
                account_row.get("errors_24h") or 0,
                account_row.get("flood_wait") or "—",
                account_row.get("proxy") or "—",
            )
            for c, item in enumerate(account_values):
                self.accounts.setItem(r, c, QTableWidgetItem(str(item)))
        self.accounts.resizeColumnsToContents()
        matrix = list(data.get("heatmap") or [])
        for day in range(7):
            heatmap_row = list(matrix[day] if day < len(matrix) else [])
            for hour in range(24):
                self.heatmap.setItem(
                    day,
                    hour,
                    QTableWidgetItem(
                        str(heatmap_row[hour] if hour < len(heatmap_row) else 0)
                    ),
                )
        channels = list(data.get("top_channels") or [])
        self.channels.setRowCount(len(channels))
        for r, value in enumerate(channels):
            channel_row = dict(value or {})
            for c, item in enumerate(
                (
                    channel_row.get("title")
                    or channel_row.get("channel_id")
                    or "—",
                    channel_row.get("channel_id") or "—",
                    channel_row.get("sent") or 0,
                )
            ):
                self.channels.setItem(r, c, QTableWidgetItem(str(item)))
        self.channels.resizeColumnsToContents()
        events = list(data.get("safety_events") or [])
        self.safety_events.setRowCount(len(events))
        for r, value in enumerate(events):
            event_row = dict(value or {})
            event_values = (
                event_row.get("occurred_at") or "—",
                event_row.get("account_id") or "—",
                event_row.get("event_type") or "—",
                f"{event_row.get('from_level') or '—'} → {event_row.get('to_level') or '—'}",
                event_row.get("code") or "—",
            )
            for c, item in enumerate(event_values):
                self.safety_events.setItem(r, c, QTableWidgetItem(str(item)))
        self.safety_events.resizeColumnsToContents()

    def _export(self, format_name: str) -> None:
        if not self._last_data:
            self.refresh()
            return
        suffix = "json" if format_name == "json" else "csv"
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт операционной аналитики",
            str(Path.home() / f"lanset-operational-analytics.{suffix}"),
            "JSON (*.json)" if suffix == "json" else "CSV (*.csv)")
        if not path:
            return
        try:
            Path(path).write_text(str(self.adapter.export_operational_analytics(format_name)),
                                  encoding="utf-8", newline="\n")
        except Exception as exc:
            QMessageBox.critical(self, "Экспорт", f"Не удалось сохранить аналитику:\n{exc}")

from __future__ import annotations

import math

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHeaderView,
    QGridLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.campaign_schedule import from_db_time, utc_now
from core.version import APP_NAME
from gui.views.common import TaskWatcher


class LinksView(QWidget):
    def __init__(self, adapter):
        super().__init__()
        self.adapter = adapter
        self.current_task_id = None
        self._account_id = 0
        self.total = 0
        self._last_rendered_progress: int | None = None
        self._due_restart_task_id: int | None = None
        self._page_active = True
        self.watcher = TaskWatcher(adapter, self)
        self.watcher.changed.connect(self._task_changed)
        self.watcher.completed.connect(self._task_finished)

        title = QLabel("Связки каналов")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "LansetSpBot подготовит маршруты: для каналов найдёт обсуждения, "
            "а обычные группы отметит для сообщений без привязки к посту."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)

        info_card = QFrame()
        info_card.setObjectName("infoCard")
        info_layout = QVBoxLayout(info_card)
        info_title = QLabel("Что произойдёт")
        info_title.setObjectName("cardTitle")
        info = QLabel(
            "Для канала программа получает ID обсуждения через Telegram API, один раз "
            "проверяет участие и при необходимости вступает. Обычная группа не требует "
            "поста или связанного обсуждения: она сохраняется как отдельный маршрут для "
            "прямого сообщения. Между проверками выдерживается случайная пауза 3–7 секунд, "
            "между новыми вступлениями — 15–25 секунд. Каждый объект проверяется один раз. "
            "Кнопка «Стоп» не сокращает FloodWait: задача дождётся Telegram-таймера и "
            "защитного буфера, затем продолжит или останется на паузе до продолжения."
        )
        info.setWordWrap(True)
        info.setObjectName("mutedText")
        info_layout.addWidget(info_title)
        info_layout.addWidget(info)

        self.link_button = QPushButton("Связать каналы и вступить в обсуждения")
        self.link_button.setObjectName("primaryButton")
        self.link_button.clicked.connect(self.start_linking)
        self.stop_button = QPushButton("Стоп")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_linking)
        self.status = QLabel("Готово к проверке")
        self.status.setObjectName("statusTitle")
        self.buttons_layout = QGridLayout()
        self.buttons_layout.addWidget(self.link_button, 0, 0)
        self.buttons_layout.addWidget(self.stop_button, 0, 1)
        self.buttons_layout.setColumnStretch(2, 1)
        self.buttons_layout.addWidget(self.status, 0, 3)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Канал", "ID канала", "Чат обсуждения", "Статус"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 28, 34, 28)
        layout.setSpacing(18)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(info_card)
        layout.addLayout(self.buttons_layout)
        layout.addWidget(self.progress)
        layout.addWidget(self.table, 1)
        self.load_channels()
        QTimer.singleShot(0, self._restore_active_task)

    def _current_account_id(self) -> int:
        getter = getattr(self.adapter, "get_current_account_id", None)
        if not callable(getter):
            return 0
        try:
            return max(0, int(getter() or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _task_account_id(task: dict) -> int:
        payload = task.get("payload") or {}
        if not isinstance(payload, dict):
            return 0
        try:
            return max(0, int(payload.get("account_id") or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def handle_account_changed(self) -> None:
        """Forget task/UI state owned by the previous Telegram account."""

        self._account_id = self._current_account_id()
        self.watcher.stop()
        self.current_task_id = None
        self.total = 0
        self._last_rendered_progress = None
        self._due_restart_task_id = None
        self.progress.setValue(0)
        self.link_button.setEnabled(self._account_id > 0)
        self.link_button.setText("Проверить новые каналы")
        self.stop_button.setEnabled(False)
        self.status.setText(
            "Готово к проверке"
            if self._account_id > 0
            else "Telegram-аккаунт не подключён"
        )
        self.table.setRowCount(0)
        if self._page_active:
            self.load_channels()
            self._restore_active_task()

    def set_page_active(self, active: bool) -> None:
        self._page_active = bool(active)
        if self._account_id != self._current_account_id():
            self.handle_account_changed()
        self.watcher.set_active(self._page_active)
        if self._page_active:
            self.load_channels()
            if self.current_task_id is None:
                self._restore_active_task()

    def set_compact_mode(self, compact: bool) -> None:
        self.buttons_layout.removeWidget(self.link_button)
        self.buttons_layout.removeWidget(self.stop_button)
        self.buttons_layout.removeWidget(self.status)
        if compact:
            self.buttons_layout.addWidget(self.link_button, 0, 0)
            self.buttons_layout.addWidget(self.stop_button, 0, 1)
            self.buttons_layout.addWidget(self.status, 1, 0, 1, 2)
            self.table.setColumnHidden(1, True)
        else:
            self.buttons_layout.addWidget(self.link_button, 0, 0)
            self.buttons_layout.addWidget(self.stop_button, 0, 1)
            self.buttons_layout.addWidget(self.status, 0, 3)
            self.table.setColumnHidden(1, False)

    @staticmethod
    def _task_total(task: dict, fallback: int) -> int:
        payload = task.get("payload") or {}
        checkpoint = (
            payload.get("_link_checkpoint") if isinstance(payload, dict) else None
        )
        if isinstance(checkpoint, dict):
            channel_ids = checkpoint.get("channel_ids")
            group_ids = checkpoint.get("group_ids")
            if isinstance(channel_ids, list) and isinstance(group_ids, list):
                return len(channel_ids) + len(group_ids)
        return max(0, int(fallback))

    @staticmethod
    def _pause_requested(task: dict) -> bool:
        payload = task.get("payload") or {}
        return bool(isinstance(payload, dict) and payload.get("_link_pause_requested"))

    def _restore_active_task(self) -> None:
        account_id = self._current_account_id()
        self._account_id = account_id
        if account_id <= 0:
            return
        try:
            try:
                task = self.adapter.get_active_link_task(account_id=account_id)
            except TypeError:
                task = self.adapter.get_active_link_task()
        except Exception:
            return
        if not task or self._task_account_id(task) != account_id:
            return
        try:
            unchecked = self.adapter.count_unchecked_link_targets(
                account_id=account_id
            )
        except TypeError:
            unchecked = self.adapter.count_unchecked_link_targets()
        self.current_task_id = int(task["id"])
        self.total = self._task_total(task, unchecked)
        self._task_changed(task)
        if str(task.get("status") or "") != "paused":
            self.watcher.watch(self.current_task_id)

    def start_linking(self):
        all_targets = self.adapter.get_channels() or []
        if not all_targets:
            QMessageBox.information(
                self,
                "Нет каналов и групп",
                "Сначала получите каналы и группы во вкладке «Каналы»",
            )
            return
        try:
            active = self.adapter.get_active_link_task()
            unchecked = int(self.adapter.count_unchecked_link_targets())
            if active and str(active.get("status") or "") == "paused":
                task = active
                if not self.adapter.resume_link_task(int(task["id"])):
                    raise RuntimeError(
                        "Не удалось продолжить сохранённую задачу связок"
                    )
                task = dict(task)
                task["status"] = "pending"
            elif active:
                task = active
            else:
                if unchecked <= 0:
                    self.status.setText(
                        "Все каналы и чаты уже проверены · повторный обход отключён"
                    )
                    QMessageBox.information(
                        self,
                        "Связки уже проверены",
                        "Новых каналов и чатов нет. Ранее проверенные объекты повторно "
                        "не запрашиваются у Telegram.",
                    )
                    return
                task = self.adapter.create_task("link_channels", {})
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return

        self.total = self._task_total(task, unchecked)
        self.current_task_id = int(task["id"])
        self.link_button.setEnabled(False)
        self.link_button.setText("Связки выполняются")
        self.stop_button.setEnabled(True)
        self.progress.setValue(int(task.get("progress") or 0))
        self._last_rendered_progress = None
        self._due_restart_task_id = None
        self.status.setText(
            f"Продолжение сохранённой задачи #{self.current_task_id}"
            if task.get("reused") or active
            else f"Подготовлено объектов: {self.total}"
        )
        self.watcher.watch(self.current_task_id)
        if not self.adapter.start_queue():
            # Existing deferred/paused state must never be destroyed because the
            # queue cannot start. Only a just-created empty task is cancellable.
            if not task.get("reused") and not active:
                self.adapter.cancel_task(self.current_task_id)
            self.watcher.stop()
            self.link_button.setEnabled(True)
            self.link_button.setText("Проверить новые каналы")
            self.stop_button.setEnabled(False)
            self.status.setText("Не удалось запустить очередь")
            QMessageBox.warning(
                self, APP_NAME, self.adapter.get_queue_unavailable_message()
            )

    def stop_linking(self) -> None:
        if self.current_task_id is None:
            return
        try:
            changed = self.adapter.pause_link_task(int(self.current_task_id))
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if not changed:
            QMessageBox.information(
                self, APP_NAME, "Задача уже завершена или остановлена"
            )
            return
        self.stop_button.setEnabled(False)
        self.link_button.setText("Стоп принят")
        self.status.setText(
            "Стоп принят · текущий FloodWait/защитная задержка не прерываются"
        )

    def _task_changed(self, task):
        owner = self._current_account_id()
        if owner <= 0 or self._task_account_id(task) != owner:
            return
        if self.current_task_id is not None and int(task.get("id") or 0) != int(self.current_task_id):
            return
        value = int(task.get("progress") or 0)
        self.progress.setValue(value)
        done = min(self.total, round(self.total * value / 100)) if self.total else 0
        task_status = str(task.get("status") or "")
        pause_requested = self._pause_requested(task)
        if task_status == "running":
            self.link_button.setEnabled(False)
            self.link_button.setText(
                "Стоп принят" if pause_requested else "Связки выполняются"
            )
            self.stop_button.setEnabled(not pause_requested)
            self._due_restart_task_id = None
            runtime_status = str(task.get("status_text") or "").strip()
            self.status.setText(
                runtime_status
                or (
                    "Стоп принят · остановка после текущего запроса/задержки"
                    if pause_requested
                    else f"Обработано: {done} из {self.total}"
                )
            )
            if value != self._last_rendered_progress:
                self._last_rendered_progress = value
                self.load_channels()
        elif task_status == "pending":
            self.link_button.setEnabled(False)
            self.link_button.setText(
                "Стоп принят" if pause_requested else "Связки ожидают"
            )
            self.stop_button.setEnabled(not pause_requested)
            retry_at = from_db_time(task.get("not_before"))
            if retry_at is not None and retry_at > utc_now():
                remaining = max(1, math.ceil((retry_at - utc_now()).total_seconds()))
                if pause_requested:
                    self.status.setText(
                        "Стоп принят · FloodWait и защитный буфер завершатся через "
                        f"{self._format_duration(remaining)} · затем задача перейдёт "
                        f"в паузу · обработано: {done} из {self.total}"
                    )
                else:
                    self.status.setText(
                        "Telegram ограничил запросы аккаунта · все RPC остановлены · "
                        f"продолжение через {self._format_duration(remaining)} · "
                        f"обработано: {done} из {self.total}"
                    )
                self._due_restart_task_id = None
            elif task.get("not_before"):
                self.status.setText(
                    (
                        "Ожидание завершено · фиксируем сохранённую паузу…"
                        if pause_requested
                        else f"Возобновление связок… · обработано: {done} из {self.total}"
                    )
                )
                task_id = int(task.get("id") or 0)
                if task_id > 0 and self._due_restart_task_id != task_id:
                    self._due_restart_task_id = task_id
                    self.adapter.start_queue()
            elif pause_requested:
                self.status.setText("Стоп принят · фиксируем сохранённую паузу…")
                task_id = int(task.get("id") or 0)
                if task_id > 0 and self._due_restart_task_id != task_id:
                    self._due_restart_task_id = task_id
                    self.adapter.start_queue()
            else:
                self.status.setText("Ожидание запуска…")
        elif task_status == "paused":
            self.watcher.stop()
            self.link_button.setEnabled(True)
            self.link_button.setText("Продолжить связки")
            self.stop_button.setEnabled(False)
            self.status.setText(
                f"Остановлено · прогресс сохранён: {done} из {self.total}"
            )
            self.load_channels()

    @staticmethod
    def _format_duration(seconds: int) -> str:
        value = max(0, int(seconds))
        hours, remainder = divmod(value, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _task_finished(self, task):
        owner = self._current_account_id()
        if owner <= 0 or self._task_account_id(task) != owner:
            return
        if self.current_task_id is not None and int(task.get("id") or 0) != int(self.current_task_id):
            return
        self.current_task_id = None
        self.link_button.setEnabled(True)
        self.link_button.setText("Проверить новые каналы")
        self.stop_button.setEnabled(False)
        self._due_restart_task_id = None
        self._last_rendered_progress = None
        self.load_channels()
        if task.get("status") == "completed":
            self.progress.setValue(100)
            linked = sum(
                1
                for row in self.adapter.get_channels()
                if str(row.get("target_kind") or "channel") == "channel"
                and row.get("linked_chat_id")
            )
            unchecked = int(self.adapter.count_unchecked_link_targets())
            self.status.setText(
                f"Связано каналов: {linked} · новых непроверенных объектов: {unchecked}"
            )
        else:
            self.status.setText("Операция завершилась с ошибкой")
            QMessageBox.warning(
                self, "Ошибка связки", str(task.get("error") or "Неизвестная ошибка")
            )

    def load_channels(self):
        channels = [
            row
            for row in (self.adapter.get_channels() or [])
            if str(row.get("target_kind") or "channel") == "channel"
        ]
        self.table.setRowCount(len(channels))
        for row, channel in enumerate(channels):
            discussion = channel.get("linked_chat_title") or (
                str(channel.get("linked_chat_id"))
                if channel.get("linked_chat_id")
                else "—"
            )
            status = channel.get("link_status") or "Не проверено"
            values = [
                channel.get("title") or "Без названия",
                str(channel.get("channel_id") or ""),
                discussion,
                status,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)

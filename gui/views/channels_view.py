from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    QThreadPool,
    QTimer,
)
from PySide6.QtWidgets import (
    QFrame,
    QHeaderView,
    QGridLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QAbstractItemView,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from core.version import APP_NAME
from gui.background import BackgroundCall, connect_lifecycle_safe
from gui.views.common import TaskWatcher


class ChannelTableModel(QAbstractTableModel):
    HEADERS = ("Канал / группа", "Username", "Тип", "ID", "Текущий аккаунт")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[str, str, str, str, str, int]] = []

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        if role == int(Qt.ItemDataRole.DisplayRole):
            return self._rows[index.row()][index.column()]
        if role == int(Qt.ItemDataRole.UserRole):
            return self._rows[index.row()][5]
        return None

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ):
        if (
            role == int(Qt.ItemDataRole.DisplayRole)
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return None

    def replace_rows(self, rows: list[tuple[str, str, str, str, str, int]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def peer_id_at(self, row: int) -> int | None:
        if 0 <= row < len(self._rows):
            value = int(self._rows[row][5])
            return value if value else None
        return None

    def title_at(self, row: int) -> str:
        if 0 <= row < len(self._rows):
            return self._rows[row][0]
        return ""


class ChannelsView(QWidget):
    def __init__(self, adapter, queue_worker=None):
        super().__init__()
        self.adapter = adapter
        self.current_task_id = None
        self.current_mode = ""
        self._account_id = 0
        self._account_generation = 0
        self._join_refresh_job: BackgroundCall | None = None
        self._join_refresh_pending = False
        self._load_job: BackgroundCall | None = None
        self._load_generation = 0
        self._reload_requested = False
        self._page_active = True
        self.watcher = TaskWatcher(adapter, self)
        self.watcher.changed.connect(self._task_changed)
        self.watcher.completed.connect(self._task_finished)

        title = QLabel("Мои каналы и группы")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "Получите каналы и группы для комментирования и сохраните список подписок для переноса на другой аккаунт."
        )
        subtitle.setWordWrap(True)
        subtitle.setObjectName("pageSubtitle")

        instruction = QFrame()
        instruction.setObjectName("infoCard")
        info_layout = QVBoxLayout(instruction)
        info_title = QLabel("Как пользоваться")
        info_title.setObjectName("cardTitle")
        info = QLabel(
            "«Получить каналы и сохранить список» одним проходом обновляет рабочую базу и "
            "запоминает публичные каналы и группы. После смены аккаунта перенесите список "
            "кнопкой «Импортировать каналы из предыдущего аккаунта» в разделе «Аккаунт». "
            "Приватные чаты без публичного username или сохранённой инвайт-ссылки останутся в списке, "
            "но автоматически вступить в них нельзя."
        )
        info.setWordWrap(True)
        info.setObjectName("mutedText")
        info_layout.addWidget(info_title)
        info_layout.addWidget(info)

        self.sync_button = QPushButton("Получить каналы и сохранить список")
        self.sync_button.setObjectName("primaryButton")
        self.sync_button.clicked.connect(lambda: self._start_task("sync_channels"))
        self.save_button = QPushButton("Сохранить список аккаунта")
        self.save_button.setObjectName("secondaryButton")
        self.save_button.hide()
        self.join_button = QPushButton("Вступить в сохранённые")
        self.join_button.setObjectName("primaryButton")
        self.join_button.clicked.connect(self.start_join_campaign)
        self.join_button.hide()
        self.pause_join_button = QPushButton("Пауза")
        self.pause_join_button.setObjectName("secondaryButton")
        self.pause_join_button.clicked.connect(self.pause_join_campaign)
        self.pause_join_button.setEnabled(False)
        self.stop_join_button = QPushButton("Остановить")
        self.stop_join_button.setObjectName("dangerButton")
        self.stop_join_button.clicked.connect(self.stop_join_campaign)
        self.stop_join_button.setEnabled(False)
        self.delete_button = QPushButton("Удалить выбранные")
        self.delete_button.setObjectName("dangerButton")
        self.delete_button.clicked.connect(self.delete_selected_channels)

        self.buttons = [
            self.sync_button,
            self.pause_join_button,
            self.stop_join_button,
            self.delete_button,
        ]
        self.buttons_layout = QGridLayout()
        self._arrange_buttons(False)

        self.summary = QLabel("Каналы ещё не загружены")
        self.summary.setObjectName("statusTitle")
        self.join_summary = QLabel("Кампания вступлений не запущена")
        self.join_summary.setObjectName("mutedText")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()

        self.table = QTableView()
        self.channel_model = ChannelTableModel(self.table)
        self.table.setModel(self.channel_model)
        self.table.setMinimumWidth(0)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        widths = {1: 180, 2: 120, 3: 150, 4: 170}
        for col in range(1, 5):
            self.table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.Interactive
            )
            self.table.horizontalHeader().resizeSection(col, widths[col])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 28, 34, 28)
        layout.setSpacing(16)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(instruction)
        layout.addLayout(self.buttons_layout)
        layout.addWidget(self.summary)
        layout.addWidget(self.join_summary)
        layout.addWidget(self.progress)
        layout.addWidget(self.table, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.request_join_state_refresh)
        self.timer.start()
        self.load_channels()
        self.request_join_state_refresh()

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
        """Invalidate task, model and join snapshots from the previous owner."""

        self._account_generation += 1
        self._account_id = self._current_account_id()
        self.watcher.stop()
        self.current_task_id = None
        self.current_mode = ""
        self._load_generation += 1
        self._reload_requested = self._load_job is not None
        self._join_refresh_pending = self._join_refresh_job is not None
        self.progress.hide()
        self.progress.setValue(0)
        self.channel_model.replace_rows([])
        self.summary.setText(
            "Каналы ещё не загружены"
            if self._account_id > 0
            else "Telegram-аккаунт не подключён"
        )
        self._apply_join_state(
            None,
            account_id=self._account_id,
            generation=self._account_generation,
        )
        self._set_buttons(self._account_id > 0)
        if self._page_active:
            self.load_channels()
            self.request_join_state_refresh()

    def set_page_active(self, active: bool) -> None:
        self._page_active = bool(active)
        if self._account_id != self._current_account_id():
            self.handle_account_changed()
        self.watcher.set_active(self._page_active)
        if self._page_active:
            if not self.timer.isActive():
                self.timer.start()
            self.load_channels()
            self.request_join_state_refresh()
        else:
            self._account_generation += 1
            self._join_refresh_pending = False
            self.timer.stop()

    def _arrange_buttons(self, compact: bool) -> None:
        while self.buttons_layout.count():
            item = self.buttons_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.setParent(self)
        if compact:
            for index, button in enumerate(self.buttons):
                self.buttons_layout.addWidget(button, index, 0)
        else:
            for index, button in enumerate(self.buttons):
                self.buttons_layout.addWidget(button, 0, index)
        self.buttons_layout.setColumnStretch(
            len(self.buttons) if not compact else 1, 1
        )

    def set_compact_mode(self, compact: bool) -> None:
        self._arrange_buttons(compact)
        self.table.setColumnHidden(1, compact)
        self.table.setColumnHidden(2, compact)
        self.table.setColumnHidden(3, compact)

    def _start_task(self, task_type):
        try:
            task = self.adapter.create_task(task_type, {})
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.current_mode = task_type
        self.current_task_id = int(task["id"])
        self.progress.show()
        self.progress.setValue(0)
        self._set_buttons(False)
        self.summary.setText("Подключаемся к Telegram…")
        self.watcher.watch(self.current_task_id)
        if not self.adapter.start_queue():
            self.adapter.cancel_task(self.current_task_id)
            self.watcher.stop()
            self._set_buttons(True)
            QMessageBox.warning(
                self, APP_NAME, self.adapter.get_queue_unavailable_message()
            )

    def _set_buttons(self, enabled):
        self.sync_button.setEnabled(enabled)
        self.save_button.setEnabled(enabled)
        self.join_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)

    def delete_selected_channels(self):
        rows = sorted(
            {index.row() for index in self.table.selectionModel().selectedRows()}
        )
        channel_ids = []
        titles = []
        for row in rows:
            channel_id = self.channel_model.peer_id_at(row)
            if channel_id is None:
                continue
            channel_ids.append(channel_id)
            titles.append(self.channel_model.title_at(row) or str(channel_id))
        if not channel_ids:
            QMessageBox.information(
                self, APP_NAME, "Выберите один или несколько каналов в таблице."
            )
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "Удалить выбранные каналы и отменить связанные слоты и задачи?\n\n"
            + "\n".join(titles[:8])
            + ("\n…" if len(titles) > 8 else ""),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_buttons(False)
        try:
            result = self.adapter.delete_channels(channel_ids)
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
        else:
            self.load_channels()
            self.summary.setText(
                f"Удалено: {len(result.get('deleted_channel_ids', []))}; "
                f"отменено задач: {len(result.get('cancelled_task_ids', []))}"
            )
        finally:
            self._set_buttons(True)

    def _task_changed(self, task):
        owner = self._current_account_id()
        if owner <= 0 or self._task_account_id(task) != owner:
            return
        if self.current_task_id is not None and int(task.get("id") or 0) != int(self.current_task_id):
            return
        value = int(task.get("progress") or 0)
        self.progress.setValue(value)
        text = task.get("status_text") or (
            "Ожидание запуска…"
            if task.get("status") == "pending"
            else f"Выполнение · {value}%"
        )
        self.summary.setText(str(text))

    def _task_finished(self, task):
        owner = self._current_account_id()
        if owner <= 0 or self._task_account_id(task) != owner:
            return
        if self.current_task_id is not None and int(task.get("id") or 0) != int(self.current_task_id):
            return
        self.current_task_id = None
        self.current_mode = ""
        self._set_buttons(True)
        self.load_channels()
        if task.get("status") == "completed":
            self.progress.setValue(100)
            self.summary.setText(
                str(task.get("status_text") or "Список успешно обновлён")
            )
        else:
            self.summary.setText("Операция завершилась ошибкой")
            QMessageBox.warning(
                self, APP_NAME, str(task.get("error") or "Неизвестная ошибка")
            )

    def start_join_campaign(self):
        try:
            campaign = self.adapter.start_join_campaign()
        except Exception as exc:
            QMessageBox.warning(self, "Кампания вступлений", str(exc))
            return
        self.join_summary.setText(
            f"Запланировано: {campaign.get('total_count', 0)} · "
            f"лимит {campaign.get('max_per_hour', 0)} вступлений/час"
        )
        self.refresh_join_state()

    def pause_join_campaign(self):
        try:
            state = self.adapter.get_join_campaign_state()
            if state and state.get("status") == "paused":
                self.adapter.resume_join_campaign()
            else:
                self.adapter.pause_join_campaign()
        except Exception as exc:
            QMessageBox.warning(self, "Кампания вступлений", str(exc))
            return
        self.refresh_join_state()

    def stop_join_campaign(self):
        try:
            self.adapter.stop_join_campaign()
        except Exception as exc:
            QMessageBox.warning(self, "Кампания вступлений", str(exc))
            return
        self.refresh_join_state()

    def request_join_state_refresh(self) -> None:
        """Poll one account's join campaign and reject stale callbacks."""

        if not self._page_active:
            return
        if self._join_refresh_job is not None:
            self._join_refresh_pending = True
            return
        account_id = self._current_account_id()
        generation = self._account_generation
        cleanup = getattr(self.adapter, "close_thread_connection", None)

        def load_state():
            try:
                return self.adapter.get_join_campaign_state(account_id=account_id)
            except TypeError:
                return self.adapter.get_join_campaign_state()

        job = BackgroundCall(
            load_state,
            cleanup=cleanup if callable(cleanup) else None,
        )
        self._join_refresh_job = job

        def succeeded(view: ChannelsView, state: object) -> None:
            view._apply_join_state(
                state if isinstance(state, dict) else None,
                account_id=account_id,
                generation=generation,
            )

        def failed(view: ChannelsView, message: str) -> None:
            if (
                generation == view._account_generation
                and account_id == view._current_account_id()
            ):
                view.join_summary.setText(f"Ошибка статуса: {message}")

        def finished(view: ChannelsView) -> None:
            if view._join_refresh_job is job:
                view._join_refresh_job = None
            if view._join_refresh_pending and view._page_active:
                view._join_refresh_pending = False
                QTimer.singleShot(0, view.request_join_state_refresh)

        connect_lifecycle_safe(
            job,
            self,
            succeeded=succeeded,
            failed=failed,
            finished=finished,
        )
        QThreadPool.globalInstance().start(job)

    def refresh_join_state(self):
        account_id = self._current_account_id()
        generation = self._account_generation
        try:
            try:
                state = self.adapter.get_join_campaign_state(account_id=account_id)
            except TypeError:
                state = self.adapter.get_join_campaign_state()
        except Exception as exc:
            self.join_summary.setText(f"Ошибка статуса: {exc}")
            return
        self._apply_join_state(
            state if isinstance(state, dict) else None,
            account_id=account_id,
            generation=generation,
        )

    def _apply_join_state(
        self,
        state: dict | None,
        *,
        account_id: int | None = None,
        generation: int | None = None,
    ) -> bool:
        owner_account_id = (
            self._current_account_id() if account_id is None else max(0, int(account_id))
        )
        owner_generation = (
            self._account_generation if generation is None else int(generation)
        )
        if (
            owner_account_id != self._current_account_id()
            or owner_generation != self._account_generation
        ):
            self._join_refresh_pending = True
            return False
        if state and int(state.get("account_id") or owner_account_id) != owner_account_id:
            self._join_refresh_pending = True
            return False
        if not state:
            self.join_summary.setText("Кампания вступлений не запущена")
            self.pause_join_button.setText("Пауза")
            self.pause_join_button.setEnabled(False)
            self.stop_join_button.setEnabled(False)
            return True
        raw_status = str(state.get("status") or "")
        status = {
            "running": "Активна",
            "paused": "Пауза",
            "network_wait": "Ожидание сети",
            "completed": "Завершена",
            "stopped": "Остановлена",
        }.get(raw_status, raw_status)
        self.join_summary.setText(
            f"{status} · обработано {state.get('attempted_count', 0)}/{state.get('total_count', 0)} · "
            f"вступлений {state.get('joined_count', 0)} · следующее: {state.get('next_scheduled_display') or '—'}"
        )
        self.pause_join_button.setText(
            "Продолжить" if state.get("status") == "paused" else "Пауза"
        )
        campaign_active = raw_status in {"running", "paused", "network_wait"}
        self.pause_join_button.setEnabled(campaign_active)
        self.stop_join_button.setEnabled(campaign_active)
        return True

    @staticmethod
    def _normalise_channel_rows(
        result: tuple[str, list[dict]],
    ) -> tuple[str, list[tuple[str, str, str, str, str, int]]]:
        source, items = result
        rows: list[tuple[str, str, str, str, str, int]] = []
        if source == "saved":
            for item in items:
                peer_id = int(item.get("peer_id") or 0)
                membership_status = str(item.get("membership_status") or "")
                membership = {"member": "Состоит", "failed": "Ошибка"}.get(
                    membership_status, "Не проверено"
                )
                rows.append(
                    (
                        str(item.get("title") or "Без названия"),
                        f"@{item['username']}" if item.get("username") else "—",
                        str(item.get("kind") or "—"),
                        str(peer_id or ""),
                        membership,
                        peer_id,
                    )
                )
        else:
            for item in items:
                peer_id = int(item.get("channel_id") or 0)
                rows.append(
                    (
                        str(item.get("title") or "Без названия"),
                        f"@{item['username']}" if item.get("username") else "—",
                        "channel",
                        str(peer_id or ""),
                        "Рабочая база",
                        peer_id,
                    )
                )
        return source, rows

    def _fetch_channel_rows(
        self, account_id: int | None = None
    ) -> tuple[str, list[dict]]:
        owner_account_id = (
            self._current_account_id() if account_id is None else max(0, int(account_id))
        )
        try:
            saved = self.adapter.get_saved_dialogs(account_id=owner_account_id) or []
        except TypeError:
            saved = self.adapter.get_saved_dialogs() or []
        if saved:
            return "saved", list(saved)
        try:
            channels = self.adapter.get_channels(account_id=owner_account_id) or []
        except TypeError:
            channels = self.adapter.get_channels() or []
        return "channels", list(channels)

    def load_channels(self):
        """Load SQLite data off the GUI thread and swap one lightweight model."""

        self._load_generation += 1
        generation = self._load_generation
        account_generation = self._account_generation
        account_id = self._current_account_id()
        if self._load_job is not None:
            self._reload_requested = True
            return

        self.summary.setText("Загрузка каналов…")
        cleanup = getattr(self.adapter, "close_thread_connection", None)
        job = BackgroundCall(
            lambda: self._fetch_channel_rows(account_id),
            cleanup=cleanup if callable(cleanup) else None,
        )
        self._load_job = job

        def succeeded(view: ChannelsView, result: object) -> None:
            if (
                generation != view._load_generation
                or account_generation != view._account_generation
                or account_id != view._current_account_id()
            ):
                view._reload_requested = True
                return
            try:
                source, rows = view._normalise_channel_rows(result)  # type: ignore[arg-type]
            except Exception as exc:
                view.channel_model.replace_rows([])
                view.summary.setText(f"Не удалось обработать список каналов: {exc}")
                return
            view.channel_model.replace_rows(rows)
            if source == "saved":
                view.summary.setText(f"Сохранено каналов и групп: {len(rows)}")
            else:
                view.summary.setText(
                    f"В рабочей базе: {len(rows)}"
                    if rows
                    else "Каналы ещё не загружены"
                )

        def failed(view: ChannelsView, message: str) -> None:
            if (
                generation != view._load_generation
                or account_generation != view._account_generation
                or account_id != view._current_account_id()
            ):
                view._reload_requested = True
                return
            view.channel_model.replace_rows([])
            view.summary.setText(f"Не удалось загрузить каналы: {message}")

        def finished(view: ChannelsView) -> None:
            if view._load_job is job:
                view._load_job = None
            reload_requested = (
                view._reload_requested or generation != view._load_generation
            )
            view._reload_requested = False
            if reload_requested and view._page_active:
                QTimer.singleShot(0, view.load_channels)

        connect_lifecycle_safe(
            job,
            self,
            succeeded=succeeded,
            failed=failed,
            finished=finished,
        )
        QThreadPool.globalInstance().start(job)

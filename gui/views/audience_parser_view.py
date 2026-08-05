from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths, QThreadPool, QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.version import APP_NAME
from gui.background import BackgroundCall, connect_lifecycle_safe
from gui.views.common import TaskWatcher
from services.audience_parser import build_audience_export_filename


class AudienceParserView(QWidget):
    """Aurora page for exporting visible group members' usernames to TXT."""

    def __init__(self, adapter, parent=None):
        super().__init__(parent)
        self.adapter = adapter
        self._account_id = 0
        self._account_generation = 0
        self._load_generation = 0
        self._load_job: BackgroundCall | None = None
        self._reload_requested = False
        self._page_active = False
        self._compact_mode = False
        self._source_guard = False
        self.current_task_id: int | None = None
        self.current_mode = ""
        self.output_path: Path | None = None

        self.watcher = TaskWatcher(adapter, self)
        self.watcher.changed.connect(self._task_changed)
        self.watcher.completed.connect(self._task_finished)
        self.watcher.failed.connect(self._watch_failed)

        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(32, 28, 32, 32)
        self._root_layout.setSpacing(18)

        title = QLabel("Парсинг аудитории")
        title.setObjectName("pageTitle")
        title.setWordWrap(True)
        self._root_layout.addWidget(title)

        subtitle = QLabel(
            "Соберите уникальные @username участников одной доступной группы. "
            "Каналы, удалённые аккаунты, боты и пользователи без username не экспортируются."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        self._root_layout.addWidget(subtitle)

        account_card = QFrame()
        account_card.setObjectName("infoCard")
        account_layout = QVBoxLayout(account_card)
        account_layout.setContentsMargins(22, 18, 22, 18)
        account_layout.setSpacing(7)
        account_title = QLabel("Активный аккаунт")
        account_title.setObjectName("cardTitle")
        self.account_label = QLabel("Telegram-аккаунт не выбран")
        self.account_label.setObjectName("mutedText")
        self.account_label.setWordWrap(True)
        account_layout.addWidget(account_title)
        account_layout.addWidget(self.account_label)
        self._root_layout.addWidget(account_card)

        source_card = QFrame()
        source_card.setObjectName("card")
        source_layout = QVBoxLayout(source_card)
        source_layout.setContentsMargins(22, 20, 22, 22)
        source_layout.setSpacing(12)

        source_title = QLabel("Группа для парсинга")
        source_title.setObjectName("cardTitle")
        source_layout.addWidget(source_title)

        source_hint = QLabel(
            "Выберите одну группу из сохранённого списка либо вставьте ссылку / @username. "
            "Обычные каналы в список не попадают."
        )
        source_hint.setObjectName("mutedText")
        source_hint.setWordWrap(True)
        source_layout.addWidget(source_hint)

        self.group_combo = QComboBox()
        self.group_combo.setMinimumContentsLength(24)
        self.group_combo.addItem("Группы ещё не загружены", None)
        self.group_combo.currentIndexChanged.connect(self._group_selected)
        source_layout.addWidget(self.group_combo)

        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText("https://t.me/groupname или @groupname")
        self.link_input.textChanged.connect(self._link_changed)
        source_layout.addWidget(self.link_input)

        source_actions = QHBoxLayout()
        source_actions.setSpacing(10)
        self.load_groups_button = QPushButton("Загрузить мои группы")
        self.load_groups_button.setObjectName("secondaryButton")
        self.load_groups_button.clicked.connect(self._start_group_sync)
        source_actions.addWidget(self.load_groups_button)
        source_actions.addStretch(1)
        source_layout.addLayout(source_actions)

        self.groups_status = QLabel("Список групп выбранного аккаунта ещё не проверен")
        self.groups_status.setObjectName("mutedText")
        self.groups_status.setWordWrap(True)
        source_layout.addWidget(self.groups_status)
        self._root_layout.addWidget(source_card)

        result_card = QFrame()
        result_card.setObjectName("card")
        result_layout = QVBoxLayout(result_card)
        result_layout.setContentsMargins(22, 20, 22, 22)
        result_layout.setSpacing(12)

        result_title = QLabel("Экспорт")
        result_title.setObjectName("cardTitle")
        result_layout.addWidget(result_title)

        self.summary = QLabel("Парсинг не запущен")
        self.summary.setObjectName("statusTitle")
        self.summary.setWordWrap(True)
        result_layout.addWidget(self.summary)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        result_layout.addWidget(self.progress)

        self.actions_layout = QGridLayout()
        self.actions_layout.setHorizontalSpacing(10)
        self.actions_layout.setVerticalSpacing(10)
        self.start_button = QPushButton("Начать парсинг")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_parsing)
        self.stop_button = QPushButton("Остановить")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_parsing)
        self.open_folder_button = QPushButton("Открыть папку")
        self.open_folder_button.setObjectName("secondaryButton")
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.clicked.connect(self.open_output_folder)
        self._arrange_actions(False)
        result_layout.addLayout(self.actions_layout)

        self.output_label = QLabel("TXT-файл будет выбран перед запуском")
        self.output_label.setObjectName("mutedText")
        self.output_label.setWordWrap(True)
        result_layout.addWidget(self.output_label)
        self._root_layout.addWidget(result_card)
        self._root_layout.addStretch(1)

        self.handle_account_changed()

    def _current_account_id(self) -> int:
        try:
            return max(0, int(self.adapter.get_current_account_id() or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _task_account_id(task: dict[str, Any]) -> int:
        payload = task.get("payload") or {}
        try:
            return max(
                0,
                int(
                    task.get("account_id")
                    or (payload.get("account_id") if isinstance(payload, dict) else 0)
                    or 0
                ),
            )
        except (TypeError, ValueError, OverflowError):
            return 0

    def _account_caption(self, account_id: int) -> str:
        if account_id <= 0:
            return "Telegram-аккаунт не выбран"
        try:
            rows = self.adapter.list_telegram_accounts() or []
        except Exception:
            rows = []
        for row in rows:
            try:
                row_id = int(row.get("telegram_account_id") or row.get("id") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if row_id != account_id:
                continue
            name = str(
                row.get("display_name")
                or row.get("account_name")
                or row.get("username")
                or row.get("phone")
                or f"ID {account_id}"
            ).strip()
            username = str(row.get("username") or "").strip().lstrip("@")
            return f"{name} · @{username}" if username and f"@{username}" not in name else name
        return f"Telegram ID {account_id}"

    def handle_account_changed(self) -> None:
        self._account_generation += 1
        self._account_id = self._current_account_id()
        self._load_generation += 1
        self._reload_requested = self._load_job is not None
        self.watcher.stop()
        self.current_task_id = None
        self.current_mode = ""
        self.output_path = None
        self.progress.setValue(0)
        self.stop_button.setEnabled(False)
        self.open_folder_button.setEnabled(False)
        self.account_label.setText(self._account_caption(self._account_id))
        self.link_input.clear()
        self._replace_groups([], loaded=False, syncing=False)
        self.summary.setText(
            "Парсинг не запущен"
            if self._account_id > 0
            else "Сначала выберите аккаунт на странице «Аккаунт»"
        )
        self._set_work_enabled(self._account_id > 0)
        if self._page_active and self._account_id > 0:
            self.load_cached_groups()

    def set_page_active(self, active: bool) -> None:
        self._page_active = bool(active)
        self.watcher.set_active(self._page_active)
        if self._account_id != self._current_account_id():
            self.handle_account_changed()
        elif self._page_active and self._account_id > 0:
            self.load_cached_groups()

    def _set_work_enabled(self, enabled: bool) -> None:
        idle = bool(enabled and self.current_task_id is None)
        self.group_combo.setEnabled(idle)
        self.link_input.setEnabled(idle)
        self.start_button.setEnabled(idle)
        if not enabled:
            self.load_groups_button.setEnabled(False)

    @staticmethod
    def _normalize_cached_groups(result: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
        loaded = bool(result.get("loaded"))
        saved = result.get("saved") or []
        working = result.get("working") or []
        groups: list[dict[str, Any]] = []
        seen: set[int] = set()

        for item in saved:
            kind = str(item.get("kind") or "").strip().lower()
            membership = str(item.get("membership_status") or "").strip().lower()
            if kind not in {"group", "supergroup"} or membership == "left":
                continue
            peer_id = int(item.get("peer_id") or 0)
            if not peer_id or peer_id in seen:
                continue
            seen.add(peer_id)
            groups.append(
                {
                    "peer_id": peer_id,
                    "title": str(item.get("title") or "Без названия"),
                    "username": str(item.get("username") or ""),
                    "access_hash": item.get("access_hash"),
                    "peer_type": str(item.get("peer_type") or "channel"),
                }
            )

        if not groups:
            for item in working:
                if str(item.get("target_kind") or "").strip().lower() != "group":
                    continue
                peer_id = int(item.get("channel_id") or 0)
                if not peer_id or peer_id in seen:
                    continue
                seen.add(peer_id)
                groups.append(
                    {
                        "peer_id": peer_id,
                        "title": str(item.get("title") or "Без названия"),
                        "username": str(item.get("username") or ""),
                        "access_hash": item.get("access_hash"),
                        "peer_type": str(item.get("peer_type") or "channel"),
                    }
                )
        groups.sort(key=lambda item: item["title"].casefold())
        return loaded, groups

    def _fetch_cached_groups(self, account_id: int) -> dict[str, Any]:
        try:
            saved = self.adapter.get_saved_dialogs(account_id=account_id) or []
        except TypeError:
            saved = self.adapter.get_saved_dialogs() or []
        try:
            working = self.adapter.get_channels(account_id=account_id) or []
        except TypeError:
            working = self.adapter.get_channels() or []
        completed_sync = False
        active_sync_task = None
        try:
            tasks = self.adapter.get_tasks() or []
        except Exception:
            tasks = []
        for task in tasks:
            if str(task.get("type") or "") not in {"sync_channels", "sync_saved_dialogs"}:
                continue
            if self._task_account_id(task) != account_id:
                continue
            status = str(task.get("status") or "")
            if status == "completed":
                completed_sync = True
            elif status in {"pending", "running", "processing"} and active_sync_task is None:
                active_sync_task = dict(task)
        return {
            "loaded": bool(saved or working or completed_sync),
            "saved": list(saved),
            "working": list(working),
            "active_sync_task": active_sync_task,
        }

    def load_cached_groups(self) -> None:
        self._load_generation += 1
        generation = self._load_generation
        account_generation = self._account_generation
        account_id = self._current_account_id()
        if account_id <= 0:
            return
        if self._load_job is not None:
            self._reload_requested = True
            return

        self.groups_status.setText("Проверяем сохранённый список групп…")
        cleanup = getattr(self.adapter, "close_thread_connection", None)
        job = BackgroundCall(
            lambda: self._fetch_cached_groups(account_id),
            cleanup=cleanup if callable(cleanup) else None,
        )
        self._load_job = job

        def succeeded(view: AudienceParserView, result: object) -> None:
            if (
                generation != view._load_generation
                or account_generation != view._account_generation
                or account_id != view._current_account_id()
            ):
                view._reload_requested = True
                return
            result_map = result if isinstance(result, dict) else {}
            loaded, groups = view._normalize_cached_groups(result_map)
            active_sync = result_map.get("active_sync_task")
            syncing = isinstance(active_sync, dict)
            view._replace_groups(groups, loaded=loaded, syncing=syncing)
            if isinstance(active_sync, dict) and view.current_task_id is None:
                view.current_task_id = int(active_sync.get("id") or 0) or None
                if view.current_task_id is not None:
                    view.current_mode = "sync"
                    view.stop_button.setEnabled(False)
                    view._set_work_enabled(False)
                    view.watcher.watch(view.current_task_id)

        def failed(view: AudienceParserView, message: str) -> None:
            if account_id == view._current_account_id():
                view.groups_status.setText(f"Не удалось загрузить список групп: {message}")
                view.load_groups_button.setEnabled(view.current_task_id is None)

        def finished(view: AudienceParserView) -> None:
            if view._load_job is job:
                view._load_job = None
            reload_requested = view._reload_requested or generation != view._load_generation
            view._reload_requested = False
            if reload_requested and view._page_active:
                QTimer.singleShot(0, view.load_cached_groups)

        connect_lifecycle_safe(
            job,
            self,
            succeeded=succeeded,
            failed=failed,
            finished=finished,
        )
        QThreadPool.globalInstance().start(job)

    def _replace_groups(
        self, groups: list[dict[str, Any]], *, loaded: bool, syncing: bool
    ) -> None:
        self._source_guard = True
        try:
            self.group_combo.clear()
            if groups:
                self.group_combo.addItem("Выберите одну группу", None)
                for item in groups:
                    username = str(item.get("username") or "").strip().lstrip("@")
                    suffix = f" · @{username}" if username else ""
                    self.group_combo.addItem(f"{item['title']}{suffix}", item)
            else:
                self.group_combo.addItem(
                    "В сохранённом списке нет доступных групп"
                    if loaded
                    else "Группы ещё не загружены",
                    None,
                )
        finally:
            self._source_guard = False
        self.load_groups_button.setEnabled(
            self._account_id > 0
            and not loaded
            and not syncing
            and self.current_task_id is None
        )
        if syncing:
            self.groups_status.setText(
                "Список каналов и групп уже загружается во вкладке «Каналы»…"
            )
        elif loaded:
            self.groups_status.setText(
                f"Доступно групп: {len(groups)}. Список уже получен во вкладке «Каналы»."
                if groups
                else "Список во вкладке «Каналы» уже получен, но доступных групп в нём нет."
            )
        else:
            self.groups_status.setText(
                "Список ещё не получен. Нажмите «Загрузить мои группы»."
            )

    def _group_selected(self, index: int) -> None:
        if self._source_guard or index <= 0:
            return
        if self.group_combo.itemData(index) is None:
            return
        self._source_guard = True
        try:
            self.link_input.clear()
        finally:
            self._source_guard = False

    def _link_changed(self, text: str) -> None:
        if self._source_guard or not text.strip():
            return
        self._source_guard = True
        try:
            self.group_combo.setCurrentIndex(0)
        finally:
            self._source_guard = False

    def _start_group_sync(self) -> None:
        if self.current_task_id is not None:
            return
        self._start_task("sync_channels", {}, mode="sync")

    def _selected_source(self) -> tuple[dict[str, Any] | None, str]:
        link = self.link_input.text().strip()
        if link:
            return {"link": link}, link.lstrip("@").rsplit("/", 1)[-1] or "group"
        data = self.group_combo.currentData(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict):
            return dict(data), str(data.get("title") or "group")
        return None, ""

    def start_parsing(self) -> None:
        if self.current_task_id is not None:
            return
        if self._current_account_id() <= 0:
            QMessageBox.information(
                self, APP_NAME, "Сначала выберите аккаунт на странице «Аккаунт»."
            )
            return
        source, title = self._selected_source()
        if source is None:
            QMessageBox.information(
                self, APP_NAME, "Выберите одну группу или вставьте ссылку на неё."
            )
            return

        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        initial = str(Path(documents or str(Path.home())) / build_audience_export_filename(title))
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Сохранить аудиторию",
            initial,
            "Текстовый файл (*.txt)",
        )
        if not selected:
            return
        output_path = Path(selected)
        if output_path.suffix.lower() != ".txt":
            output_path = output_path.with_suffix(".txt")
        self.output_path = output_path
        self.output_label.setText(str(output_path))
        self.open_folder_button.setEnabled(False)
        self._start_task(
            "parse_audience",
            {
                "source": source,
                "source_title": title,
                "output_path": str(output_path),
            },
            mode="parse",
        )

    def _start_task(self, task_type: str, payload: dict[str, Any], *, mode: str) -> None:
        try:
            task = self.adapter.create_task(task_type, payload)
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.current_task_id = int(task["id"])
        self.current_mode = mode
        self.progress.setValue(0)
        self.summary.setText(
            "Подключаемся к Telegram…"
            if mode == "parse"
            else "Получаем список каналов и групп…"
        )
        self.stop_button.setEnabled(mode == "parse")
        self._set_work_enabled(False)
        self.load_groups_button.setEnabled(False)
        self.watcher.watch(self.current_task_id)
        if not self.adapter.start_queue():
            self.adapter.cancel_task(self.current_task_id)
            self.watcher.stop()
            self.current_task_id = None
            self.current_mode = ""
            self.stop_button.setEnabled(False)
            self._set_work_enabled(self._account_id > 0)
            QMessageBox.warning(
                self, APP_NAME, self.adapter.get_queue_unavailable_message()
            )

    def stop_parsing(self) -> None:
        task_id = self.current_task_id
        if task_id is None or self.current_mode != "parse":
            return
        self.stop_button.setEnabled(False)
        try:
            stopped = bool(self.adapter.cancel_task(task_id))
        except Exception as exc:
            self.stop_button.setEnabled(True)
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.summary.setText(
            "Останавливаем парсинг…"
            if stopped
            else "Задача уже завершена или остановлена"
        )

    def _task_changed(self, task: dict[str, Any]) -> None:
        if self._task_account_id(task) != self._current_account_id():
            return
        if self.current_task_id is None or int(task.get("id") or 0) != self.current_task_id:
            return
        self.progress.setValue(max(0, min(100, int(task.get("progress") or 0))))
        status_text = str(task.get("status_text") or "").strip()
        if status_text:
            self.summary.setText(status_text)

    def _task_finished(self, task: dict[str, Any]) -> None:
        if self._task_account_id(task) != self._current_account_id():
            return
        if self.current_task_id is None or int(task.get("id") or 0) != self.current_task_id:
            return
        mode = self.current_mode
        self.current_task_id = None
        self.current_mode = ""
        self.stop_button.setEnabled(False)
        self._set_work_enabled(self._account_id > 0)

        status = str(task.get("status") or "")
        if mode == "sync":
            self.summary.setText(
                str(task.get("status_text") or "Список групп обновлён")
                if status == "completed"
                else "Не удалось обновить список групп"
            )
            self.load_cached_groups()
            if status == "failed":
                QMessageBox.warning(
                    self, APP_NAME, str(task.get("error") or "Неизвестная ошибка")
                )
            return

        if status == "completed":
            self.progress.setValue(100)
            self.summary.setText(str(task.get("status_text") or "Парсинг завершён"))
            self.open_folder_button.setEnabled(
                self.output_path is not None and self.output_path.exists()
            )
        elif status == "cancelled":
            self.summary.setText("Парсинг остановлен. Итоговый TXT-файл не создан.")
            self.open_folder_button.setEnabled(False)
        else:
            self.summary.setText("Парсинг завершился ошибкой")
            self.open_folder_button.setEnabled(False)
            QMessageBox.warning(
                self, APP_NAME, str(task.get("error") or "Неизвестная ошибка")
            )
        self.load_cached_groups()

    def _watch_failed(self, message: str) -> None:
        self.summary.setText(f"Не удалось получить состояние задачи: {message}")

    def open_output_folder(self) -> None:
        path = self.output_path
        if path is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def _arrange_actions(self, compact: bool) -> None:
        buttons = (self.start_button, self.stop_button, self.open_folder_button)
        while self.actions_layout.count():
            item = self.actions_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(self)
        if compact:
            for row, button in enumerate(buttons):
                self.actions_layout.addWidget(button, row, 0)
        else:
            for column, button in enumerate(buttons):
                self.actions_layout.addWidget(button, 0, column)
            self.actions_layout.setColumnStretch(len(buttons), 1)

    def set_compact_mode(self, compact: bool) -> None:
        self._compact_mode = bool(compact)
        margin = 18 if self._compact_mode else 32
        self._root_layout.setContentsMargins(margin, 22, margin, 24)
        self._arrange_actions(self._compact_mode)

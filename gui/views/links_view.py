from __future__ import annotations

import math
from typing import Iterable, cast

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QThreadPool,
    Qt,
    QTimer,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QGridLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from core.campaign_schedule import from_db_time, utc_now
from core.version import APP_NAME
from gui.background import BackgroundCall, connect_lifecycle_safe
from gui.views.common import TaskWatcher


LINK_DELAY_MIN_SECONDS = 10
LINK_DELAY_MAX_SECONDS = 200
LINK_DELAY_DEFAULT_SECONDS = 135
LINK_DELAY_SETTING_PREFIX = "automation.link_check_delay_"
LINK_DELAY_TARGET_KEY = f"{LINK_DELAY_SETTING_PREFIX}target_seconds"
LINK_DELAY_MIN_KEY = f"{LINK_DELAY_SETTING_PREFIX}min_seconds"
LINK_DELAY_MAX_KEY = f"{LINK_DELAY_SETTING_PREFIX}max_seconds"


def _link_delay_bounds(selected_seconds: int) -> tuple[int, int]:
    target = max(
        LINK_DELAY_MIN_SECONDS,
        min(LINK_DELAY_MAX_SECONDS, int(selected_seconds)),
    )
    if target <= LINK_DELAY_MIN_SECONDS + 5:
        return LINK_DELAY_MIN_SECONDS, LINK_DELAY_MIN_SECONDS + 5
    spread = max(5, math.ceil(target * 0.11))
    return max(LINK_DELAY_MIN_SECONDS, target - spread), target


class LinkTableModel(QAbstractTableModel):
    HEADERS = ("Канал", "ID канала", "Чат обсуждения", "Статус")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[str, str, str, str]] = []

    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),
    ) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),
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
        if (
            role == int(Qt.ItemDataRole.TextAlignmentRole)
            and index.column() == 1
        ):
            return Qt.AlignmentFlag.AlignCenter
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

    def replace_rows(self, rows: list[tuple[str, str, str, str]]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()


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
        self._account_generation = 0
        self._load_generation = 0
        self._load_job: BackgroundCall | None = None
        self._reload_requested = False
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
            "прямого сообщения. Между проверками выдерживается случайная пауза из "
            "диапазона, выбранного ползунком рядом со «Стоп». Ползунок можно менять во "
            "время работы: новое значение применяется к следующей паузе между связками, "
            "но не обрывает уже начатое ожидание. Между новыми вступлениями — 45–70 "
            "секунд. Каждый объект проверяется один раз. Кнопка «Стоп» не сокращает "
            "FloodWait: задача дождётся Telegram-таймера и защитного буфера, затем "
            "продолжит или останется на паузе до продолжения."
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

        self._pending_link_delay_account_id = 0
        self._pending_link_delay_payload: dict[str, int] | None = None
        self._link_delay_restoring = False
        self._link_delay_save_timer = QTimer(self)
        self._link_delay_save_timer.setSingleShot(True)
        self._link_delay_save_timer.setInterval(300)
        self._link_delay_save_timer.timeout.connect(self._persist_link_delay_setting)

        self.link_delay_slider = QSlider(Qt.Orientation.Horizontal)
        self.link_delay_slider.setObjectName("linkDelaySlider")
        self.link_delay_slider.setRange(LINK_DELAY_MIN_SECONDS, LINK_DELAY_MAX_SECONDS)
        self.link_delay_slider.setSingleStep(1)
        self.link_delay_slider.setPageStep(10)
        self.link_delay_slider.setValue(LINK_DELAY_DEFAULT_SECONDS)
        self.link_delay_slider.setFixedWidth(150)
        self.link_delay_slider.setAccessibleName("Пауза между связками")
        self.link_delay_slider.setStyleSheet(
            """
            QSlider#linkDelaySlider::groove:horizontal {
                height: 4px;
                background: #30343C;
                border-radius: 2px;
            }
            QSlider#linkDelaySlider::sub-page:horizontal {
                background: #7F93B8;
                border-radius: 2px;
            }
            QSlider#linkDelaySlider::add-page:horizontal {
                background: #242832;
                border-radius: 2px;
            }
            QSlider#linkDelaySlider::handle:horizontal {
                width: 14px;
                margin: -5px 0;
                border-radius: 7px;
                background: #F7FAFD;
                border: 1px solid #6E84AD;
            }
            QSlider#linkDelaySlider:disabled::handle:horizontal {
                background: #656B75;
                border-color: #3A3F49;
            }
            """
        )
        self.link_delay_value = QLabel()
        self.link_delay_value.setObjectName("mutedText")
        self.link_delay_value.setMinimumWidth(82)
        self.link_delay_slider.valueChanged.connect(self._link_delay_changed)

        self.status = QLabel("Готово к проверке")
        self.status.setObjectName("statusTitle")
        self.buttons_layout = QGridLayout()
        self.buttons_layout.addWidget(self.link_button, 0, 0)
        self.buttons_layout.addWidget(self.stop_button, 0, 1)
        self.buttons_layout.addWidget(self.link_delay_slider, 0, 2)
        self.buttons_layout.addWidget(self.link_delay_value, 0, 3)
        self.buttons_layout.setColumnStretch(4, 1)
        self.buttons_layout.addWidget(self.status, 0, 5)
        self._load_link_delay_setting()

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        self.table = QTableView()
        self.link_model = LinkTableModel(self.table)
        self.table.setModel(self.link_model)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
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

    def _configured_link_delay_range(self) -> tuple[int, int]:
        config = getattr(getattr(self.adapter, "api", None), "config", None)
        try:
            low = int(round(float(getattr(config, "link_check_delay_min_seconds", 105))))
            high = int(round(float(getattr(config, "link_check_delay_max_seconds", 135))))
        except (TypeError, ValueError, OverflowError):
            return 105, 135
        low = max(0, low)
        return low, max(low, high)

    def _set_link_delay_display(self, low: int, high: int) -> None:
        self.link_delay_value.setText(f"{low}–{high} сек")
        self.link_delay_slider.setToolTip(
            "Случайная пауза между проверками связок: "
            f"{low}–{high} секунд. Изменение применяется к следующей паузе; "
            "FloodWait и задержки вступления не меняются."
        )

    def _link_delay_changed(self, selected_seconds: int) -> None:
        low, high = _link_delay_bounds(selected_seconds)
        self._set_link_delay_display(low, high)
        if self._link_delay_restoring:
            return
        account_id = self._current_account_id()
        if account_id <= 0:
            return
        self._pending_link_delay_account_id = account_id
        self._pending_link_delay_payload = {
            LINK_DELAY_TARGET_KEY: int(selected_seconds),
            LINK_DELAY_MIN_KEY: low,
            LINK_DELAY_MAX_KEY: high,
        }
        self._link_delay_save_timer.start()

    @staticmethod
    def _valid_saved_link_delay(values: dict) -> tuple[int, int, int] | None:
        try:
            target = int(values[LINK_DELAY_TARGET_KEY])
            low = int(values[LINK_DELAY_MIN_KEY])
            high = int(values[LINK_DELAY_MAX_KEY])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if not (
            LINK_DELAY_MIN_SECONDS <= target <= LINK_DELAY_MAX_SECONDS
            and LINK_DELAY_MIN_SECONDS <= low <= high <= LINK_DELAY_MAX_SECONDS
        ):
            return None
        return target, low, high

    def _load_link_delay_setting(self) -> None:
        account_id = self._current_account_id()
        self.link_delay_slider.setEnabled(account_id > 0)
        if account_id <= 0:
            low, high = self._configured_link_delay_range()
            self._set_link_delay_display(low, high)
            return
        try:
            values = self.adapter.get_settings(LINK_DELAY_SETTING_PREFIX) or {}
        except Exception:
            values = {}
        saved = self._valid_saved_link_delay(values) if isinstance(values, dict) else None
        self._link_delay_restoring = True
        self.link_delay_slider.blockSignals(True)
        try:
            if saved is None:
                target = LINK_DELAY_DEFAULT_SECONDS
                low, high = self._configured_link_delay_range()
            else:
                target, low, high = saved
            self.link_delay_slider.setValue(target)
            self._set_link_delay_display(low, high)
        finally:
            self.link_delay_slider.blockSignals(False)
            self._link_delay_restoring = False

    def _persist_link_delay_setting(self) -> None:
        account_id = int(self._pending_link_delay_account_id or 0)
        payload = self._pending_link_delay_payload
        self._pending_link_delay_account_id = 0
        self._pending_link_delay_payload = None
        if account_id <= 0 or not payload:
            return
        try:
            self.adapter.save_account_settings(payload, account_id=account_id)
        except Exception as exc:
            if account_id == self._current_account_id():
                low = int(payload[LINK_DELAY_MIN_KEY])
                high = int(payload[LINK_DELAY_MAX_KEY])
                self.link_delay_value.setText(f"{low}–{high} сек ⚠")
                self.link_delay_slider.setToolTip(
                    f"Не удалось сохранить паузу: {exc}. "
                    "До успешного сохранения worker использует предыдущее значение."
                )

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

        self._account_generation += 1
        self._load_generation += 1
        self._reload_requested = self._load_job is not None
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
        self._load_link_delay_setting()
        self.status.setText(
            "Готово к проверке"
            if self._account_id > 0
            else "Telegram-аккаунт не подключён"
        )
        self.link_model.replace_rows([])
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
        else:
            self._load_generation += 1

    def set_compact_mode(self, compact: bool) -> None:
        self.buttons_layout.removeWidget(self.link_button)
        self.buttons_layout.removeWidget(self.stop_button)
        self.buttons_layout.removeWidget(self.link_delay_slider)
        self.buttons_layout.removeWidget(self.link_delay_value)
        self.buttons_layout.removeWidget(self.status)
        self.buttons_layout.setColumnStretch(4, 0 if compact else 1)
        if compact:
            self.buttons_layout.addWidget(self.link_button, 0, 0)
            self.buttons_layout.addWidget(self.stop_button, 0, 1)
            self.buttons_layout.addWidget(self.link_delay_slider, 1, 0)
            self.buttons_layout.addWidget(self.link_delay_value, 1, 1)
            self.buttons_layout.addWidget(self.status, 2, 0, 1, 2)
            self.table.setColumnHidden(1, True)
        else:
            self.buttons_layout.addWidget(self.link_button, 0, 0)
            self.buttons_layout.addWidget(self.stop_button, 0, 1)
            self.buttons_layout.addWidget(self.link_delay_slider, 0, 2)
            self.buttons_layout.addWidget(self.link_delay_value, 0, 3)
            self.buttons_layout.addWidget(self.status, 0, 5)
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

    def _fetch_link_rows(
        self,
        account_id: int,
    ) -> list[tuple[str, str, str, str]]:
        try:
            channels = self.adapter.get_channels(account_id=account_id) or []
        except TypeError:
            channels = self.adapter.get_channels() or []

        rows: list[tuple[str, str, str, str]] = []
        for channel in channels:
            if str(channel.get("target_kind") or "channel") != "channel":
                continue
            discussion = channel.get("linked_chat_title") or (
                str(channel.get("linked_chat_id"))
                if channel.get("linked_chat_id")
                else "—"
            )
            rows.append(
                (
                    str(channel.get("title") or "Без названия"),
                    str(channel.get("channel_id") or ""),
                    str(discussion),
                    str(channel.get("link_status") or "Не проверено"),
                )
            )
        return rows

    def load_channels(self):
        self._load_generation += 1
        generation = self._load_generation
        account_generation = self._account_generation
        account_id = self._current_account_id()
        if self._load_job is not None:
            self._reload_requested = True
            return
        self._reload_requested = False
        if account_id <= 0:
            self.link_model.replace_rows([])
            return

        cleanup = getattr(self.adapter, "close_thread_connection", None)
        job = BackgroundCall(
            lambda: self._fetch_link_rows(account_id),
            cleanup=cleanup if callable(cleanup) else None,
        )
        self._load_job = job

        def succeeded(view: LinksView, value: object) -> None:
            if (
                generation != view._load_generation
                or account_generation != view._account_generation
                or account_id != view._current_account_id()
                or not view._page_active
            ):
                view._reload_requested = True
                return
            rows = list(
                cast(Iterable[tuple[str, str, str, str]], value or [])
            )
            view.link_model.replace_rows(rows)

        def failed(view: LinksView, message: str) -> None:
            if (
                generation == view._load_generation
                and account_generation == view._account_generation
                and account_id == view._current_account_id()
                and view._page_active
            ):
                view.status.setText(f"Не удалось загрузить каналы: {message}")

        def finished(view: LinksView) -> None:
            if view._load_job is job:
                view._load_job = None
            rerun = (
                view._reload_requested
                or generation != view._load_generation
            )
            view._reload_requested = False
            if rerun and view._page_active:
                QTimer.singleShot(0, view.load_channels)

        connect_lifecycle_safe(
            job,
            self,
            succeeded=succeeded,
            failed=failed,
            finished=finished,
        )
        QThreadPool.globalInstance().start(job)

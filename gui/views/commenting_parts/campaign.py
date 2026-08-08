from __future__ import annotations
from typing import cast
import logging
from PySide6.QtCore import QThreadPool, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from core.version import APP_NAME
from core.openai_settings import SOURCE_OPENAI, SOURCE_PREWRITTEN
from core.config import MAX_COMMENT_VARIANTS
from core.countdown import countdown_label, seconds_until
from core.campaign_schedule import from_db_time
from gui.background import BackgroundCall, connect_lifecycle_safe
from services.observability import (
    campaign_statistics,
    classify_result,
    format_campaign_statistics,
    humanize_reason,
)
log = logging.getLogger(__name__)

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from gui.views.commenting_view import CommentingView

class CommentingCampaignMixin:
    def set_page_active(self, active: bool) -> None:
        self._page_active = bool(active)
        if self._page_active:
            if not self.refresh_timer.isActive():
                self.refresh_timer.start()
            if not self.countdown_timer.isActive():
                self.countdown_timer.start()
            self.load_comments()
            self.load_openai_configuration()
            self.request_campaign_refresh()
        else:
            # Reject a queued result from a page that is no longer the active
            # owner and cancel the network request when possible.
            self._cancel_openai_test()
            self._account_generation += 1
            self._refresh_pending = False
            self.refresh_timer.stop()
            self.countdown_timer.stop()
    def _current_account_id(self) -> int:
        getter = getattr(self.adapter, "get_current_account_id", None)
        if not callable(getter):
            return 0
        try:
            return max(0, int(getter() or 0))
        except (TypeError, ValueError, OverflowError):
            return 0
    def _set_next_check(self, value, *, fallback: str = "Следующая проверка: —") -> None:
        parsed = from_db_time(value)
        key = parsed.isoformat() if parsed is not None else ""
        if key != self._next_check_key:
            self._next_check_due_refresh_requested = False
        self._next_check_at = parsed
        self._next_check_key = key
        self._next_check_fallback = str(fallback or "Следующая проверка: —")
        self._update_next_check_label()
    def _clear_next_check(self, text: str = "Следующая проверка: —") -> None:
        self._next_check_at = None
        self._next_check_key = ""
        self._next_check_due_refresh_requested = False
        self._next_check_fallback = str(text or "Следующая проверка: —")
        self.next_label.setText(self._next_check_fallback)
    def _update_next_check_label(self) -> None:
        target = self._next_check_at
        if target is None:
            self.next_label.setText(self._next_check_fallback)
            return
        self.next_label.setText(
            countdown_label(
                "Следующая проверка",
                target,
                include_deadline=True,
                include_date=True,
            )
        )
        remaining = seconds_until(target)
        if (
            remaining == 0
            and self._page_active
            and self._refresh_job is None
            and not self._next_check_due_refresh_requested
        ):
            self._next_check_due_refresh_requested = True
            self.request_campaign_refresh()
    def handle_account_changed(self) -> None:
        """Invalidate old async reads and synchronously clear the previous owner."""

        self.limit_save_timer.stop()
        self._daily_limit_account_id = None
        self._cancel_openai_test()
        self._account_generation += 1
        self._refresh_pending = self._refresh_job is not None
        account_id = self._current_account_id()
        self._apply_campaign_snapshot(
            {
                "account_id": account_id,
                "generation": self._account_generation,
                "state": None,
                "channels": [],
                "history": [],
            }
        )
        self.load_daily_limit()
        self.load_comments(force=True)
        self.load_openai_configuration()
        self.refresh_campaign()
    def _apply_compact(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        for editor in self.editors:
            editor.setMinimumHeight(62 if compact else 70)
            editor.setMaximumHeight(84 if compact else 92)

        for button in (
            self.start_button,
            self.pause_button,
            self.resume_button,
            self.stop_button,
        ):
            self.controls_grid.removeWidget(button)
        if compact:
            self.controls_grid.addWidget(self.start_button, 0, 0)
            self.controls_grid.addWidget(self.pause_button, 0, 1)
            self.controls_grid.addWidget(self.resume_button, 1, 0)
            self.controls_grid.addWidget(self.stop_button, 1, 1)
            self.controls_grid.setColumnStretch(2, 0)
            self.main_layout.setContentsMargins(18, 20, 18, 20)
        else:
            self.controls_grid.addWidget(self.start_button, 0, 0)
            self.controls_grid.addWidget(self.pause_button, 0, 1)
            self.controls_grid.addWidget(self.resume_button, 0, 2)
            self.controls_grid.addWidget(self.stop_button, 0, 3)
            self.controls_grid.setColumnStretch(4, 1)
            self.main_layout.setContentsMargins(34, 28, 34, 28)

        for widget in (
            self.status,
            self.period_label,
            self.next_label,
            self.count_label,
        ):
            self.campaign_layout.removeWidget(widget)
        if compact:
            self.campaign_layout.addWidget(self.status, 0, 0)
            self.campaign_layout.addWidget(self.period_label, 1, 0)
            self.campaign_layout.addWidget(self.next_label, 2, 0)
            self.campaign_layout.addWidget(self.count_label, 3, 0)
        else:
            self.campaign_layout.addWidget(self.status, 0, 0, 1, 2)
            self.campaign_layout.addWidget(self.period_label, 1, 0)
            self.campaign_layout.addWidget(self.next_label, 1, 1)
            self.campaign_layout.addWidget(self.count_label, 2, 0, 1, 2)
    def _set_save_status(self, text: str, *, saved: bool) -> None:
        self.save_status.setText(text)
        self.save_status.setObjectName(
            "saveStatusSaved" if saved else "saveStatusDirty"
        )
        self.save_status.style().unpolish(self.save_status)
        self.save_status.style().polish(self.save_status)
    def start_campaign(self):
        self.limit_save_timer.stop()
        current = self.adapter.get_comment_campaign_state()
        if current and str(current.get("status") or "") in {
            "running",
            "paused",
            "network_wait",
            "cycle_wait",
        }:
            # A stale enabled button must never act as a manual "refresh" or
            # create a second campaign. Repaint the existing state instead.
            self._apply_campaign_snapshot(self._load_campaign_snapshot())
            return
        account_id = self._current_account_id()
        if account_id <= 0:
            QMessageBox.warning(
                self,
                APP_NAME,
                "Сначала выберите Telegram-аккаунт",
            )
            return
        if self._daily_limit_account_id != account_id:
            self.limit_save_timer.stop()
            self.load_daily_limit()

        daily_limit = int(self.daily_limit_slider.value())
        if daily_limit <= 0:
            QMessageBox.information(
                self,
                "Кампания отключена",
                "Выберите количество комментариев в сутки от 1 до 1000",
            )
            return
        if not self._save_daily_limit(account_id=account_id):
            return
        source = self._current_comment_source()
        all_comments = self._all_comments()
        comments = [text for text in all_comments if text]
        if source == SOURCE_PREWRITTEN and not comments:
            QMessageBox.information(
                self, "Нет комментариев", "Добавьте хотя бы один вариант комментария"
            )
            return
        if source == SOURCE_OPENAI and not self.save_openai_configuration(quiet=True):
            return
        if not self.adapter.get_channels():
            QMessageBox.information(
                self, "Нет каналов", "Сначала получите список каналов"
            )
            return
        if not self.adapter.get_commenting_channels():
            QMessageBox.information(
                self,
                "Нет рабочих целей",
                "Сначала получите каналы и группы, затем выполните проверку во вкладке «Связки»",
            )
            return
        try:
            campaign = self.adapter.start_comment_campaign(
                all_comments,
                continuous=self.continuous.isChecked(),
                daily_limit=daily_limit,
                comment_source=source,
            )
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.current_campaign_id = int(campaign["id"])
        self._comments_dirty = False
        if source == SOURCE_PREWRITTEN:
            self._set_save_status("✓ Комментарии сохранены и кампания запущена", saved=True)
        else:
            self.openai_status.setText("OpenAI-кампания запущена · отправка автоматическая")
        self.table.setRowCount(0)
        self.refresh_campaign()
    def pause_campaign(self):
        if not self.adapter.pause_comment_campaign():
            QMessageBox.information(
                self, "Кампания", "Активную кампанию не удалось поставить на паузу"
            )
        self.refresh_campaign()
    def resume_campaign(self):
        if not self.adapter.resume_comment_campaign():
            QMessageBox.information(
                self, "Кампания", "Нет приостановленной кампании для продолжения"
            )
        self.refresh_campaign()
    def stop_campaign(self):
        state = self.adapter.get_comment_campaign_state()
        if not state or state.get("status") not in {
            "running",
            "paused",
            "network_wait",
            "cycle_wait",
        }:
            return
        answer = QMessageBox.question(
            self,
            "Остановить кампанию",
            "Будущие слоты будут отменены. Уже начатая операция завершится безопасно. Продолжить?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.adapter.stop_comment_campaign()
        self.refresh_campaign()
    def _load_campaign_snapshot(
        self,
        *,
        account_id: int | None = None,
        generation: int | None = None,
    ) -> dict[str, object]:
        owner_account_id = (
            self._current_account_id() if account_id is None else max(0, int(account_id))
        )
        owner_generation = (
            self._account_generation if generation is None else int(generation)
        )

        state_getter = self.adapter.get_comment_campaign_state
        try:
            state = state_getter(account_id=owner_account_id)
        except TypeError:  # compatibility with lightweight test adapters
            state = state_getter()
        channels: list[dict] = []
        history: list[dict] = []
        if state and owner_account_id > 0:
            campaign_id = int(state["id"])
            try:
                channels = list(
                    self.adapter.get_channels(account_id=owner_account_id) or []
                )
            except TypeError:
                channels = list(self.adapter.get_channels() or [])
            try:
                history = list(
                    self.adapter.get_comment_history(
                        campaign_id=campaign_id,
                        limit=200,
                        account_id=owner_account_id,
                    )
                    or []
                )
            except TypeError:
                history = list(
                    self.adapter.get_comment_history(
                        campaign_id=campaign_id, limit=200
                    )
                    or []
                )
        return {
            "account_id": owner_account_id,
            "generation": owner_generation,
            "state": state,
            "channels": channels,
            "history": history,
        }
    def _snapshot_is_current(self, snapshot: dict[str, object]) -> bool:
        try:
            snapshot_account_id = max(0, int(cast(int, snapshot.get("account_id")) or 0))
            snapshot_generation = int(
                cast(int, snapshot.get("generation", self._account_generation))
            )
        except (TypeError, ValueError, OverflowError):
            return False
        return (
            snapshot_account_id == self._current_account_id()
            and snapshot_generation == self._account_generation
        )
    def request_campaign_refresh(self) -> None:
        """Poll SQLite in the thread pool without accepting stale account data."""

        if not self._page_active:
            return
        if self._refresh_job is not None:
            self._refresh_pending = True
            return
        account_id = self._current_account_id()
        generation = self._account_generation
        cleanup = getattr(self.adapter, "close_thread_connection", None)

        def load_snapshot() -> dict[str, object]:
            return self._load_campaign_snapshot(
                account_id=account_id, generation=generation
            )

        job = BackgroundCall(
            load_snapshot,
            cleanup=cleanup if callable(cleanup) else None,
        )
        self._refresh_job = job

        def succeeded(view: CommentingView, snapshot: object) -> None:
            if not isinstance(snapshot, dict):
                if generation == view._account_generation:
                    view._handle_refresh_error("Некорректный снимок состояния")
                return
            if not view._snapshot_is_current(snapshot):
                view._refresh_pending = True
                return
            try:
                view._apply_campaign_snapshot(snapshot)
            except Exception as exc:  # noqa: BLE001 - Qt signal boundary
                view._handle_refresh_error(f"{type(exc).__name__}: {exc}")
            else:
                view._last_refresh_error = ""

        def failed(view: CommentingView, message: str) -> None:
            if (
                generation == view._account_generation
                and account_id == view._current_account_id()
            ):
                view._handle_refresh_error(message)

        def finished(view: CommentingView) -> None:
            if view._refresh_job is job:
                view._refresh_job = None
            if view._refresh_pending and view._page_active:
                view._refresh_pending = False
                QTimer.singleShot(0, view.request_campaign_refresh)

        connect_lifecycle_safe(
            job,
            self,
            succeeded=succeeded,
            failed=failed,
            finished=finished,
        )
        QThreadPool.globalInstance().start(job)
    def _handle_refresh_error(self, message: str) -> None:
        message = str(message or "Неизвестная ошибка")
        if message != self._last_refresh_error:
            log.warning("Could not refresh comment campaign UI: %s", message)
            self._last_refresh_error = message
        self.status.setText("Не удалось обновить статус кампании")
        if self._next_check_at is None:
            self.next_label.setText("Следующая проверка: временно недоступна")
    def refresh_campaign(self):
        """Synchronous refresh for explicit actions; timer polling is asynchronous."""

        try:
            self._refresh_campaign()
        except Exception as exc:  # noqa: BLE001 - explicit UI callback boundary
            self._handle_refresh_error(f"{type(exc).__name__}: {exc}")
            return
        self._last_refresh_error = ""
    def _refresh_campaign(self):
        self._apply_campaign_snapshot(self._load_campaign_snapshot())
    def _apply_campaign_snapshot(self, snapshot: dict[str, object]) -> bool:
        if ("account_id" in snapshot or "generation" in snapshot) and not self._snapshot_is_current(snapshot):
            return False
        state = snapshot.get("state")
        if not isinstance(state, dict):
            self.current_campaign_id = None
            self.status.setText("Кампания не запущена")
            self.period_label.setText("Период: —")
            self._clear_next_check("Следующая проверка: —")
            selected_limit = int(self.daily_limit_slider.value())
            self.count_label.setText(
                f"Выполнено: 0 из {selected_limit} · отправлено: 0"
            )
            self.progress.setValue(0)
            self._set_buttons("none")
            self.table.setRowCount(0)
            self._last_history_rows = []
            self.campaign_stats_label.setText(
                format_campaign_statistics(campaign_statistics(None, []))
            )
            self.channel_names = {}
            return True

        self.current_campaign_id = int(state["id"])
        status = str(state.get("status") or "")
        labels = {
            "running": "Кампания работает",
            "paused": "Кампания на паузе",
            "network_wait": "Кампания ожидает сеть",
            "cycle_wait": "Текущий цикл выполнен",
            "completed": "Суточный период завершён",
            "stopped": "Кампания остановлена",
        }
        title = labels.get(status, f"Статус: {status}")
        reason = str(state.get("pause_reason") or "").strip()
        self.status.setText(title + (f" · {reason}" if reason else ""))
        self.period_label.setText(
            f"Период: {state.get('started_display', '—')} — {state.get('ends_display', '—')}"
        )
        if status == "paused":
            self._clear_next_check("Следующая проверка: после продолжения")
        elif status in {"completed", "stopped"}:
            self._clear_next_check("Следующая проверка: —")
        else:
            self._set_next_check(
                state.get("next_scheduled_at"),
                fallback="Следующая проверка: ожидается планирование",
            )
        attempted = int(state.get("attempted_count") or 0)
        sent = int(state.get("sent_count") or 0)
        limit = max(1, int(state.get("daily_limit") or 40))
        planned = max(1, int(state.get("planned_count") or limit))
        self.count_label.setText(
            f"Выполнено: {attempted} из {planned} · отправлено: {sent} · темп: {limit}/24 ч"
        )
        self.progress.setValue(min(100, round(attempted * 100 / planned)))
        self._set_buttons(status)

        channel_rows = snapshot.get("channels")
        self.channel_names = {
            int(row["channel_id"]): row.get("title") or str(row["channel_id"])
            for row in (list(channel_rows) if isinstance(channel_rows, list) else [])
            if isinstance(row, dict) and row.get("channel_id") is not None
        }
        history_rows = snapshot.get("history")
        rows = list(history_rows) if isinstance(history_rows, list) else []
        self.campaign_stats_label.setText(
            format_campaign_statistics(campaign_statistics(state, rows))
        )
        self._render_history(rows)
        return True
    def _rerender_history(self, _index: int = -1) -> None:
        self._render_history(self._last_history_rows, remember=False)
    def _render_history(self, rows: list[dict], *, remember: bool = True) -> None:
        if remember:
            self._last_history_rows = list(rows)
        selected = str(self.history_filter.currentData() or "all")
        visible = [
            item
            for item in self._last_history_rows
            if selected == "all" or classify_result(item.get("status")) == selected
        ]
        self.table.setRowCount(len(visible))
        for row, item in enumerate(visible):
            channel_id = item.get("channel_id")
            values = [
                self.channel_names.get(int(channel_id), str(channel_id))
                if channel_id is not None
                else "—",
                str(item.get("post_id") or "обычное сообщение"),
                str(item.get("comment_text") or "—"),
                humanize_reason(item.get("status")),
            ]
            for column, value in enumerate(values):
                widget_item = QTableWidgetItem(value)
                if column == 1:
                    widget_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, widget_item)
    def _set_buttons(self, status: str):
        if status in {"running", "paused", "network_wait", "cycle_wait"}:
            self.limit_save_timer.stop()
        self.start_button.setEnabled(
            status not in {"running", "paused", "network_wait", "cycle_wait"}
        )
        self.pause_button.setEnabled(status == "running")
        self.resume_button.setEnabled(status == "paused")
        self.stop_button.setEnabled(
            status in {"running", "paused", "network_wait", "cycle_wait"}
        )
        editors_enabled = status not in {
            "running",
            "paused",
            "network_wait",
            "cycle_wait",
        }
        for editor in self.editors:
            editor.setEnabled(editors_enabled)
        self.save_comments_button.setEnabled(editors_enabled)
        self.add_variant_button.setEnabled(False)
        self.delete_variant_button.setEnabled(False)
        self.import_previous_button.setEnabled(editors_enabled)
        self.continuous.setEnabled(editors_enabled)
        self.daily_limit_slider.setEnabled(editors_enabled)
    def load_history(self):
        if self.current_campaign_id is None:
            self.table.setRowCount(0)
            return
        self.channel_names = {
            int(row["channel_id"]): row.get("title") or str(row["channel_id"])
            for row in self.adapter.get_channels()
        }
        rows = self.adapter.get_comment_history(
            campaign_id=self.current_campaign_id,
            limit=200,
        )
        self._render_history(list(rows or []))

from __future__ import annotations

from typing import cast

from datetime import datetime
import weakref

import shiboken6
from PySide6.QtCore import QThreadPool, QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from core.campaign_schedule import from_db_time, utc_now
from core.countdown import countdown_label, seconds_until
from gui.background import BackgroundCall


class ActivityPanel(QFrame):
    """Compact, user-facing activity feed reconstructed from persistent state."""

    def __init__(self, adapter, parent=None):
        super().__init__(parent)
        self.adapter = adapter
        self.setObjectName("activityPanel")
        self.setMinimumHeight(145)

        self._account_id: int | None = None
        self._campaign_id: int | None = None
        self._campaign_status = ""
        self._last_history_id = 0
        self._last_log_id = 0
        self._last_next_at = ""
        self._last_missed_count = 0
        self._channel_names: dict[int, str] = {}
        self._collapsed = False
        self._last_scheduler_error = ""
        self._restriction_active = False
        self._join_campaign_id = None
        self._join_attempted = -1
        self._link_task_id: int | None = None
        self._link_task_status = ""
        self._link_task_not_before = ""
        self._link_wait_bucket: int | None = None
        self._refresh_job: BackgroundCall | None = None
        self._refresh_pending = False
        self._account_generation = 0
        self._countdown_at = None
        self._countdown_key = ""
        self._countdown_prefix = "Следующая проверка"
        self._countdown_include_deadline = True
        self._countdown_due_refresh_requested = False
        self._countdown_fallback = "Следующая проверка: —"

        self.title_label = QLabel("ЖИВОЙ ЖУРНАЛ")
        self.title_label.setObjectName("activityTitle")
        self.state_label = QLabel("Ожидание кампании")
        self.state_label.setObjectName("activityBadge")
        self.state_label.setWordWrap(True)
        self.next_label = QLabel("Следующая проверка: —")
        self.next_label.setMinimumWidth(290)
        self.next_label.setObjectName("activityNext")
        self.next_label.setWordWrap(True)

        self.spambot_button = QPushButton("Проверить блокировку @SpamBot")
        self.spambot_button.setObjectName("tinyButton")
        self.spambot_button.setToolTip(
            "Кнопка становится доступна после остановки кампании из-за ограничения Telegram"
        )
        self.spambot_button.setEnabled(False)
        self.spambot_button.clicked.connect(self._check_spambot)

        self.collapse_button = QPushButton("Свернуть")
        self.collapse_button.setObjectName("tinyButton")
        self.collapse_button.clicked.connect(self._toggle_collapsed)

        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(self.title_label)
        header.addWidget(self.state_label)
        header.addStretch(1)
        header.addWidget(self.next_label)
        header.addWidget(self.spambot_button)
        header.addWidget(self.collapse_button)

        self.feed = QPlainTextEdit()
        self.feed.setObjectName("activityLog")
        self.feed.setReadOnly(True)
        self.feed.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.feed.setMaximumBlockCount(500)
        self.feed.setPlaceholderText("Здесь появятся действия LansetSpBot…")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 13, 18, 14)
        layout.setSpacing(9)
        layout.addLayout(header)
        layout.addWidget(self.feed, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(3_000)
        self.timer.timeout.connect(self.request_refresh)
        self.timer.start()

        # The header countdown is local-only and updates every second. It is
        # derived from an absolute timestamp, so a delayed event-loop tick
        # catches up instead of continuing from a stale local counter.
        self.countdown_timer = QTimer(self)
        self.countdown_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.countdown_timer.setInterval(1_000)
        self.countdown_timer.timeout.connect(self._update_countdown_label)
        self.countdown_timer.start()
        self._append("Журнал готов. Запустите кампанию во вкладке «Комментирование».")
        self.request_refresh()

    def _current_account_id(self) -> int:
        getter = getattr(self.adapter, "get_current_account_id", None)
        if not callable(getter):
            return 0
        try:
            return max(0, int(getter() or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def handle_account_changed(self) -> None:
        """Clear the old feed immediately and invalidate its queued callbacks."""

        self._account_generation += 1
        self._refresh_pending = self._refresh_job is not None
        self._reset_for_account(self._current_account_id())
        if self._refresh_job is None:
            self.request_refresh()

    def set_compact(self, compact: bool) -> None:
        self.title_label.setVisible(not compact)
        self.next_label.setVisible(not compact)
        self.collapse_button.setText(
            ("Развернуть" if self._collapsed else "Свернуть")
            if not compact
            else ("▲" if self._collapsed else "▼")
        )
        self.collapse_button.setToolTip(
            "Развернуть журнал" if self._collapsed else "Свернуть журнал"
        )
        self.spambot_button.setText(
            "@SpamBot" if compact else "Проверить блокировку @SpamBot"
        )

    def _check_spambot(self) -> None:
        try:
            state = self.adapter.get_account_restriction_state() or {}
        except Exception as exc:
            QMessageBox.warning(self, "@SpamBot", str(exc))
            return

        opened = QDesktopServices.openUrl(QUrl("tg://resolve?domain=SpamBot"))
        if not opened:
            QDesktopServices.openUrl(QUrl("https://t.me/SpamBot"))

        if not bool(state.get("active")):
            QMessageBox.information(
                self,
                "Проверка @SpamBot",
                "@SpamBot открыт в Telegram. Сейчас локальная блокировка LansetSpBot "
                "не установлена. Следуйте ответу официального бота.",
            )
            return

        message = QMessageBox(self)
        message.setWindowTitle("Проверка ограничения @SpamBot")
        message.setIcon(QMessageBox.Icon.Warning)
        message.setText(
            "LansetSpBot остановил все отправки после ограничения Telegram.\n\n"
            "Проверьте ответ официального @SpamBot. Снимайте локальную блокировку "
            "только если бот прямо сообщает, что ограничений больше нет."
        )
        clear_button = message.addButton(
            "@SpamBot сообщил: ограничений нет", QMessageBox.ButtonRole.AcceptRole
        )
        message.addButton("Оставить блокировку", QMessageBox.ButtonRole.RejectRole)
        message.exec()
        if message.clickedButton() is clear_button:
            try:
                self.adapter.confirm_spambot_restriction_cleared()
            except Exception as exc:
                QMessageBox.warning(self, "@SpamBot", str(exc))
                return
            self._append(
                "Локальная блокировка снята после подтверждения ответа @SpamBot."
            )
            self.request_refresh()

    def _toggle_collapsed(self) -> None:
        self._collapsed = not self._collapsed
        self.feed.setVisible(not self._collapsed)
        compact = not self.title_label.isVisible()
        self.collapse_button.setText(
            ("▲" if self._collapsed else "▼")
            if compact
            else ("Развернуть" if self._collapsed else "Свернуть")
        )
        self.setMinimumHeight(54 if self._collapsed else 145)
        self.setMaximumHeight(64 if self._collapsed else 16_777_215)

    @staticmethod
    def _clock() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _append(self, text: str) -> None:
        clean = " ".join(str(text or "").split())
        if not clean:
            return
        self.feed.appendPlainText(f"[{self._clock()}]  {clean}")
        cursor = self.feed.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.feed.setTextCursor(cursor)
        self.feed.ensureCursorVisible()

    def _reset_for_account(self, account_id: int) -> None:
        """Drop all cached GUI state when the selected Telegram account changes."""

        was_initialized = self._account_id is not None
        self._account_id = max(0, int(account_id or 0))
        self.feed.clear()
        self._campaign_id = None
        self._campaign_status = ""
        self._last_history_id = 0
        self._last_log_id = 0
        self._last_next_at = ""
        self._last_missed_count = 0
        self._channel_names = {}
        self._last_scheduler_error = ""
        self._restriction_active = False
        self._join_campaign_id = None
        self._join_attempted = -1
        self._link_task_id = None
        self._link_task_status = ""
        self._link_task_not_before = ""
        self._link_wait_bucket = None
        self.state_label.setText(
            "Ожидание кампании"
            if self._account_id > 0
            else "Аккаунт не подключён"
        )
        self.spambot_button.setEnabled(False)
        self.spambot_button.setObjectName("tinyButton")
        self.spambot_button.style().unpolish(self.spambot_button)
        self.spambot_button.style().polish(self.spambot_button)
        self._clear_countdown("Следующая проверка: —")
        if self._account_id > 0:
            self._append(
                (
                    "Аккаунт переключён. "
                    if was_initialized
                    else "Журнал текущего аккаунта загружен. "
                )
                + "Показываются только действия этого Telegram-аккаунта."
            )
        else:
            self._append("Telegram-аккаунт не подключён.")

    @staticmethod
    def _duration_text(seconds: int) -> str:
        seconds = max(0, int(seconds))
        if seconds < 10:
            return "сейчас"
        if seconds < 60:
            return f"через {seconds} сек"
        minutes = round(seconds / 60)
        if minutes < 60:
            return f"через {minutes} мин"
        hours, minutes = divmod(minutes, 60)
        if minutes:
            return f"через {hours} ч {minutes} мин"
        return f"через {hours} ч"

    def _next_description(self, value) -> tuple[str, str]:
        parsed = from_db_time(value)
        if parsed is None:
            return "Следующая проверка: —", ""
        seconds = round((parsed - utc_now()).total_seconds())
        relative = self._duration_text(seconds)
        local_time = parsed.astimezone().strftime("%H:%M")
        return f"Следующая проверка {relative} · {local_time}", relative

    def _set_countdown(
        self,
        value,
        *,
        prefix: str = "Следующая проверка",
        include_deadline: bool = True,
        fallback: str | None = None,
    ) -> None:
        parsed = from_db_time(value)
        key = parsed.isoformat() if parsed is not None else ""
        if key != self._countdown_key or prefix != self._countdown_prefix:
            self._countdown_due_refresh_requested = False
        self._countdown_at = parsed
        self._countdown_key = key
        self._countdown_prefix = str(prefix)
        self._countdown_include_deadline = bool(include_deadline)
        self._countdown_fallback = fallback or f"{prefix}: —"
        self._update_countdown_label()

    def _clear_countdown(self, text: str = "Следующая проверка: —") -> None:
        self._countdown_at = None
        self._countdown_key = ""
        self._countdown_due_refresh_requested = False
        self._countdown_fallback = str(text or "Следующая проверка: —")
        self.next_label.setText(self._countdown_fallback)

    def _update_countdown_label(self) -> None:
        target = self._countdown_at
        if target is None:
            self.next_label.setText(self._countdown_fallback)
            return
        self.next_label.setText(
            countdown_label(
                self._countdown_prefix,
                target,
                include_deadline=self._countdown_include_deadline,
                include_date=False,
            )
        )
        remaining = seconds_until(target)
        if (
            remaining == 0
            and self._refresh_job is None
            and not self._countdown_due_refresh_requested
        ):
            self._countdown_due_refresh_requested = True
            self.request_refresh()

    def _load_channel_names(self) -> None:
        try:
            self._channel_names = {
                int(row["channel_id"]): row.get("title")
                or row.get("username")
                or str(row["channel_id"])
                for row in (self.adapter.get_channels() or [])
                if row.get("channel_id") is not None
            }
        except Exception:
            self._channel_names = {}

    def _history_message(self, row: dict) -> str:
        channel_id = row.get("channel_id")
        channel = "канал"
        if channel_id is not None:
            try:
                channel = self._channel_names.get(int(channel_id), str(channel_id))
            except (TypeError, ValueError):
                channel = str(channel_id)
        status = str(row.get("status") or "").strip()
        post_id = row.get("post_id")
        if status == "Отправлено":
            suffix = f" под постом #{post_id}" if post_id is not None else ""
            return f"Оставлен комментарий в чате обсуждения «{channel}»{suffix}."
        if status.startswith("Пропущено:"):
            return f"«{channel}» — {status}."
        if status.startswith("Кампания приостановлена"):
            return status + ("." if not status.endswith(".") else "")
        if status:
            return f"«{channel}» — {status}."
        return f"Проверка канала «{channel}» завершена."

    def _refresh_persistent_logs(self, rows: list[dict]) -> None:
        for row in sorted(rows, key=lambda item: int(item.get("id") or 0)):
            row_id = int(row.get("id") or 0)
            if row_id <= self._last_log_id:
                continue
            level = str(row.get("level") or "INFO").upper()
            message = str(row.get("message") or "").strip()
            if message:
                self._append(f"[{level}] {message}")
            self._last_log_id = max(self._last_log_id, row_id)

    @staticmethod
    def _countdown_text(seconds: int) -> str:
        value = max(0, int(seconds))
        hours, remainder = divmod(value, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _apply_link_task(self, raw_task: object, *, restricted: bool) -> bool:
        """Render the latest link-preparation task in the shared live journal.

        Detailed worker events arrive through persistent logs.  This method adds
        a coarse FloodWait countdown and lifecycle transitions, and gives an
        active link task priority in the compact header without flooding the
        journal every three seconds.
        """

        if not isinstance(raw_task, dict):
            return False

        task_id = int(raw_task.get("id") or 0)
        if task_id <= 0:
            return False
        status = str(raw_task.get("status") or "")
        progress = max(0, min(100, int(raw_task.get("progress") or 0)))
        not_before = str(raw_task.get("not_before") or "")
        previous_status = self._link_task_status
        previous_not_before = self._link_task_not_before
        is_new = task_id != self._link_task_id
        if is_new:
            self._link_task_id = task_id
            self._link_task_status = ""
            self._link_task_not_before = ""
            self._link_wait_bucket = None
            previous_status = ""
            previous_not_before = ""

        retry_at = from_db_time(not_before)
        remaining = 0
        waiting = False
        if retry_at is not None:
            remaining = max(0, round((retry_at - utc_now()).total_seconds()))
            waiting = status == "pending" and remaining > 0

        active = status == "running" or status == "pending"
        if active and not restricted:
            if waiting:
                self.state_label.setText(f"Связки · FloodWait · {progress}%")
                self._set_countdown(
                    retry_at,
                    prefix="Продолжение",
                    include_deadline=False,
                )
            elif status == "running":
                self.state_label.setText(f"Связки · выполняются · {progress}%")
                self._clear_countdown(
                    "Checkpoint сохраняется после каждого канала"
                )
            else:
                self.state_label.setText(f"Связки · ожидание запуска · {progress}%")
                self._clear_countdown("Следующая проверка: —")

        if is_new and status == "pending" and not waiting:
            self._append("[Связки] Задача поставлена в очередь.")

        if waiting:
            step = 300 if remaining > 600 else (60 if remaining > 60 else 10)
            bucket = ((remaining + step - 1) // step) * step
            if self._link_wait_bucket is None:
                self._link_wait_bucket = bucket
            elif bucket != self._link_wait_bucket:
                self._link_wait_bucket = bucket
                self._append(
                    "[Связки] FloodWait продолжается: до автоматического "
                    f"возобновления {self._countdown_text(remaining)}; "
                    f"прогресс сохранён на {progress}%."
                )
        else:
            if (
                status == "running"
                and previous_status == "pending"
                and previous_not_before
            ):
                self._append(
                    "[Связки] FloodWait завершён. Работа продолжена с "
                    "сохранённого канала без повторного обхода."
                )
            self._link_wait_bucket = None

        if status in {"failed", "cancelled"} and status != previous_status:
            error = str(raw_task.get("error") or "неизвестная ошибка").strip()
            self._append(f"[Связки] Операция остановлена: {error}.")

        self._link_task_status = status
        self._link_task_not_before = not_before
        return active

    def _load_snapshot(
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
        join_getter = self.adapter.get_join_campaign_state
        try:
            state = state_getter(account_id=owner_account_id)
        except TypeError:
            state = state_getter()
        try:
            join_state = join_getter(account_id=owner_account_id)
        except TypeError:
            join_state = join_getter()

        channels: list[dict] = []
        history: list[dict] = []
        if state and owner_account_id > 0:
            try:
                channels = list(
                    self.adapter.get_channels(account_id=owner_account_id) or []
                )
            except TypeError:
                channels = list(self.adapter.get_channels() or [])
            limit = int(state.get("daily_limit") or 40)
            try:
                history = list(
                    self.adapter.get_comment_history(
                        campaign_id=int(state["id"]),
                        limit=max(100, limit + 20),
                        account_id=owner_account_id,
                    )
                    or []
                )
            except TypeError:
                history = list(
                    self.adapter.get_comment_history(
                        campaign_id=int(state["id"]),
                        limit=max(100, limit + 20),
                    )
                    or []
                )
        restriction_getter = getattr(
            self.adapter, "get_account_restriction_state", None
        )
        restriction: dict = {}
        if callable(restriction_getter):
            try:
                restriction = dict(
                    restriction_getter(account_id=owner_account_id) or {}
                )
            except TypeError:  # compatibility with lightweight GUI test doubles
                restriction = dict(restriction_getter() or {})
        link_task: dict | None = None
        link_task_getter = getattr(self.adapter, "get_active_link_task", None)
        if callable(link_task_getter):
            try:
                link_task = link_task_getter(account_id=owner_account_id)
            except TypeError:
                link_task = link_task_getter()
        else:  # compatibility with older/lightweight adapters only
            task_getter = getattr(self.adapter, "get_tasks", None)
            if callable(task_getter):
                try:
                    tasks = list(task_getter(limit=100) or [])
                except TypeError:
                    tasks = list(task_getter() or [])
                candidates = [
                    row
                    for row in tasks
                    if isinstance(row, dict)
                    and str(row.get("type") or "") == "link_channels"
                    and str(row.get("status") or "")
                    in {"pending", "running", "paused"}
                    and int((row.get("payload") or {}).get("account_id") or 0)
                    == owner_account_id
                ]
                if candidates:
                    link_task = max(
                        candidates,
                        key=lambda row: int(row.get("id") or 0),
                    )
        try:
            log_rows = self.adapter.get_logs(
                limit=150, account_id=owner_account_id
            )
        except TypeError:  # compatibility with lightweight GUI test doubles
            log_rows = self.adapter.get_logs(limit=150)
        return {
            "account_id": owner_account_id,
            "generation": owner_generation,
            "logs": list(log_rows or []),
            "restriction": restriction,
            "scheduler_error": str(self.adapter.get_scheduler_error() or ""),
            "link_task": link_task,
            "state": state,
            "join_state": join_state,
            "channels": channels,
            "history": history,
        }

    def request_refresh(self) -> None:
        """Refresh persistent state without accepting stale account snapshots."""

        if self._refresh_job is not None:
            self._refresh_pending = True
            return

        panel_ref = weakref.ref(self)
        account_id = self._current_account_id()
        generation = self._account_generation

        def live_panel() -> ActivityPanel | None:
            panel = panel_ref()
            if panel is None or not shiboken6.isValid(panel):
                return None
            return panel

        def load_snapshot() -> dict[str, object] | None:
            panel = live_panel()
            if panel is None:
                return None
            return panel._load_snapshot(
                account_id=account_id, generation=generation
            )

        cleanup = getattr(self.adapter, "close_thread_connection", None)
        job = BackgroundCall(
            load_snapshot,
            cleanup=cleanup if callable(cleanup) else None,
        )
        self._refresh_job = job

        def succeeded(snapshot: object) -> None:
            panel = live_panel()
            if panel is not None and isinstance(snapshot, dict):
                panel._apply_snapshot(snapshot)

        def failed(_message: str) -> None:
            panel = live_panel()
            if (
                panel is not None
                and generation == panel._account_generation
                and account_id == panel._current_account_id()
            ):
                panel.state_label.setText("Нет связи с базой")

        def finished() -> None:
            panel = live_panel()
            if panel is None:
                return
            if panel._refresh_job is job:
                panel._refresh_job = None
            if panel._refresh_pending:
                panel._refresh_pending = False
                QTimer.singleShot(0, panel.request_refresh)

        job.signals.succeeded.connect(succeeded)
        job.signals.failed.connect(failed)
        job.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(job)

    def refresh(self) -> None:
        """Synchronous refresh retained for explicit user actions and tests."""

        try:
            snapshot = self._load_snapshot()
        except Exception:
            self.state_label.setText("Нет связи с базой")
            return
        self._apply_snapshot(snapshot)

    def _apply_snapshot(self, snapshot: dict[str, object]) -> None:
        snapshot_account_id = max(0, int(cast(int, snapshot.get("account_id")) or 0))
        try:
            snapshot_generation = int(
                cast(int, snapshot.get("generation", self._account_generation))
            )
        except (TypeError, ValueError, OverflowError):
            snapshot_generation = -1
        current_account_id = self._current_account_id()
        if (
            current_account_id != snapshot_account_id
            or snapshot_generation != self._account_generation
        ):
            # Never paint a queued read from the previous owner. Clear the old
            # feed immediately and arrange a refresh once the active job exits.
            if self._account_id != current_account_id:
                self._reset_for_account(current_account_id)
            self._refresh_pending = True
            return
        if self._account_id != snapshot_account_id:
            self._reset_for_account(snapshot_account_id)
        logs = snapshot.get("logs")
        self._refresh_persistent_logs(list(logs) if isinstance(logs, list) else [])
        scheduler_error = str(snapshot.get("scheduler_error") or "")
        if scheduler_error and scheduler_error != self._last_scheduler_error:
            self._append(
                f"Ошибка планировщика: {scheduler_error}. Кампания приостановлена."
            )
        self._last_scheduler_error = scheduler_error
        raw_restriction = snapshot.get("restriction")
        restriction = raw_restriction if isinstance(raw_restriction, dict) else {}
        restricted = bool(restriction.get("active"))
        self.spambot_button.setEnabled(restricted)
        self.spambot_button.setToolTip(
            "Открыть официальный @SpamBot и подтвердить снятие ограничения"
            if restricted
            else "Доступно после остановки кампании из-за ограничения Telegram"
        )
        self.spambot_button.setObjectName(
            "dangerButton" if restricted else "tinyButton"
        )
        self.spambot_button.style().unpolish(self.spambot_button)
        self.spambot_button.style().polish(self.spambot_button)
        if restricted:
            code = str(restriction.get("code") or "telegram_restricted")
            self.state_label.setText(f"RESTRICTED · {code} · проверьте @SpamBot")
            if not self._restriction_active:
                self._append(
                    "Telegram ограничил аккаунт. Комментарии и оставшиеся "
                    "вступления остановлены; автоматические повторы отключены. "
                    "Нажмите «Проверить блокировку @SpamBot»."
                )
        self._restriction_active = restricted
        if restricted:
            self._clear_countdown("Отправки остановлены из-за ограничения Telegram")
        link_active = self._apply_link_task(
            snapshot.get("link_task"), restricted=restricted
        )
        state = snapshot.get("state")
        join_state = snapshot.get("join_state")
        if not isinstance(state, dict):
            state = None
        if not isinstance(join_state, dict):
            join_state = None

        if join_state:
            join_id = int(join_state.get("id") or 0)
            attempted = int(join_state.get("attempted_count") or 0)
            joined = int(join_state.get("joined_count") or 0)
            total = int(join_state.get("total_count") or 0)
            if join_id != self._join_campaign_id:
                self._join_campaign_id = join_id
                self._join_attempted = attempted
                join_status = str(join_state.get("status") or "")
                if join_status == "completed":
                    self._append(
                        f"Последняя кампания вступлений завершена: обработано {attempted}/{total}, "
                        f"успешно вступили в {joined}."
                    )
                elif join_status == "stopped":
                    self._append(
                        f"Последняя кампания вступлений остановлена: обработано {attempted}/{total}, "
                        f"успешно вступили в {joined}."
                    )
                else:
                    hourly_limit = int(join_state.get("max_per_hour") or 0)
                    self._append(
                        f"Кампания вступлений запущена: {total} каналов/групп, "
                        f"лимит {hourly_limit} успешных вступлений в час."
                    )
            elif attempted != self._join_attempted:
                self._append(
                    f"Кампания вступлений: обработано {attempted}/{total}, успешно вступили в {joined}."
                )
                self._join_attempted = attempted

        if not state:
            if join_state:
                attempted = int(join_state.get("attempted_count") or 0)
                joined = int(join_state.get("joined_count") or 0)
                total = int(join_state.get("total_count") or 0)
                raw_join_status = str(join_state.get("status") or "")
                status = {
                    "running": "Вступления",
                    "paused": "Вступления · пауза",
                    "network_wait": "Вступления · сеть",
                    "completed": "Вступления · завершены",
                    "stopped": "Вступления · остановлены",
                }.get(raw_join_status, "Вступления")
                if not restricted and not link_active:
                    self.state_label.setText(
                        f"{status} · {attempted}/{total} · успешно {joined}"
                    )
                if not link_active and not restricted:
                    if raw_join_status == "paused":
                        self._clear_countdown(
                            "Следующая попытка вступления: после продолжения"
                        )
                    elif raw_join_status in {"completed", "stopped"}:
                        self._clear_countdown("Следующая попытка вступления: —")
                    else:
                        self._set_countdown(
                            join_state.get("next_scheduled_at"),
                            prefix="Следующая попытка вступления",
                            include_deadline=True,
                            fallback="Следующая попытка вступления: ожидается планирование",
                        )
            else:
                if not restricted and not link_active:
                    self.state_label.setText("Ожидание кампании")
                if not link_active and not restricted:
                    self._clear_countdown("Следующая проверка: —")
            return

        campaign_id = int(state["id"])
        status = str(state.get("status") or "")
        attempted = int(state.get("attempted_count") or 0)
        sent = int(state.get("sent_count") or 0)
        limit = int(state.get("daily_limit") or 40)
        planned = max(1, int(state.get("planned_count") or limit))
        status_names = {
            "running": "Активна",
            "paused": "Пауза",
            "network_wait": "Ожидание сети",
            "completed": "Завершена",
            "stopped": "Остановлена",
        }
        if not restricted and not link_active:
            self.state_label.setText(
                f"{status_names.get(status, status or 'Кампания')} · {attempted}/{planned} · "
                f"отправлено {sent} · темп {limit}/24ч"
            )

        _next_text, relative = self._next_description(state.get("next_scheduled_at"))
        if not link_active and not restricted:
            if status == "paused":
                self._clear_countdown("Следующая проверка: после продолжения")
            elif status in {"completed", "stopped"}:
                self._clear_countdown("Следующая проверка: —")
            else:
                self._set_countdown(
                    state.get("next_scheduled_at"),
                    prefix="Следующая проверка",
                    include_deadline=True,
                    fallback="Следующая проверка: ожидается планирование",
                )

        if campaign_id != self._campaign_id:
            self._campaign_id = campaign_id
            self._campaign_status = status
            self._last_history_id = 0
            self._last_next_at = ""
            self._last_missed_count = 0
            channel_rows = snapshot.get("channels")
            self._channel_names = {
                int(row["channel_id"]): row.get("title")
                or row.get("username")
                or str(row["channel_id"])
                for row in (
                    list(channel_rows) if isinstance(channel_rows, list) else []
                )
                if isinstance(row, dict) and row.get("channel_id") is not None
            }
            initial_messages = {
                "running": (
                    f"Кампания запущена. Запланировано {planned} уникальных каналов; "
                    f"темп ползунка {limit}/24ч."
                ),
                "paused": (
                    f"Кампания на паузе. В плане {planned} уникальных каналов; "
                    f"темп {limit}/24ч."
                ),
                "network_wait": "Кампания ожидает восстановления соединения с Telegram.",
                "completed": "Последняя суточная кампания завершена.",
                "stopped": "Последняя кампания остановлена пользователем.",
            }
            self._append(
                initial_messages.get(
                    status, f"Загружена кампания со статусом: {status or 'неизвестно'}."
                )
            )

        if status != self._campaign_status:
            messages = {
                "running": "Кампания продолжена.",
                "paused": "Кампания поставлена на паузу.",
                "network_wait": "Нет соединения с Telegram. Кампания автоматически ждёт восстановления сети.",
                "completed": "Суточная кампания завершена.",
                "stopped": "Кампания остановлена пользователем.",
            }
            if status in messages:
                self._append(messages[status])
            self._campaign_status = status

        history = snapshot.get("history")
        for row in list(history) if isinstance(history, list) else []:
            if not isinstance(row, dict):
                continue
            row_id = int(row.get("id") or 0)
            if row_id <= self._last_history_id:
                continue
            self._append(self._history_message(row))
            self._last_history_id = max(self._last_history_id, row_id)

        missed_count = int((state.get("schedule_counts") or {}).get("missed", 0))
        if missed_count > self._last_missed_count:
            delta = missed_count - self._last_missed_count
            self._append(
                f"После простоя пропущено {delta} накопившихся слотов. Догоняющей рассылки не будет."
            )
        self._last_missed_count = missed_count

        next_at = str(state.get("next_scheduled_at") or "")
        if next_at and next_at != self._last_next_at:
            parsed = from_db_time(next_at)
            if parsed is not None:
                local_time = parsed.astimezone().strftime("%d.%m в %H:%M")
                self._append(
                    f"Следующая проверка запланирована {relative} — {local_time}."
                )
            self._last_next_at = next_at

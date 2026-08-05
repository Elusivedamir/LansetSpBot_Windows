from __future__ import annotations

from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton

from core.campaign_schedule import from_db_time, utc_now
from core.version import APP_NAME
from gui.views.links_view import LinksView


class PremiumLinksView(LinksView):
    """Links page that always runs validation and exposes an explicit force mode."""

    def __init__(self, adapter):
        super().__init__(adapter)
        self.link_button.setText("Проверить новые связки")
        for label in self.findChildren(QLabel):
            if "Между проверками выдерживается" in label.text():
                label.setText(
                    "Для канала программа получает ID обсуждения через Telegram API, "
                    "проверяет актуальность связки, участие и права текущего аккаунта. "
                    "Между отдельными Telegram API-запросами выдерживается 2–5 секунд, "
                    "между каналами — 12–20 секунд, между операциями вступления — "
                    "2–5 минут. Локальные паузы и настоящий Telegram FloodWait "
                    "отображаются раздельно; позиция сохраняется до ожидания."
                )
                break
        self.force_button = QPushButton("Перепроверить всё принудительно")
        self.force_button.setObjectName("secondaryButton")
        self.force_button.setToolTip(
            "Игнорировать сохранённый результат проверки и заново проверить все каналы"
        )
        self.force_button.clicked.connect(lambda: self._start_validation(force=True))
        self.buttons_layout.addWidget(self.force_button, 1, 0, 1, 2)
        try:
            self.link_button.clicked.disconnect()
        except RuntimeError:
            pass
        self.link_button.clicked.connect(lambda: self._start_validation(force=False))

    def set_compact_mode(self, compact: bool) -> None:
        super().set_compact_mode(compact)
        self.buttons_layout.removeWidget(self.force_button)
        self.buttons_layout.addWidget(
            self.force_button, 2 if compact else 1, 0, 1, 2
        )

    def start_linking(self) -> None:
        self._start_validation(force=False)

    def start_force_recheck(self) -> None:
        self._start_validation(force=True)

    def _set_busy(self, busy: bool, text: str | None = None) -> None:
        self.link_button.setEnabled(not busy)
        self.force_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.link_button.setProperty("busy", busy)
        self.force_button.setProperty("busy", busy)
        if text:
            self.link_button.setText(text)
        for button in (self.link_button, self.force_button):
            button.style().unpolish(button)
            button.style().polish(button)

    def _start_validation(self, *, force: bool) -> None:
        all_targets = self.adapter.get_channels() or []
        if not all_targets:
            QMessageBox.information(
                self,
                "Нет каналов и групп",
                "Сначала получите каналы и группы во вкладке «Каналы»",
            )
            return
        try:
            account_id = self._current_account_id()
            if account_id <= 0:
                raise ValueError("Сначала выберите Telegram-аккаунт")
            active = self.adapter.get_active_link_task(account_id=account_id)
            unchecked = int(
                self.adapter.count_unchecked_link_targets(account_id=account_id)
            )
            if active and self._task_account_id(active) != account_id:
                active = None
            if force and active:
                raise RuntimeError(
                    "Для аккаунта уже выполняется проверка связок. "
                    "Дождитесь завершения или остановите её перед принудительной перепроверкой."
                )
            if active and str(active.get("status") or "") == "paused":
                task = active
                if not self.adapter.resume_link_task(int(task["id"])):
                    raise RuntimeError("Не удалось продолжить сохранённую задачу связок")
                task = dict(task)
                task["status"] = "pending"
            elif active:
                task = active
            else:
                payload = {
                    "force_recheck": bool(force),
                    "revalidate_cached": True,
                    "account_id": account_id,
                }
                task = self.adapter.create_task("link_channels", payload)
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return

        self.total = self._task_total(task, max(unchecked, len(all_targets)))
        self.current_task_id = int(task["id"])
        self._set_busy(
            True,
            "Принудительная проверка выполняется"
            if force
            else "Связки проверяются",
        )
        self.progress.setValue(int(task.get("progress") or 0))
        self._last_rendered_progress = None
        self._due_restart_task_id = None
        self.status.setText(
            f"Задача #{self.current_task_id}: фактическая Telegram-проверка запущена"
        )
        self.watcher.watch(self.current_task_id)
        if not self.adapter.start_queue():
            if not task.get("reused") and not active:
                self.adapter.cancel_task(self.current_task_id)
            self.watcher.stop()
            self.current_task_id = None
            self._set_busy(False, "Проверить новые связки")
            self.stop_button.setEnabled(False)
            self.status.setText("Не удалось запустить очередь")
            QMessageBox.warning(
                self, APP_NAME, self.adapter.get_queue_unavailable_message()
            )

    def stop_linking(self) -> None:
        super().stop_linking()
        if self.current_task_id is not None and not self.stop_button.isEnabled():
            self.status.setText(
                "Стоп принят · текущий Telegram-запрос или локальная пауза "
                "завершатся, затем задача остановится на сохранённой позиции"
            )

    @staticmethod
    def _wait_kind(task: dict) -> str:
        payload = task.get("payload") or {}
        checkpoint = payload.get("_link_checkpoint") if isinstance(payload, dict) else None
        if isinstance(checkpoint, dict):
            return str(checkpoint.get("wait_type") or "")
        error = str(task.get("error") or "")
        if "local_join_rate_wait" in error:
            return "local_join_cooldown"
        return "telegram_flood_wait" if task.get("not_before") else ""

    def _task_changed(self, task):
        super()._task_changed(task)
        owner = self._current_account_id()
        if owner <= 0 or self._task_account_id(task) != owner:
            return
        task_status = str(task.get("status") or "")
        if task_status not in {"pending", "running"}:
            return
        wait_kind = self._wait_kind(task)
        retry_at = from_db_time(task.get("not_before"))
        remaining = (
            max(0, round((retry_at - utc_now()).total_seconds()))
            if retry_at is not None
            else 0
        )
        runtime = str(task.get("status_text") or "").strip()
        if wait_kind in {"local_join_cooldown", "channel_cooldown"}:
            prefix = (
                "Локальная пауза между вступлениями"
                if wait_kind == "local_join_cooldown"
                else "Пауза между каналами"
            )
            self.status.setText(
                runtime
                or f"{prefix}: {self._format_duration(remaining)} · позиция сохранена"
            )
        elif wait_kind == "telegram_flood_wait" and remaining > 0:
            self.status.setText(
                runtime
                or "Telegram FloodWait: "
                f"{self._format_duration(remaining)} · все RPC аккаунта остановлены"
            )

    def _task_finished(self, task):
        super()._task_finished(task)
        self.force_button.setEnabled(True)
        self.force_button.setProperty("busy", False)
        self.link_button.setProperty("busy", False)
        self.link_button.setText("Проверить новые связки")
        self.force_button.style().unpolish(self.force_button)
        self.force_button.style().polish(self.force_button)

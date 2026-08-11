# OBSERVABILITY-PACKAGE-V3
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, cast

from PySide6.QtCore import QStandardPaths, QThreadPool, QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
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


_CAMPAIGN_STATUS_LABELS = {
    "running": "активна",
    "paused": "на паузе",
    "network_wait": "ожидает сеть",
    "cycle_wait": "ожидает цикл",
    "stopped": "остановлена",
    "failed": "ошибка",
    "completed": "завершена",
}

_WARMUP_STATUS_LABELS = {
    "active": "активен",
    "paused": "на паузе",
    "completed": "завершён",
    "failed": "ошибка",
    "available": "свободен",
}


class AudienceParserView(QWidget):
    """Aurora page for exporting visible group members' usernames to TXT."""

    def __init__(self, adapter, parent=None):
        super().__init__(parent)
        self.adapter = adapter
        self._account_id = 0
        self._account_rows: dict[int, dict[str, Any]] = {}
        self._account_generation = 0
        self._load_generation = 0
        self._load_job: BackgroundCall | None = None
        self._reload_requested = False
        self._account_refresh_job: BackgroundCall | None = None
        self._account_refresh_pending = False
        self._account_refresh_generation = 0
        self._page_active = False
        self._compact_mode = False
        self._source_guard = False
        self._resumable_task: dict[str, Any] | None = None
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
        account_layout.setSpacing(8)
        account_title = QLabel("Аккаунт для парсинга")
        account_title.setObjectName("cardTitle")
        self.account_selector = QComboBox()
        self.account_selector.setMinimumContentsLength(28)
        self.account_selector.currentIndexChanged.connect(
            self._parser_account_selected
        )
        self.account_label = QLabel("Telegram-аккаунт не выбран")
        self.account_label.setObjectName("mutedText")
        self.account_label.setWordWrap(True)
        account_layout.addWidget(account_title)
        account_layout.addWidget(self.account_selector)
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

        filters_card = QFrame()
        filters_card.setObjectName("card")
        filters_layout = QVBoxLayout(filters_card)
        filters_layout.setContentsMargins(22, 18, 22, 20)
        filters_layout.setSpacing(10)
        filters_title = QLabel("Дополнительные фильтры")
        filters_title.setObjectName("cardTitle")
        filters_hint = QLabel(
            "Все новые фильтры выключены по умолчанию. Без них правила парсинга не меняются."
        )
        filters_hint.setObjectName("mutedText")
        filters_hint.setWordWrap(True)
        filters_layout.addWidget(filters_title)
        filters_layout.addWidget(filters_hint)
        filters_row = QHBoxLayout()
        self.exclude_admins = QCheckBox("Исключать администраторов")
        self.exclude_scam_fake = QCheckBox("Исключать scam/fake")
        self.activity_filter = QComboBox()
        self.activity_filter.addItem("Активность: не учитывать", 0)
        self.activity_filter.addItem("Активность: 7 дней", 7)
        self.activity_filter.addItem("Активность: 30 дней", 30)
        self.activity_filter.addItem("Активность: 90 дней", 90)
        filters_row.addWidget(self.exclude_admins)
        filters_row.addWidget(self.exclude_scam_fake)
        filters_row.addStretch(1)
        filters_row.addWidget(self.activity_filter)
        filters_layout.addLayout(filters_row)
        self._root_layout.addWidget(filters_card)

        self.recovery_card = QFrame()
        self.recovery_card.setObjectName("infoCard")
        recovery_layout = QVBoxLayout(self.recovery_card)
        recovery_layout.setContentsMargins(22, 18, 22, 18)
        recovery_layout.setSpacing(10)
        recovery_title = QLabel("Незавершённая выгрузка")
        recovery_title.setObjectName("cardTitle")
        self.recovery_label = QLabel("—")
        self.recovery_label.setObjectName("mutedText")
        self.recovery_label.setWordWrap(True)
        recovery_actions = QHBoxLayout()
        self.resume_recovery_button = QPushButton("Продолжить")
        self.resume_recovery_button.setObjectName("primaryButton")
        self.resume_recovery_button.clicked.connect(self.resume_recovered_export)
        self.restart_recovery_button = QPushButton("Начать заново")
        self.restart_recovery_button.setObjectName("secondaryButton")
        self.restart_recovery_button.clicked.connect(self.restart_recovered_export)
        self.discard_recovery_button = QPushButton("Удалить")
        self.discard_recovery_button.setObjectName("dangerButton")
        self.discard_recovery_button.clicked.connect(self.discard_recovered_export)
        recovery_actions.addWidget(self.resume_recovery_button)
        recovery_actions.addWidget(self.restart_recovery_button)
        recovery_actions.addWidget(self.discard_recovery_button)
        recovery_actions.addStretch(1)
        recovery_layout.addWidget(recovery_title)
        recovery_layout.addWidget(self.recovery_label)
        recovery_layout.addLayout(recovery_actions)
        self.recovery_card.hide()
        self._root_layout.addWidget(self.recovery_card)

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
        self.parser_stats = QLabel("Статистика появится после запуска")
        self.parser_stats.setObjectName("mutedText")
        self.parser_stats.setWordWrap(True)
        result_layout.addWidget(self.parser_stats)

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

        self.account_refresh_timer = QTimer(self)
        # Status overview is not a scheduler clock. Ten seconds keeps it fresh
        # while reducing database pressure during long multi-account sessions.
        self.account_refresh_timer.setInterval(10_000)
        self.account_refresh_timer.timeout.connect(
            self._periodic_account_refresh
        )
        self.handle_account_changed()

    def _global_account_id(self) -> int:
        try:
            return max(0, int(self.adapter.get_current_account_id() or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def _current_account_id(self) -> int:
        return max(0, int(self._account_id or 0))

    @staticmethod
    def _task_account_id(task: dict[str, Any]) -> int:
        payload = task.get("payload") or {}
        try:
            return max(
                0,
                int(
                    task.get("account_id")
                    or (
                        payload.get("account_id")
                        if isinstance(payload, dict)
                        else 0
                    )
                    or 0
                ),
            )
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _account_identity(
        account: dict[str, Any],
        account_id: int,
    ) -> str:
        name = str(
            account.get("display_name")
            or account.get("account_name")
            or account.get("username")
            or account.get("phone_masked")
            or f"ID {account_id}"
        ).strip()
        username = str(account.get("username") or "").strip().lstrip("@")
        if username and f"@{username}" not in name:
            return f"{name} · @{username}"
        return name

    @staticmethod
    def _campaign_state_for(
        adapter,
        account_id: int,
        getter_name: str,
    ) -> dict[str, Any]:
        getter = getattr(adapter, getter_name, None)
        if not callable(getter):
            return {}
        try:
            state = getter(account_id=account_id)
        except (TypeError, ValueError, OverflowError):
            return {}
        except Exception:
            return {}
        return dict(state or {}) if isinstance(state, dict) else {}

    @staticmethod
    def _pair_status_map(
        pairs: list[dict[str, Any]],
    ) -> dict[int, str]:
        priority = {
            "running": 5,
            "paused": 4,
            "failed": 3,
            "completed": 2,
            "archived": 1,
        }
        result: dict[int, str] = {}
        for pair in pairs:
            status = str(pair.get("status") or "").strip()
            if not status:
                continue
            for key in ("account_a_id", "account_b_id"):
                try:
                    account_id = int(pair.get(key) or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if account_id <= 0:
                    continue
                previous = result.get(account_id, "")
                if priority.get(status, 0) >= priority.get(previous, 0):
                    result[account_id] = status
        return result

    def _workflow_accounts(self) -> list[dict[str, Any]]:
        overview: dict[str, Any] = {}
        try:
            overview = dict(self.adapter.get_warmup_overview() or {})
            rows = [
                dict(item) for item in overview.get("accounts") or []
            ]
        except Exception:
            try:
                rows = [
                    dict(item)
                    for item in self.adapter.list_telegram_accounts() or []
                ]
            except Exception:
                rows = []

        pair_status_by_account = self._pair_status_map(
            [dict(item) for item in overview.get("pairs") or []]
        )
        authorized: list[dict[str, Any]] = []
        relevant: list[dict[str, Any]] = []
        active_campaign_statuses = {
            "running",
            "paused",
            "network_wait",
            "cycle_wait",
        }
        relevant_campaign_statuses = active_campaign_statuses | {
            "stopped",
            "failed",
        }

        for row in rows:
            if not bool(row.get("authorized")):
                continue
            try:
                account_id = int(
                    row.get("telegram_account_id")
                    or row.get("id")
                    or 0
                )
            except (TypeError, ValueError, OverflowError):
                continue
            if account_id <= 0:
                continue

            comment = self._campaign_state_for(
                self.adapter,
                account_id,
                "get_comment_campaign_state",
            )
            join = self._campaign_state_for(
                self.adapter,
                account_id,
                "get_join_campaign_state",
            )
            candidates = [
                (
                    "комментарии",
                    str(comment.get("status") or "").strip(),
                ),
                (
                    "вступления",
                    str(join.get("status") or "").strip(),
                ),
            ]
            candidates = [
                (kind, status)
                for kind, status in candidates
                if status
            ]

            chosen_kind = ""
            chosen_status = ""
            for kind, status in candidates:
                if status in active_campaign_statuses:
                    chosen_kind = kind
                    chosen_status = status
                    break
            if not chosen_status:
                for kind, status in candidates:
                    if status in relevant_campaign_statuses:
                        chosen_kind = kind
                        chosen_status = status
                        break

            row["campaign_kind"] = chosen_kind
            row["campaign_status"] = chosen_status
            row["campaign_active"] = bool(
                row.get("campaign_active")
                or chosen_status in active_campaign_statuses
            )

            warmup_status = str(
                row.get("warmup_status") or "available"
            ).strip()
            pair_status = pair_status_by_account.get(account_id, "")
            if pair_status == "running":
                warmup_status = "active"
            elif pair_status == "paused":
                warmup_status = "paused"
            elif pair_status == "completed":
                warmup_status = "completed"
            elif pair_status == "failed":
                warmup_status = "failed"
            row["warmup_status"] = warmup_status

            workflow_relevant = bool(
                row.get("stopped")
                or row["campaign_active"]
                or chosen_status in relevant_campaign_statuses
                or warmup_status not in {"", "available"}
                or row.get("active_pair_id")
            )
            row["workflow_relevant"] = workflow_relevant
            row["_account_id"] = account_id
            authorized.append(row)
            if workflow_relevant:
                relevant.append(row)

        # Parsing is account-scoped and may run independently of campaigns on
        # other accounts. Workflow relevance changes badges/sort order only; it
        # must never hide another authorized Telegram account.
        selected = authorized
        selected.sort(
            key=lambda row: (
                0
                if (
                    bool(row.get("campaign_active"))
                    or str(row.get("warmup_status") or "") == "active"
                )
                else 1,
                self._account_identity(
                    row,
                    int(row["_account_id"]),
                ).casefold(),
            )
        )
        return selected

    def _workflow_status_text(self, row: dict[str, Any]) -> str:
        if not row:
            return "Telegram-аккаунт не выбран"

        parts: list[str] = []
        if bool(row.get("stopped")):
            parts.append("Аккаунт: остановлен")
        else:
            runtime = str(
                row.get("runtime_state") or "connected"
            ).strip()
            if runtime:
                parts.append(f"Аккаунт: {runtime}")

        warmup_status = str(
            row.get("warmup_status") or "available"
        )
        if warmup_status not in {"", "available"}:
            parts.append(
                "Прогрев: "
                + _WARMUP_STATUS_LABELS.get(
                    warmup_status,
                    warmup_status,
                )
            )
        else:
            parts.append("Прогрев: не активен")

        campaign_status = str(row.get("campaign_status") or "")
        if campaign_status:
            label = _CAMPAIGN_STATUS_LABELS.get(
                campaign_status,
                campaign_status,
            )
            kind = str(row.get("campaign_kind") or "").strip()
            parts.append(
                f"Кампания ({kind}): {label}"
                if kind
                else f"Кампания: {label}"
            )
        elif bool(row.get("campaign_active")):
            parts.append("Кампания: активна")
        else:
            parts.append("Кампания: не активна")

        return " · ".join(parts)

    def _combo_caption(self, row: dict[str, Any]) -> str:
        account_id = int(row["_account_id"])
        identity = self._account_identity(row, account_id)
        tags: list[str] = []

        warmup_status = str(
            row.get("warmup_status") or "available"
        )
        if warmup_status not in {"", "available"}:
            tags.append(
                "прогрев: "
                + _WARMUP_STATUS_LABELS.get(
                    warmup_status,
                    warmup_status,
                )
            )

        campaign_status = str(row.get("campaign_status") or "")
        if campaign_status:
            tags.append(
                "кампания: "
                + _CAMPAIGN_STATUS_LABELS.get(
                    campaign_status,
                    campaign_status,
                )
            )
        elif bool(row.get("campaign_active")):
            tags.append("кампания: активна")

        if bool(row.get("stopped")):
            tags.append("аккаунт остановлен")

        return identity + (
            f" · {' · '.join(tags)}" if tags else ""
        )

    def _apply_account_options(
        self,
        rows: list[dict[str, Any]],
    ) -> None:
        previous = self._current_account_id()
        global_selected = self._global_account_id()
        self._account_rows = {
            int(row["_account_id"]): row for row in rows
        }

        self.account_selector.blockSignals(True)
        try:
            self.account_selector.clear()
            for row in rows:
                account_id = int(row["_account_id"])
                self.account_selector.addItem(
                    self._combo_caption(row),
                    account_id,
                )

            preferred = (
                previous
                if previous in self._account_rows
                else (
                    global_selected
                    if global_selected in self._account_rows
                    else (
                        int(rows[0]["_account_id"])
                        if rows
                        else 0
                    )
                )
            )
            index = self.account_selector.findData(preferred)
            if index >= 0:
                self.account_selector.setCurrentIndex(index)
            elif self.account_selector.count() > 0:
                self.account_selector.setCurrentIndex(0)
                preferred = int(
                    self.account_selector.currentData() or 0
                )
            else:
                preferred = 0
        finally:
            self.account_selector.blockSignals(False)

        self._activate_parser_account(
            preferred,
            force=preferred != previous,
        )

    def _refresh_account_options(self) -> None:
        self._account_refresh_generation += 1
        generation = self._account_refresh_generation
        if self._account_refresh_job is not None:
            self._account_refresh_pending = True
            return
        self._account_refresh_pending = False

        cleanup = getattr(self.adapter, "close_thread_connection", None)
        job = BackgroundCall(
            self._workflow_accounts,
            cleanup=cleanup if callable(cleanup) else None,
        )
        self._account_refresh_job = job

        def succeeded(view: AudienceParserView, value: object) -> None:
            if (
                generation != view._account_refresh_generation
                or not view._page_active
            ):
                view._account_refresh_pending = True
                return
            rows = [
                dict(item)
                for item in cast(Iterable[object], value or [])
                if isinstance(item, dict)
            ]
            view._apply_account_options(rows)

        def failed(view: AudienceParserView, message: str) -> None:
            if (
                generation == view._account_refresh_generation
                and view._page_active
            ):
                view.account_label.setText(
                    f"Не удалось обновить список аккаунтов: {message}"
                )

        def finished(view: AudienceParserView) -> None:
            if view._account_refresh_job is job:
                view._account_refresh_job = None
            rerun = (
                view._account_refresh_pending
                or generation != view._account_refresh_generation
            )
            view._account_refresh_pending = False
            if rerun and view._page_active:
                QTimer.singleShot(0, view._refresh_account_options)

        connect_lifecycle_safe(
            job,
            self,
            succeeded=succeeded,
            failed=failed,
            finished=finished,
        )
        QThreadPool.globalInstance().start(job)

    def _periodic_account_refresh(self) -> None:
        if self._page_active and self.current_task_id is None:
            self._refresh_account_options()

    def _parser_account_selected(self, _index: int) -> None:
        account_id = int(
            self.account_selector.currentData() or 0
        )
        self._activate_parser_account(account_id)

    def _activate_parser_account(
        self,
        account_id: int,
        *,
        force: bool = False,
    ) -> None:
        account_id = max(0, int(account_id or 0))
        if account_id == self._account_id and not force:
            row = self._account_rows.get(account_id, {})
            self.account_label.setText(
                self._workflow_status_text(row)
            )
            return

        self._account_generation += 1
        self._account_id = account_id
        self._load_generation += 1
        self._reload_requested = self._load_job is not None
        self.watcher.stop()
        self.current_task_id = None
        self.current_mode = ""
        self.output_path = None
        self.progress.setValue(0)
        self.parser_stats.setText(
            "Статистика появится после запуска"
        )
        self.stop_button.setEnabled(False)
        self.open_folder_button.setEnabled(False)
        self.link_input.clear()
        self._replace_groups(
            [],
            loaded=False,
            syncing=False,
        )

        row = self._account_rows.get(account_id, {})
        self.account_label.setText(
            self._workflow_status_text(row)
        )
        stopped = bool(row.get("stopped"))

        if account_id <= 0:
            self.summary.setText(
                "Нет подключённых авторизованных аккаунтов"
            )
        elif stopped:
            self.summary.setText(
                "Аккаунт отображается, но его работа остановлена. "
                "Возобновите аккаунт во вкладке «Аккаунт»."
            )
        else:
            self.summary.setText("Парсинг не запущен")

        self._set_work_enabled(
            account_id > 0 and not stopped
        )
        if (
            self._page_active
            and account_id > 0
            and not stopped
        ):
            self.load_cached_groups()
        self.refresh_recovery()

    def handle_account_changed(self) -> None:
        self._account_refresh_generation += 1
        if self._page_active:
            self._refresh_account_options()
        else:
            self._account_refresh_pending = True

    def set_page_active(self, active: bool) -> None:
        self._page_active = bool(active)
        self.watcher.set_active(self._page_active)
        if self._page_active:
            self.account_refresh_timer.start()
            self._refresh_account_options()
            if self._account_id > 0:
                row = self._account_rows.get(
                    self._account_id,
                    {},
                )
                if not bool(row.get("stopped")):
                    self.load_cached_groups()
            self.refresh_recovery()
        else:
            self.account_refresh_timer.stop()
            self._account_refresh_generation += 1

    def _set_work_enabled(self, enabled: bool) -> None:
        idle = bool(
            enabled and self.current_task_id is None
        )
        self.account_selector.setEnabled(
            self.current_task_id is None
        )
        self.group_combo.setEnabled(idle)
        self.link_input.setEnabled(idle)
        self.start_button.setEnabled(idle)
        self.exclude_admins.setEnabled(idle)
        self.exclude_scam_fake.setEnabled(idle)
        self.activity_filter.setEnabled(idle)
        self.load_groups_button.setEnabled(idle)

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
                self,
                APP_NAME,
                "Выберите аккаунт в блоке «Аккаунт для парсинга».",
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
                "filters": {
                    "exclude_admins": self.exclude_admins.isChecked(),
                    "exclude_scam_fake": self.exclude_scam_fake.isChecked(),
                    "activity_days": int(self.activity_filter.currentData() or 0),
                },
            },
            mode="parse",
        )

    def _start_task(self, task_type: str, payload: dict[str, Any], *, mode: str) -> None:
        payload = dict(payload)
        payload["account_id"] = self._current_account_id()
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
        if mode == "parse":
            self.parser_stats.setText(
                "Просмотрено: 0 · сохранено: 0 · скорость: 0.0/с"
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
            if self.current_mode == "parse":
                self.parser_stats.setText(status_text)

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
        self.refresh_recovery()

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

    def refresh_recovery(self) -> None:
        account_id = self._current_account_id()
        if account_id <= 0 or self.current_task_id is not None:
            self._resumable_task = None
            self.recovery_card.hide()
            return
        try:
            task = self.adapter.find_resumable_audience_task(account_id)
        except Exception as exc:
            self._resumable_task = None
            self.recovery_card.hide()
            self.parser_stats.setText(f"Не удалось проверить восстановление: {exc}")
            return
        self._resumable_task = dict(task) if isinstance(task, dict) else None
        if not self._resumable_task:
            self.recovery_card.hide()
            return
        checkpoint = self._resumable_task.get("checkpoint") or {}
        payload = self._resumable_task.get("payload") or {}
        counters = checkpoint.get("counters") or {}
        title = (
            checkpoint.get("source_title")
            or payload.get("source_title")
            or "группа"
        )
        self.recovery_label.setText(
            f"Группа: {title} · позиция: {int(checkpoint.get('offset') or 0)} · "
            f"просмотрено: {int(counters.get('scanned') or 0)} · "
            f"сохранено: {int(counters.get('saved') or 0)}"
        )
        self.recovery_card.show()

    def _activate_recovered_task(self, task: dict[str, Any]) -> None:
        task_id = int(task.get("id") or 0)
        if task_id <= 0:
            return
        payload = task.get("payload") or {}
        self.current_task_id = task_id
        self.current_mode = "parse"
        output = str(payload.get("output_path") or "") if isinstance(payload, dict) else ""
        self.output_path = Path(output) if output else None
        if self.output_path is not None:
            self.output_label.setText(str(self.output_path))
        self.progress.setValue(max(0, min(100, int(task.get("progress") or 0))))
        self.stop_button.setEnabled(True)
        self.open_folder_button.setEnabled(False)
        self._set_work_enabled(False)
        self.recovery_card.hide()
        self.watcher.watch(task_id)
        if not self.adapter.start_queue():
            self.summary.setText("Задача подготовлена, но фоновый обработчик недоступен")
            QMessageBox.warning(self, APP_NAME, self.adapter.get_queue_unavailable_message())

    def resume_recovered_export(self) -> None:
        task = self._resumable_task
        if not task:
            return
        task_id = int(task.get("id") or 0)
        try:
            changed = bool(self.adapter.resume_audience_task(task_id))
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if not changed:
            QMessageBox.warning(self, APP_NAME, "Не удалось продолжить сохранённую выгрузку")
            self.refresh_recovery()
            return
        task["status"] = "pending"
        self._activate_recovered_task(task)

    def restart_recovered_export(self) -> None:
        task = self._resumable_task
        if not task:
            return
        task_id = int(task.get("id") or 0)
        try:
            changed = bool(self.adapter.restart_audience_task(task_id))
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if not changed:
            QMessageBox.warning(self, APP_NAME, "Не удалось начать выгрузку заново")
            self.refresh_recovery()
            return
        task["status"] = "pending"
        task["progress"] = 0
        self._activate_recovered_task(task)

    def discard_recovered_export(self) -> None:
        task = self._resumable_task
        if not task:
            return
        task_id = int(task.get("id") or 0)
        try:
            changed = bool(self.adapter.discard_audience_task(task_id))
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if not changed:
            QMessageBox.warning(self, APP_NAME, "Не удалось удалить незавершённую выгрузку")
        self._resumable_task = None
        self.recovery_card.hide()
        self.parser_stats.setText("Незавершённая выгрузка удалена")

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

from __future__ import annotations

from functools import partial
from typing import Any, Callable

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.campaign_schedule import from_db_time
from core.countdown import countdown_label
from gui.background import BackgroundCall, connect_lifecycle_safe


_STATUS_LABELS = {
    "running": "Прогрев выполняется",
    "paused": "Прогрев на паузе",
    "completed": "Прогрев завершён",
    "archived": "Связка завершена",
}


class WarmupView(QWidget):
    """Managed seven-day account-pair workflows with native controls."""

    COLLAPSED_GROUP_LIMIT = 2

    def __init__(self, adapter) -> None:
        super().__init__()
        self.adapter = adapter
        self._overview: dict[str, Any] = {}
        self._busy = False
        self._page_active = False
        self._compact = False
        self._refresh_in_flight = False
        self._refresh_pending = False
        self._selector_accounts: list[dict[str, Any]] = []
        self._selector_refresh_in_flight = False
        self._selector_refresh_pending = False
        self._background_jobs: set[BackgroundCall] = set()
        self._selected_pair_id: int | None = None
        self._journal_views: dict[int, dict[str, Any]] = {}
        self._expanded_group_pairs: set[int] = set()

        root = QVBoxLayout(self)
        root.setContentsMargins(30, 28, 30, 30)
        root.setSpacing(18)

        title = QLabel("Прогрев")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "Свяжите два собственных Telegram-аккаунта. Программа создаст "
            "разный семидневный сценарий, подготовит контакты, распределит "
            "диалоги и действия только по добавленным вами группам."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        account_card = QFrame()
        account_card.setObjectName("card")
        account_layout = QVBoxLayout(account_card)
        account_layout.setContentsMargins(20, 18, 20, 18)
        account_layout.setSpacing(12)
        existing_title = QLabel("Подключённый аккаунт")
        existing_title.setObjectName("cardTitle")
        self.existing_account_selector = QComboBox()
        self.existing_account_selector.setMinimumContentsLength(24)
        self.existing_account_selector.currentIndexChanged.connect(
            self._existing_account_selected
        )
        self.existing_account_hint = QLabel(
            "Выберите аккаунт, уже подключённый во вкладке «Аккаунт». "
            "Подключение и авторизация из «Прогрева» недоступны."
        )
        self.existing_account_hint.setObjectName("mutedText")
        self.existing_account_hint.setWordWrap(True)
        account_layout.addWidget(existing_title)
        account_layout.addWidget(self.existing_account_selector)
        account_layout.addWidget(self.existing_account_hint)
        self.account_card = account_card
        root.addWidget(account_card)
        account_card.hide()

        create_card = QFrame()
        create_card.setObjectName("card")
        create_layout = QVBoxLayout(create_card)
        create_layout.setContentsMargins(20, 18, 20, 18)
        create_layout.setSpacing(12)
        create_title = QLabel("Новая связка на 7 дней")
        create_title.setObjectName("cardTitle")
        self.limit_label = QLabel("Аккаунтов в прогреве: 0 из 40")
        self.limit_label.setObjectName("mutedText")
        create_layout.addWidget(create_title)
        create_layout.addWidget(self.limit_label)

        selectors = QHBoxLayout()
        selectors.setSpacing(10)
        self.account_a = QComboBox()
        self.account_a.setMinimumWidth(220)
        self.account_a.currentIndexChanged.connect(self._pair_selection_changed)
        self.account_b = QComboBox()
        self.account_b.setMinimumWidth(220)
        self.account_b.currentIndexChanged.connect(self._pair_selection_changed)
        account_a_box = QVBoxLayout()
        account_a_label = QLabel("Аккаунт A")
        account_a_label.setObjectName("mutedText")
        account_a_box.addWidget(account_a_label)
        account_a_box.addWidget(self.account_a)
        account_b_box = QVBoxLayout()
        account_b_label = QLabel("Аккаунт B")
        account_b_label.setObjectName("mutedText")
        account_b_box.addWidget(account_b_label)
        account_b_box.addWidget(self.account_b)
        self.create_button = QPushButton("Создать связку")
        self.create_button.setObjectName("primaryButton")
        self.create_button.clicked.connect(self._create_pair)
        selectors.addLayout(account_a_box, 1)
        selectors.addWidget(QLabel("↔"))
        selectors.addLayout(account_b_box, 1)
        selectors.addWidget(self.create_button)
        create_layout.addLayout(selectors)
        root.addWidget(create_card)

        groups_card = QFrame()
        groups_card.setObjectName("card")
        groups_layout = QVBoxLayout(groups_card)
        groups_layout.setContentsMargins(20, 18, 20, 18)
        groups_layout.setSpacing(10)
        groups_header = QHBoxLayout()
        groups_title = QLabel("Группы прогрева")
        groups_title.setObjectName("cardTitle")
        self.load_groups_a_button = QPushButton("Группы для аккаунта A")
        self.load_groups_a_button.setObjectName("primaryButton")
        self.load_groups_a_button.clicked.connect(
            partial(self._load_pair_groups, "a")
        )
        self.load_groups_b_button = QPushButton("Группы для аккаунта B")
        self.load_groups_b_button.setObjectName("primaryButton")
        self.load_groups_b_button.clicked.connect(
            partial(self._load_pair_groups, "b")
        )
        self.add_group_button = QPushButton("Добавить вручную")
        self.add_group_button.setObjectName("secondaryButton")
        self.add_group_button.clicked.connect(self._add_group)
        groups_header.addWidget(groups_title)
        groups_header.addStretch(1)
        groups_header.addWidget(self.load_groups_a_button)
        groups_header.addWidget(self.load_groups_b_button)
        groups_header.addWidget(self.add_group_button)
        groups_layout.addLayout(groups_header)
        groups_hint = QLabel(
            "У каждого аккаунта связки свой список. Нажмите отдельную кнопку "
            "аккаунта: программа возьмёт его синхронизированные Telegram-группы "
            "и случайно подберёт 3–4. "
            "Частоту посещений, число читаемых постов и реакций программа задаёт автоматически."
        )
        groups_hint.setObjectName("mutedText")
        groups_hint.setWordWrap(True)
        groups_layout.addWidget(groups_hint)
        self.groups_status = QLabel("")
        self.groups_status.setObjectName("mutedText")
        self.groups_status.setWordWrap(True)
        groups_layout.addWidget(self.groups_status)
        self.groups_box = QVBoxLayout()
        self.groups_box.setSpacing(8)
        groups_layout.addLayout(self.groups_box)
        root.addWidget(groups_card)

        pairs_header = QHBoxLayout()
        pairs_title = QLabel("Связки аккаунтов")
        pairs_title.setObjectName("sectionTitle")
        self.pair_selector = QComboBox()
        self.pair_selector.setMinimumWidth(320)
        self.pair_selector.currentIndexChanged.connect(self._pair_selected)
        pairs_header.addWidget(pairs_title)
        pairs_header.addStretch(1)
        pairs_header.addWidget(self.pair_selector)
        root.addLayout(pairs_header)
        self.pairs_box = QVBoxLayout()
        self.pairs_box.setSpacing(14)
        root.addLayout(self.pairs_box)
        root.addStretch(1)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(5_000)
        self.refresh_timer.timeout.connect(self.refresh)
        self.journal_timer = QTimer(self)
        self.journal_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.journal_timer.setInterval(1_000)
        self.journal_timer.timeout.connect(self._update_journal_countdowns)
        self._set_busy(False)
        QTimer.singleShot(0, self.refresh)

    def _existing_account_selected(self, _index: int = -1) -> None:
        account_id = int(self.existing_account_selector.currentData() or 0)
        if account_id <= 0:
            return
        if self.account_a.findData(account_id) >= 0:
            self.existing_account_hint.setText(
                "Выбранный подключённый аккаунт подставлен как «Аккаунт A»."
            )
        else:
            self.existing_account_hint.setText(
                "Аккаунт подключён, но сейчас остановлен или уже назначен "
                "на активный прогрев."
            )

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif child is not None:
                WarmupView._clear_layout(child)

    @staticmethod
    def _account_label(account: dict[str, Any]) -> str:
        name = str(account.get("display_name") or "Telegram Account")
        username = str(account.get("username") or "").strip()
        phone = str(account.get("phone_masked") or "").strip()
        suffix = f" @{username}" if username else (f" · {phone}" if phone else "")
        return f"{name}{suffix}"

    @staticmethod
    def _warmup_accounts_for_selectors(
        accounts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Show every registered account; eligibility is enforced separately."""

        result: list[dict[str, Any]] = []
        for raw in accounts:
            item = dict(raw)
            try:
                account_id = int(item.get("telegram_account_id") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if account_id > 0:
                result.append(item)
        return result

    @staticmethod
    def _warmup_choice_label(account: dict[str, Any]) -> str:
        label = WarmupView._account_label(account)
        if not account.get("authorized"):
            label += " · требуется авторизация"
        elif account.get("stopped"):
            label += " · остановлен"
        active_pair_id = account.get("active_pair_id")
        if active_pair_id is not None:
            label += f" · в связке #{int(active_pair_id)}"
        return label

    @staticmethod
    def _warmup_account_creatable(account: dict[str, Any]) -> bool:
        return bool(
            account.get("authorized")
            and not account.get("stopped")
            and account.get("active_pair_id") is None
        )

    def _pair_selection_changed(self, _index: int = -1) -> None:
        self._set_busy(self._busy)

    def _selected_pair_is_creatable(self) -> bool:
        try:
            account_a = int(self.account_a.currentData() or 0)
            account_b = int(self.account_b.currentData() or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if account_a <= 0 or account_b <= 0 or account_a == account_b:
            return False
        selector_source = list(
            getattr(self, "_selector_accounts", None)
            or self._overview.get("accounts")
            or []
        )
        state_map = {
            int(item.get("telegram_account_id") or 0): dict(item)
            for item in selector_source
        }
        return all(
            account_id in state_map
            and WarmupView._warmup_account_creatable(state_map[account_id])
            for account_id in (account_a, account_b)
        )

    def _set_busy(self, active: bool) -> None:
        """Derive actions from the lightweight selector snapshot when available."""

        self._busy = bool(active)
        idle = not self._busy
        self.create_button.setEnabled(
            idle and WarmupView._selected_pair_is_creatable(self)
        )
        pair = self._selected_pair()
        pair_available = pair is not None
        self.load_groups_a_button.setEnabled(idle and pair_available)
        self.load_groups_b_button.setEnabled(idle and pair_available)
        self.add_group_button.setEnabled(idle and pair_available)

    def _finish_mutation(self, *, refresh_after: bool) -> None:
        self._set_busy(False)
        if refresh_after:
            # Always re-read authoritative SQLite state after both success and
            # failure. This prevents stale buttons after a backend invariant
            # rejected a duplicate/invalid operation.
            self.refresh(force=True)

    def _run(
        self,
        callback: Callable[[], Any],
        *,
        success: Callable[[Any], None] | None = None,
        refresh_after: bool = True,
    ) -> None:
        if self._busy:
            return
        self._set_busy(True)
        job = BackgroundCall(
            callback,
            cleanup=self.adapter.close_thread_connection,
        )

        def succeeded(owner: "WarmupView", value: Any) -> None:
            if success is not None:
                success(value)

        def failed(owner: "WarmupView", message: str) -> None:
            QMessageBox.warning(owner, "Прогрев", message)

        def finished(owner: "WarmupView") -> None:
            owner._background_jobs.discard(job)
            owner._finish_mutation(refresh_after=refresh_after)

        connect_lifecycle_safe(
            job,
            self,
            succeeded=succeeded,
            failed=failed,
            finished=finished,
        )
        self._background_jobs.add(job)
        QThreadPool.globalInstance().start(job)

    def refresh_selectors(self, *, force: bool = False) -> None:
        if self._selector_refresh_in_flight:
            self._selector_refresh_pending = (
                self._selector_refresh_pending or bool(force)
            )
            return
        self._selector_refresh_in_flight = True
        job = BackgroundCall(
            self.adapter.get_warmup_selector_accounts,
            cleanup=self.adapter.close_thread_connection,
        )

        def succeeded(owner: "WarmupView", value: Any) -> None:
            owner._apply_selector_accounts(
                [dict(item) for item in (value or [])]
            )

        def failed(owner: "WarmupView", message: str) -> None:
            owner.existing_account_hint.setText(
                f"Не удалось обновить список аккаунтов: {message}"
            )

        def finished(owner: "WarmupView") -> None:
            owner._background_jobs.discard(job)
            owner._selector_refresh_in_flight = False
            if owner._selector_refresh_pending:
                owner._selector_refresh_pending = False
                owner.refresh_selectors(force=True)

        connect_lifecycle_safe(
            job,
            self,
            succeeded=succeeded,
            failed=failed,
            finished=finished,
        )
        self._background_jobs.add(job)
        QThreadPool.globalInstance().start(job)

    def refresh(self, *, force: bool = False) -> None:
        # A/B selectors and "Получить мои каналы" must not wait for the full
        # overview, which also reads encrypted proxy settings and renders pairs.
        self.refresh_selectors(force=force)
        if self._busy and not force:
            return
        if self._refresh_in_flight:
            self._refresh_pending = self._refresh_pending or bool(force)
            return
        self._refresh_in_flight = True
        job = BackgroundCall(
            self.adapter.get_warmup_overview,
            cleanup=self.adapter.close_thread_connection,
        )

        def succeeded(owner: "WarmupView", value: Any) -> None:
            owner._apply_overview(dict(value or {}))

        def finished(owner: "WarmupView") -> None:
            owner._background_jobs.discard(job)
            owner._refresh_in_flight = False
            if owner._refresh_pending:
                owner._refresh_pending = False
                owner.refresh(force=True)

        connect_lifecycle_safe(
            job,
            self,
            succeeded=succeeded,
            finished=finished,
        )
        self._background_jobs.add(job)
        QThreadPool.globalInstance().start(job)

    def _apply_selector_accounts(self, accounts: list[dict[str, Any]]) -> None:
        self._selector_accounts = [dict(item) for item in accounts]
        visible_accounts = WarmupView._warmup_accounts_for_selectors(
            self._selector_accounts
        )

        previous_existing = self.existing_account_selector.currentData()
        self.existing_account_selector.blockSignals(True)
        self.existing_account_selector.clear()
        for account in visible_accounts:
            self.existing_account_selector.addItem(
                WarmupView._warmup_choice_label(account),
                int(account["telegram_account_id"]),
            )
        preferred_existing = previous_existing
        if preferred_existing is None:
            try:
                preferred_existing = int(self.adapter.get_selected_account_id() or 0)
            except (TypeError, ValueError, OverflowError):
                preferred_existing = 0
        existing_index = self.existing_account_selector.findData(preferred_existing)
        self.existing_account_selector.setCurrentIndex(
            existing_index
            if existing_index >= 0
            else (0 if self.existing_account_selector.count() else -1)
        )
        self.existing_account_selector.blockSignals(False)

        selected_a = self.account_a.currentData()
        selected_b = self.account_b.currentData()
        self.account_a.blockSignals(True)
        self.account_b.blockSignals(True)
        self.account_a.clear()
        self.account_b.clear()
        for account in visible_accounts:
            account_id = int(account["telegram_account_id"])
            label = WarmupView._warmup_choice_label(account)
            self.account_a.addItem(label, account_id)
            self.account_b.addItem(label, account_id)
        for combo, previous in (
            (self.account_a, selected_a),
            (self.account_b, selected_b),
        ):
            index = combo.findData(previous)
            if index >= 0:
                combo.setCurrentIndex(index)
        if (
            self.account_b.count() > 1
            and self.account_b.currentData() == self.account_a.currentData()
        ):
            self.account_b.setCurrentIndex(1)
        self.account_a.blockSignals(False)
        self.account_b.blockSignals(False)
        self._set_busy(self._busy)

    def _apply_overview(self, overview: dict[str, Any]) -> None:
        self._overview = overview
        accounts = [dict(item) for item in overview.get("accounts") or []]
        state_map = {
            int(item["telegram_account_id"]): item for item in accounts
        }
        active_count = int(overview.get("active_account_count") or 0)
        limit = int(overview.get("account_limit") or 40)
        self.limit_label.setText(f"Аккаунтов в прогреве: {active_count} из {limit}")

        self._render_pairs(
            [dict(item) for item in overview.get("pairs") or []], state_map
        )
        self._render_groups([dict(item) for item in overview.get("groups") or []])
        self._set_busy(self._busy)

    def _selected_pair(self) -> dict[str, Any] | None:
        selected = int(self._selected_pair_id or 0)
        for raw in self._overview.get("pairs") or []:
            pair = dict(raw)
            if int(pair.get("id") or 0) == selected:
                return pair
        return None

    def _pair_selected(self, _index: int = -1) -> None:
        selected = int(self.pair_selector.currentData() or 0)
        self._selected_pair_id = selected if selected > 0 else None
        if self._overview:
            self._apply_overview(dict(self._overview))

    @staticmethod
    def _assigned_group_accounts(group: dict[str, Any]) -> set[int]:
        raw = group.get("assigned_account_ids")
        if isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = str(raw or "").split(",")
        result: set[int] = set()
        for value in values:
            try:
                account_id = int(value or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if account_id > 0:
                result.add(account_id)
        return result

    def _render_groups(self, groups: list[dict[str, Any]]) -> None:
        self._clear_layout(self.groups_box)
        pair = self._selected_pair()
        if pair is None:
            empty = QLabel("Сначала создайте или выберите связку")
            empty.setObjectName("mutedText")
            self.groups_box.addWidget(empty)
            return
        account_names = {
            int(pair["account_a_id"]): str(pair.get("account_a_name") or "Аккаунт A"),
            int(pair["account_b_id"]): str(pair.get("account_b_name") or "Аккаунт B"),
        }
        assigned_rows = [
            (group, account_id)
            for group in groups
            for account_id in account_names
            if account_id in self._assigned_group_accounts(group)
        ]
        if not assigned_rows:
            empty = QLabel("Для выбранной связки группы ещё не добавлены")
            empty.setObjectName("mutedText")
            self.groups_box.addWidget(empty)
            return
        pair_id = int(pair["id"])
        expanded = pair_id in self._expanded_group_pairs
        visible_rows = (
            assigned_rows
            if expanded
            else assigned_rows[: self.COLLAPSED_GROUP_LIMIT]
        )
        for group, account_id in visible_rows:
            row_frame = QFrame()
            row_frame.setObjectName("statusCard")
            row = QHBoxLayout(row_frame)
            row.setContentsMargins(14, 10, 14, 10)
            text = QLabel(str(group.get("title") or group.get("chat_ref") or "Группа"))
            text.setObjectName("statusTitle")
            details = QLabel(f"Для аккаунта: {account_names[account_id]}")
            details.setObjectName("mutedText")
            remove = QPushButton("Удалить")
            remove.setObjectName("dangerButton")
            remove.clicked.connect(
                partial(self._remove_group, int(group["id"]), account_id)
            )
            row.addWidget(text, 1)
            row.addWidget(details)
            row.addWidget(remove)
            self.groups_box.addWidget(row_frame)
        hidden_count = max(0, len(assigned_rows) - self.COLLAPSED_GROUP_LIMIT)
        if hidden_count > 0:
            toggle = QPushButton(
                "Свернуть"
                if expanded
                else f"Показать ещё {hidden_count}"
            )
            toggle.setObjectName("secondaryButton")
            toggle.clicked.connect(
                partial(self._toggle_group_list, pair_id)
            )
            self.groups_box.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)

    def _toggle_group_list(self, pair_id: int) -> None:
        owner = int(pair_id)
        if owner in self._expanded_group_pairs:
            self._expanded_group_pairs.discard(owner)
        else:
            self._expanded_group_pairs.add(owner)
        self._render_groups(
            [dict(item) for item in self._overview.get("groups") or []]
        )

    def _proxy_widget(self, account: dict[str, Any]) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        proxy = dict(account.get("proxy") or {})
        toggle = QPushButton(
            "Прокси настроен ▸" if proxy.get("configured") else "Прокси не настроен ▸"
        )
        toggle.setCheckable(True)
        toggle.setObjectName("proxyToggle")
        details = QFrame()
        details.setObjectName("proxyCard")
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(12, 8, 12, 8)
        if proxy.get("configured"):
            value = (
                f"{proxy.get('type') or 'SOCKS5'} · "
                f"{proxy.get('host_masked') or '•••'}:{proxy.get('port') or '—'}"
            )
            if proxy.get("username_masked"):
                value += f" · логин {proxy['username_masked']}"
        else:
            value = "Рекомендуется настроить отдельный прокси во вкладке «Аккаунт»"
        label = QLabel(value)
        label.setObjectName("mutedText")
        label.setWordWrap(True)
        details_layout.addWidget(label)
        details.hide()

        def toggle_details(checked: bool) -> None:
            details.setVisible(checked)
            toggle.setText(
                ("Прокси настроен ▾" if proxy.get("configured") else "Прокси не настроен ▾")
                if checked
                else ("Прокси настроен ▸" if proxy.get("configured") else "Прокси не настроен ▸")
            )

        toggle.toggled.connect(toggle_details)
        layout.addWidget(toggle)
        layout.addWidget(details)
        return container

    def _pair_account_block(
        self,
        pair: dict[str, Any],
        account: dict[str, Any],
    ) -> QWidget:
        block = QFrame()
        block.setObjectName("accountManagerCard")
        layout = QVBoxLayout(block)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        title = QLabel(self._account_label(account))
        title.setObjectName("cardTitle")
        layout.addWidget(title)
        layout.addWidget(self._proxy_widget(account))
        status = str(account.get("warmup_status") or "available")
        if str(pair.get("status") or "") == "completed" and status == "completed":
            actions = QHBoxLayout()
            transfer = QPushButton("Аккаунт прогрет — перенести в кампанию")
            transfer.setObjectName("primaryButton")
            transfer.clicked.connect(
                partial(
                    self._transfer_account,
                    int(account["telegram_account_id"]),
                )
            )
            repartner = QPushButton("Связать с новым ещё на неделю")
            repartner.setObjectName("secondaryButton")
            repartner.clicked.connect(
                partial(
                    self._prepare_repair,
                    int(account["telegram_account_id"]),
                )
            )
            actions.addWidget(transfer)
            actions.addWidget(repartner)
            layout.addLayout(actions)
        return block

    def _render_pairs(
        self,
        pairs: list[dict[str, Any]],
        state_map: dict[int, dict[str, Any]],
    ) -> None:
        self._clear_layout(self.pairs_box)
        self._journal_views.clear()
        previous_pair_id = int(
            self._selected_pair_id or self.pair_selector.currentData() or 0
        )
        self.pair_selector.blockSignals(True)
        self.pair_selector.clear()
        for pair in pairs:
            pair_id = int(pair["id"])
            left = str(pair.get("account_a_name") or "Аккаунт A")
            right = str(pair.get("account_b_name") or "Аккаунт B")
            status = _STATUS_LABELS.get(
                str(pair.get("status") or ""), str(pair.get("status") or "")
            )
            self.pair_selector.addItem(
                f"Связка {pair_id}: {left} ↔ {right} · {status}", pair_id
            )
        pair_index = self.pair_selector.findData(previous_pair_id)
        self.pair_selector.setCurrentIndex(
            pair_index if pair_index >= 0 else (0 if pairs else -1)
        )
        selected_pair_id = int(self.pair_selector.currentData() or 0)
        self._selected_pair_id = selected_pair_id if selected_pair_id > 0 else None
        self.pair_selector.blockSignals(False)
        if not pairs:
            empty = QFrame()
            empty.setObjectName("card")
            layout = QVBoxLayout(empty)
            label = QLabel("Активных и завершённых связок пока нет")
            label.setObjectName("mutedText")
            layout.addWidget(label)
            self.pairs_box.addWidget(empty)
            return
        selected_pair = next(
            (
                pair
                for pair in pairs
                if int(pair.get("id") or 0) == selected_pair_id
            ),
            pairs[0],
        )
        self.load_groups_a_button.setText(
            f"Группы: {str(selected_pair.get('account_a_name') or 'Аккаунт A')[:24]}"
        )
        self.load_groups_b_button.setText(
            f"Группы: {str(selected_pair.get('account_b_name') or 'Аккаунт B')[:24]}"
        )
        for pair in (selected_pair,):
            pair_id = int(pair["id"])
            status = str(pair.get("status") or "")
            card = QFrame()
            card.setObjectName("card")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(20, 18, 20, 18)
            layout.setSpacing(12)

            header = QHBoxLayout()
            title = QLabel(
                f"{pair.get('account_a_name') or 'Аккаунт A'} ↔ "
                f"{pair.get('account_b_name') or 'Аккаунт B'}"
            )
            title.setObjectName("cardTitle")
            badge = QLabel(_STATUS_LABELS.get(status, status or "Связка"))
            badge.setObjectName("statusTitle")
            header.addWidget(title, 1)
            header.addWidget(badge)
            layout.addLayout(header)

            week = int(pair.get("week_number") or 1)
            progress = int(pair.get("progress_percent") or 0)
            summary = QLabel(
                f"Неделя {week} · шаг {int(pair.get('current_step') or 0)} "
                f"из {int(pair.get('total_steps') or 0)}"
            )
            summary.setObjectName("mutedText")
            progress_bar = QProgressBar()
            progress_bar.setRange(0, 100)
            progress_bar.setValue(progress)
            progress_bar.setTextVisible(True)
            layout.addWidget(summary)
            layout.addWidget(progress_bar)

            day_titles = list(pair.get("day_titles") or [])
            if day_titles:
                order = QLabel("Ротация дней: " + " → ".join(day_titles))
                order.setObjectName("mutedText")
                order.setWordWrap(True)
                layout.addWidget(order)
            profile = QLabel(
                f"Диалоговых окон: {int(pair.get('dialogue_windows') or 0)} · "
                f"ответы через {int(pair.get('reply_min_seconds') or 0) // 60}–"
                f"{int(pair.get('reply_max_seconds') or 0) // 60} мин · "
                f"Typing {int(pair.get('typing_min_seconds') or 0)}–"
                f"{int(pair.get('typing_max_seconds') or 0)} сек"
            )
            profile.setObjectName("mutedText")
            profile.setWordWrap(True)
            layout.addWidget(profile)
            layout.addWidget(self._journal_card(pair))

            accounts_row = QHBoxLayout()
            account_a = dict(state_map.get(int(pair["account_a_id"]), {}))
            account_b = dict(state_map.get(int(pair["account_b_id"]), {}))
            account_a.update(
                {
                    "telegram_account_id": int(pair["account_a_id"]),
                    "display_name": pair.get("account_a_name"),
                    "username": pair.get("account_a_username"),
                    "phone_masked": pair.get("account_a_phone"),
                }
            )
            account_b.update(
                {
                    "telegram_account_id": int(pair["account_b_id"]),
                    "display_name": pair.get("account_b_name"),
                    "username": pair.get("account_b_username"),
                    "phone_masked": pair.get("account_b_phone"),
                }
            )
            # Proxy summaries come from the current account overview.
            if int(pair["account_a_id"]) in state_map:
                account_a["proxy"] = state_map[int(pair["account_a_id"])].get("proxy")
            if int(pair["account_b_id"]) in state_map:
                account_b["proxy"] = state_map[int(pair["account_b_id"])].get("proxy")
            accounts_row.addWidget(self._pair_account_block(pair, account_a), 1)
            accounts_row.addWidget(self._pair_account_block(pair, account_b), 1)
            layout.addLayout(accounts_row)

            actions = QHBoxLayout()
            if status == "running":
                pause = QPushButton("Пауза")
                pause.setObjectName("secondaryButton")
                pause.clicked.connect(partial(self._pause_pair, pair_id))
                actions.addWidget(pause)
            elif status == "paused":
                uncertain_steps = int(pair.get("uncertain_steps") or 0)
                failed_steps = int(pair.get("failed_steps") or 0)
                if uncertain_steps <= 0:
                    resume = QPushButton(
                        "Повторить ошибочный шаг и продолжить"
                        if failed_steps > 0
                        else "Продолжить"
                    )
                    resume.setObjectName("primaryButton")
                    resume.clicked.connect(partial(self._resume_pair, pair_id))
                    actions.addWidget(resume)
                if uncertain_steps > 0:
                    uncertain_note = QLabel(
                        "Telegram не подтвердил результат. Автоматический повтор отключён."
                    )
                    uncertain_note.setObjectName("dangerText")
                    uncertain_note.setWordWrap(True)
                    actions.addWidget(uncertain_note, 1)
                archive = QPushButton(
                    "Завершить связку и освободить аккаунты"
                )
                archive.setObjectName("dangerButton")
                archive.clicked.connect(partial(self._archive_pair, pair_id))
                actions.addWidget(archive)
            elif status == "completed":
                both_completed = (
                    str(account_a.get("warmup_status") or "") == "completed"
                    and str(account_b.get("warmup_status") or "") == "completed"
                )
                if both_completed:
                    extend = QPushButton("Продлить эту связку ещё на 7 дней")
                    extend.setObjectName("secondaryButton")
                    extend.clicked.connect(partial(self._extend_pair, pair_id))
                    actions.addWidget(extend)
            actions.addStretch(1)
            layout.addLayout(actions)
            self.pairs_box.addWidget(card)

    @staticmethod
    def _journal_account(step: dict[str, Any], prefix: str) -> str:
        name = str(step.get(f"{prefix}_name") or "Telegram-аккаунт")
        username = str(step.get(f"{prefix}_username") or "").strip()
        return f"{name} @{username}" if username else name

    @classmethod
    def _journal_action(cls, step: dict[str, Any] | None) -> str:
        if not step:
            return "Действий больше нет"
        actor = cls._journal_account(step, "actor")
        target = cls._journal_account(step, "target")
        action = str(step.get("action") or "")
        if action == "ensure_contact":
            return f"{actor}: добавить {target} в контакты"
        if action == "message":
            typing = int(step.get("typing_seconds") or 0)
            message = " ".join(str(step.get("message_text") or "").split())
            preview = (
                f" · «{message[:90]}{'…' if len(message) > 90 else ''}»"
                if message
                else ""
            )
            return f"{actor}: сообщение для {target} · печатает {typing} сек{preview}"
        if action == "private_reaction":
            return f"{actor}: реакция на сообщение {target}"
        if action == "group_visit":
            posts = max(1, int(step.get("posts_to_read") or 1))
            reaction = " и поставить реакцию" if step.get("should_react") else ""
            return f"{actor}: посетить группу, прочитать {posts} поста{reaction}"
        return f"{actor}: {action or 'следующее действие'}"

    @staticmethod
    def _journal_reason(value: object) -> str:
        text = " ".join(str(value or "").split())
        lowered = text.casefold()
        if "not authorized" in lowered or "authorization_required" in lowered:
            return "Аккаунт не авторизован. Повторно войдите в Telegram во вкладке «Аккаунт»."
        if "network unavailable" in lowered or "connection failed" in lowered:
            return "Telegram-соединение было недоступно после трёх попыток."
        return text

    def _journal_card(self, pair: dict[str, Any]) -> QFrame:
        pair_id = int(pair["id"])
        activity = dict(pair.get("activity") or {})
        focus = dict(activity.get("focus") or {})
        upcoming = dict(activity.get("upcoming") or {})
        last = dict(activity.get("last") or {})

        card = QFrame()
        card.setObjectName("infoCard")
        journal = QVBoxLayout(card)
        journal.setContentsMargins(16, 13, 16, 13)
        journal.setSpacing(7)

        header = QHBoxLayout()
        title = QLabel("ЖИВОЙ ЖУРНАЛ")
        title.setObjectName("activityTitle")
        badge = QLabel()
        badge.setObjectName("activityBadge")
        header.addWidget(title)
        header.addWidget(badge)
        header.addStretch(1)
        journal.addLayout(header)

        current = QLabel()
        current.setObjectName("statusTitle")
        current.setWordWrap(True)
        countdown = QLabel()
        countdown.setObjectName("activityNext")
        countdown.setWordWrap(True)
        following = QLabel()
        following.setObjectName("mutedText")
        following.setWordWrap(True)
        previous = QLabel()
        previous.setObjectName("mutedText")
        previous.setWordWrap(True)
        reason = QLabel()
        reason.setObjectName("dangerText")
        reason.setWordWrap(True)

        journal.addWidget(current)
        journal.addWidget(countdown)
        journal.addWidget(following)
        journal.addWidget(previous)
        journal.addWidget(reason)
        self._journal_views[pair_id] = {
            "pair": dict(pair),
            "focus": focus,
            "upcoming": upcoming,
            "last": last,
            "badge": badge,
            "current": current,
            "countdown": countdown,
            "following": following,
            "previous": previous,
            "reason": reason,
        }
        self._update_journal_view(pair_id)
        return card

    def _update_journal_view(self, pair_id: int) -> None:
        view = self._journal_views.get(int(pair_id))
        if not view:
            return
        pair = dict(view["pair"])
        focus = dict(view["focus"])
        upcoming = dict(view["upcoming"])
        last = dict(view["last"])
        pair_status = str(pair.get("status") or "")
        step_status = str(focus.get("status") or "")

        if pair_status == "completed":
            badge_text = "Завершено"
            current_text = "Сейчас: недельный сценарий завершён"
        elif step_status == "running":
            badge_text = "Выполняется сейчас"
            current_text = "Сейчас: " + self._journal_action(focus)
        elif step_status == "failed":
            badge_text = "Ошибка — безопасный повтор"
            current_text = "Ошибочный шаг: " + self._journal_action(focus)
        elif step_status == "uncertain":
            badge_text = "Нужно решение"
            current_text = "Неподтверждённый шаг: " + self._journal_action(focus)
        elif pair_status == "paused":
            badge_text = "На паузе"
            current_text = "Сейчас: связка ожидает продолжения"
        else:
            badge_text = "Ожидание"
            current_text = "Следующее действие: " + self._journal_action(focus)
        view["badge"].setText(badge_text)
        view["current"].setText(current_text)

        deadline = focus.get("task_not_before") or focus.get("scheduled_at")
        if step_status == "running":
            countdown_text = "Таймер: действие выполняется сейчас"
        elif step_status == "failed":
            countdown_text = "Таймер: повтор начнётся только после нажатия кнопки"
        elif step_status == "uncertain":
            countdown_text = "Таймер: автоматический повтор отключён"
        elif pair_status == "paused":
            countdown_text = "Таймер остановлен до продолжения связки"
        elif focus:
            countdown_text = countdown_label(
                "До следующего действия",
                deadline,
                include_deadline=True,
                include_date=True,
                due_text="готовится к запуску…",
            )
        else:
            countdown_text = "Следующих действий нет"
        view["countdown"].setText(countdown_text)

        view["following"].setText(
            "После него: " + self._journal_action(upcoming)
            if upcoming
            else "После него: действий пока нет"
        )
        if last:
            completed_at = from_db_time(last.get("completed_at"))
            completed = (
                completed_at.astimezone().strftime("%d.%m %H:%M:%S")
                if completed_at is not None
                else "время не записано"
            )
            view["previous"].setText(
                f"Последнее выполненное: {self._journal_action(last)} · {completed}"
            )
        else:
            view["previous"].setText("Последнее выполненное: действий ещё не было")

        reason = self._journal_reason(
            focus.get("result_text")
            or focus.get("task_error")
            or pair.get("last_error")
        )
        view["reason"].setText(f"Причина остановки: {reason}" if reason else "")
        view["reason"].setVisible(bool(reason) and pair_status == "paused")

    def _update_journal_countdowns(self) -> None:
        for pair_id in tuple(self._journal_views):
            self._update_journal_view(pair_id)

    def _create_pair(self) -> None:
        account_a = int(self.account_a.currentData() or 0)
        account_b = int(self.account_b.currentData() or 0)
        if account_a <= 0 or account_b <= 0 or account_a == account_b:
            QMessageBox.warning(self, "Прогрев", "Выберите два разных аккаунта")
            return
        self._run(lambda: self.adapter.create_warmup_pair(account_a, account_b))

    def _load_pair_groups(self, side: str) -> None:
        pair = self._selected_pair()
        if pair is None:
            QMessageBox.warning(self, "Прогрев", "Сначала выберите связку")
            return
        key = "account_a_id" if side == "a" else "account_b_id"
        name_key = "account_a_name" if side == "a" else "account_b_name"
        account_id = int(pair.get(key) or 0)
        account_name = str(pair.get(name_key) or ("Аккаунт A" if side == "a" else "Аккаунт B"))
        self._load_synced_groups(account_id, account_name)

    def _load_synced_groups(self, account_id: int, account_name: str) -> None:
        if account_id <= 0:
            QMessageBox.warning(
                self,
                "Прогрев",
                "Сначала выберите подключённый Telegram-аккаунт",
            )
            return
        self.groups_status.setText(
            f"Подбираем 3–4 группы для аккаунта {account_name}…"
        )

        def applied(value: Any) -> None:
            result = dict(value or {})
            selected = int(result.get("selected_count") or 0)
            available = int(result.get("candidate_count") or 0)
            message = str(result.get("message") or "").strip()
            if message:
                self.groups_status.setText(message)
            elif bool(result.get("limited")):
                self.groups_status.setText(
                    f"{account_name}: найдено только {available}; добавлено: {selected}"
                )
            else:
                self.groups_status.setText(
                    f"{account_name}: подобрано {selected} · доступно: {available}"
                )

        self._run(
            lambda: self.adapter.populate_warmup_groups_from_synced(account_id),
            success=applied,
        )

    def _add_group(self) -> None:
        pair = self._selected_pair()
        if pair is None:
            QMessageBox.warning(self, "Прогрев", "Сначала выберите связку")
            return
        choices = [
            (
                str(pair.get("account_a_name") or "Аккаунт A"),
                int(pair["account_a_id"]),
            ),
            (
                str(pair.get("account_b_name") or "Аккаунт B"),
                int(pair["account_b_id"]),
            ),
        ]
        account_name, account_selected = QInputDialog.getItem(
            self,
            "Аккаунт группы",
            "Для какого аккаунта добавить группу:",
            [name for name, _account_id in choices],
            0,
            False,
        )
        if not account_selected:
            return
        account_id = next(
            account_id for name, account_id in choices if name == account_name
        )
        value, accepted = QInputDialog.getText(
            self,
            "Добавить группу для прогрева",
            "Ссылка Telegram или @username:",
        )
        if accepted and str(value).strip():
            self._run(
                lambda: self.adapter.add_warmup_group(
                    str(value).strip(), account_id
                )
            )

    def _remove_group(self, group_id: int, account_id: int) -> None:
        if QMessageBox.question(
            self,
            "Удалить группу",
            "Удалить группу только из списка этого аккаунта?",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run(lambda: self.adapter.remove_warmup_group(group_id, account_id))

    def _pause_pair(self, pair_id: int) -> None:
        self._run(lambda: self.adapter.pause_warmup_pair(pair_id))

    def _resume_pair(self, pair_id: int) -> None:
        def applied(changed: Any) -> None:
            if not bool(changed):
                QMessageBox.warning(
                    self,
                    "Прогрев",
                    "Связку нельзя продолжить автоматически: проверьте шаг с неизвестным результатом.",
                )

        self._run(
            lambda: self.adapter.resume_warmup_pair(pair_id),
            success=applied,
        )

    def _retry_pair(self, pair_id: int) -> None:
        self._run(lambda: self.adapter.retry_failed_warmup_pair(pair_id))

    def _archive_pair(self, pair_id: int) -> None:
        if QMessageBox.question(
            self,
            "Завершить связку",
            "Архивировать приостановленную связку и освободить оба аккаунта? "
            "Невыполненные шаги будут отменены; действия с неизвестным "
            "результатом не будут повторены.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run(lambda: self.adapter.archive_paused_warmup_pair(pair_id))

    def _extend_pair(self, pair_id: int) -> None:
        self._run(lambda: self.adapter.extend_warmup_pair(pair_id))

    def _transfer_account(self, account_id: int) -> None:
        if QMessageBox.question(
            self,
            "Перенос в кампанию",
            "Завершить прогрев этого аккаунта и разблокировать его для кампаний? "
            "Telegram-сессия и закреплённый прокси сохранятся.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run(lambda: self.adapter.transfer_warmup_account(account_id))

    def _prepare_repair(self, account_id: int) -> None:
        index = self.account_a.findData(account_id)
        if index >= 0:
            self.account_a.setCurrentIndex(index)
        for candidate in range(self.account_b.count()):
            if int(self.account_b.itemData(candidate) or 0) != account_id:
                self.account_b.setCurrentIndex(candidate)
                break
        self.account_b.setFocus(Qt.FocusReason.OtherFocusReason)

    def handle_account_changed(self, _account_id: int | None = None) -> None:
        self.refresh(force=True)

    def set_page_active(self, active: bool) -> None:
        self._page_active = bool(active)
        if active:
            self.refresh_timer.start()
            self.journal_timer.start()
            self.refresh(force=True)
        else:
            self.refresh_timer.stop()
            self.journal_timer.stop()

    def set_compact_mode(self, compact: bool) -> None:
        self._compact = bool(compact)
        margin = 18 if compact else 30
        layout = self.layout()
        if layout is not None:
            layout.setContentsMargins(margin, 22, margin, 26)
    def shutdown(self) -> None:
        self.refresh_timer.stop()
        self.journal_timer.stop()

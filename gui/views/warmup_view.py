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

from gui.background import BackgroundCall, connect_lifecycle_safe


_STATUS_LABELS = {
    "running": "Прогрев выполняется",
    "paused": "Прогрев на паузе",
    "completed": "Прогрев завершён",
    "archived": "Связка завершена",
}


class WarmupView(QWidget):
    """Managed seven-day account-pair workflows with native controls."""

    def __init__(self, adapter) -> None:
        super().__init__()
        self.adapter = adapter
        self._overview: dict[str, Any] = {}
        self._busy = False
        self._page_active = False
        self._compact = False
        self._refresh_in_flight = False
        self._refresh_pending = False

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
        root.addWidget(account_card)

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
        self.account_b = QComboBox()
        self.account_b.setMinimumWidth(220)
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
        self.add_group_button = QPushButton("Добавить группу для прогрева")
        self.add_group_button.setObjectName("secondaryButton")
        self.add_group_button.clicked.connect(self._add_group)
        groups_header.addWidget(groups_title)
        groups_header.addStretch(1)
        groups_header.addWidget(self.add_group_button)
        groups_layout.addLayout(groups_header)
        groups_hint = QLabel(
            "Добавляются только указанные вами Telegram-группы. Частоту посещений, "
            "число читаемых постов и реакций программа задаёт автоматически."
        )
        groups_hint.setObjectName("mutedText")
        groups_hint.setWordWrap(True)
        groups_layout.addWidget(groups_hint)
        self.groups_box = QVBoxLayout()
        self.groups_box.setSpacing(8)
        groups_layout.addLayout(self.groups_box)
        root.addWidget(groups_card)

        pairs_title = QLabel("Связки аккаунтов")
        pairs_title.setObjectName("sectionTitle")
        root.addWidget(pairs_title)
        self.pairs_box = QVBoxLayout()
        self.pairs_box.setSpacing(14)
        root.addLayout(self.pairs_box)
        root.addStretch(1)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(5_000)
        self.refresh_timer.timeout.connect(self.refresh)
        QTimer.singleShot(0, self.refresh)

    def _existing_account_selected(self, _index: int = -1) -> None:
        account_id = int(self.existing_account_selector.currentData() or 0)
        if account_id <= 0:
            return
        target = self.account_a.findData(account_id)
        if target >= 0:
            self.account_a.setCurrentIndex(target)
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

    def _set_busy(self, active: bool) -> None:
        self._busy = bool(active)
        self.create_button.setEnabled(not active)
        self.add_group_button.setEnabled(not active)

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
            if refresh_after:
                owner.refresh(force=True)

        def failed(owner: "WarmupView", message: str) -> None:
            QMessageBox.warning(owner, "Прогрев", message)

        def finished(owner: "WarmupView") -> None:
            owner._set_busy(False)

        connect_lifecycle_safe(
            job,
            self,
            succeeded=succeeded,
            failed=failed,
            finished=finished,
        )
        QThreadPool.globalInstance().start(job)

    def refresh(self, *, force: bool = False) -> None:
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
        QThreadPool.globalInstance().start(job)

    def _apply_overview(self, overview: dict[str, Any]) -> None:
        self._overview = overview
        accounts = [dict(item) for item in overview.get("accounts") or []]
        state_map = {
            int(item["telegram_account_id"]): item for item in accounts
        }
        previous_existing = self.existing_account_selector.currentData()
        self.existing_account_selector.blockSignals(True)
        self.existing_account_selector.clear()
        connected = [item for item in accounts if item.get("authorized")]
        for account in connected:
            self.existing_account_selector.addItem(
                self._account_label(account),
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
        self.account_a.clear()
        self.account_b.clear()
        # Selection and execution eligibility are different concerns. A running
        # campaign or missing proxy may make create_warmup_pair() reject the
        # operation, but the connected account must still be assignable in A/B
        # so the UI does not silently ignore the user's selection. Accounts that
        # are stopped or already belong to an active warmup pair remain excluded.
        selectable = [
            item
            for item in accounts
            if item.get("authorized")
            and not item.get("stopped")
            and item.get("active_pair_id") is None
        ]
        for account in selectable:
            account_id = int(account["telegram_account_id"])
            label = self._account_label(account)
            self.account_a.addItem(label, account_id)
            self.account_b.addItem(label, account_id)
        for combo, previous in ((self.account_a, selected_a), (self.account_b, selected_b)):
            index = combo.findData(previous)
            if index >= 0:
                combo.setCurrentIndex(index)
        if (
            self.account_b.count() > 1
            and self.account_b.currentData() == self.account_a.currentData()
        ):
            self.account_b.setCurrentIndex(1)
        self._existing_account_selected()
        active_count = int(overview.get("active_account_count") or 0)
        limit = int(overview.get("account_limit") or 40)
        self.limit_label.setText(f"Аккаунтов в прогреве: {active_count} из {limit}")
        self.create_button.setEnabled(not self._busy and len(selectable) >= 2)

        self._render_groups([dict(item) for item in overview.get("groups") or []])
        self._render_pairs(
            [dict(item) for item in overview.get("pairs") or []], state_map
        )

    def _render_groups(self, groups: list[dict[str, Any]]) -> None:
        self._clear_layout(self.groups_box)
        if not groups:
            empty = QLabel("Группы ещё не добавлены")
            empty.setObjectName("mutedText")
            self.groups_box.addWidget(empty)
            return
        for group in groups:
            row_frame = QFrame()
            row_frame.setObjectName("statusCard")
            row = QHBoxLayout(row_frame)
            row.setContentsMargins(14, 10, 14, 10)
            text = QLabel(str(group.get("title") or group.get("chat_ref") or "Группа"))
            text.setObjectName("statusTitle")
            details = QLabel(
                f"Состоят аккаунтов: {int(group.get('joined_count') or 0)}"
            )
            details.setObjectName("mutedText")
            remove = QPushButton("Удалить")
            remove.setObjectName("dangerButton")
            remove.clicked.connect(
                partial(self._remove_group, int(group["id"]))
            )
            row.addWidget(text, 1)
            row.addWidget(details)
            row.addWidget(remove)
            self.groups_box.addWidget(row_frame)

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
            value = "Настройте отдельный прокси во вкладке «Аккаунт»"
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
        if not pairs:
            empty = QFrame()
            empty.setObjectName("card")
            layout = QVBoxLayout(empty)
            label = QLabel("Активных и завершённых связок пока нет")
            label.setObjectName("mutedText")
            layout.addWidget(label)
            self.pairs_box.addWidget(empty)
            return
        for pair in pairs:
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
            if pair.get("last_error"):
                error = QLabel(str(pair["last_error"]))
                error.setObjectName("dangerText")
                error.setWordWrap(True)
                layout.addWidget(error)

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
                    resume = QPushButton("Продолжить")
                    resume.setObjectName("primaryButton")
                    resume.clicked.connect(partial(self._resume_pair, pair_id))
                    actions.addWidget(resume)
                if failed_steps > 0 and uncertain_steps <= 0:
                    retry = QPushButton("Повторить безопасно завершившийся шаг")
                    retry.setObjectName("secondaryButton")
                    retry.clicked.connect(partial(self._retry_pair, pair_id))
                    actions.addWidget(retry)
                if uncertain_steps > 0:
                    uncertain_note = QLabel(
                        "Telegram не подтвердил результат. Автоматический повтор отключён."
                    )
                    uncertain_note.setObjectName("dangerText")
                    uncertain_note.setWordWrap(True)
                    actions.addWidget(uncertain_note, 1)
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

    def _create_pair(self) -> None:
        account_a = int(self.account_a.currentData() or 0)
        account_b = int(self.account_b.currentData() or 0)
        if account_a <= 0 or account_b <= 0 or account_a == account_b:
            QMessageBox.warning(self, "Прогрев", "Выберите два разных аккаунта")
            return
        self._run(lambda: self.adapter.create_warmup_pair(account_a, account_b))

    def _add_group(self) -> None:
        value, accepted = QInputDialog.getText(
            self,
            "Добавить группу для прогрева",
            "Ссылка Telegram или @username:",
        )
        if accepted and str(value).strip():
            self._run(lambda: self.adapter.add_warmup_group(str(value).strip()))

    def _remove_group(self, group_id: int) -> None:
        if QMessageBox.question(
            self,
            "Удалить группу",
            "Удалить группу из списка прогрева?",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run(lambda: self.adapter.remove_warmup_group(group_id))

    def _pause_pair(self, pair_id: int) -> None:
        self._run(lambda: self.adapter.pause_warmup_pair(pair_id))

    def _resume_pair(self, pair_id: int) -> None:
        self._run(lambda: self.adapter.resume_warmup_pair(pair_id))

    def _retry_pair(self, pair_id: int) -> None:
        self._run(lambda: self.adapter.retry_failed_warmup_pair(pair_id))

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
            self.refresh(force=True)
        else:
            self.refresh_timer.stop()

    def set_compact_mode(self, compact: bool) -> None:
        self._compact = bool(compact)
        margin = 18 if compact else 30
        layout = self.layout()
        if layout is not None:
            layout.setContentsMargins(margin, 22, margin, 26)
    def shutdown(self) -> None:
        self.refresh_timer.stop()

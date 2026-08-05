from __future__ import annotations

import logging
from typing import Any, cast

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
)

from core.version import APP_NAME
from gui.account_manager_panel import format_account_identity
from gui.dialogs import AccountSourceDialog, DangerConfirmDialog, FactoryResetDialog
from gui.views.account_view import AccountView
from services.account_import_service import AccountImportService


log = logging.getLogger(__name__)


class PremiumAccountView(AccountView):
    """Account page presentation fixes without replacing authorization internals."""

    link_recheck_requested = Signal(bool)
    QUIET_BLOCK_SETTINGS_KEY = "ui/account/quiet_schedule_expanded"

    def __init__(self, adapter, config):
        self._premium_ready = False
        self._catalog_snapshot: list[dict] = []
        self._catalog_by_id: dict[int, dict] = {}
        super().__init__(adapter, config)
        self._install_selected_account_card()
        self._install_collapsible_schedule()
        self.status_card.hide()  # dynamic operational text belongs to the live journal
        self.root_layout.setContentsMargins(24, 12, 24, 18)
        self.root_layout.setSpacing(10)
        self._premium_ready = True
        self._load_account_catalog()

    def _install_selected_account_card(self) -> None:
        self.selected_account_card = QFrame()
        self.selected_account_card.setObjectName("selectedAccountCard")
        row = QHBoxLayout(self.selected_account_card)
        row.setContentsMargins(16, 12, 12, 12)
        row.setSpacing(12)
        self.selected_account_identity = QLabel("Telegram-аккаунт не выбран")
        self.selected_account_identity.setObjectName("selectedAccountIdentity")
        self.selected_account_identity.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.selected_account_identity.setAccessibleName("Выбранный Telegram-аккаунт")
        self.selected_account_delete = QPushButton("🗑")
        self.selected_account_delete.setObjectName("accountDeleteButton")
        self.selected_account_delete.setToolTip("Удалить выбранный Telegram-аккаунт")
        self.selected_account_delete.setAccessibleName(
            "Удалить выбранный Telegram-аккаунт"
        )
        self.selected_account_delete.clicked.connect(self._delete_visible_account)
        row.addWidget(self.selected_account_identity, 1)
        row.addWidget(self.selected_account_delete, 0)
        self.root_layout.insertWidget(2, self.selected_account_card)
        self._render_selected_identity()

    def _install_collapsible_schedule(self) -> None:
        position = self.form_layout.indexOf(self.schedule_enabled)
        row = 0
        if position >= 0:
            position_info = cast(
                tuple[int, ...], self.form_layout.getItemPosition(position)
            )
            row = int(position_info[0])
        self.schedule_toggle = QPushButton()
        self.schedule_toggle.setObjectName("secondaryButton")
        self.schedule_toggle.setCheckable(True)
        self.schedule_toggle.setAccessibleName("Расписание тишины")
        self.schedule_toggle.toggled.connect(self._set_schedule_expanded)
        self.form_layout.insertRow(row, self.schedule_toggle)
        expanded = QSettings().value(
            self.QUIET_BLOCK_SETTINGS_KEY, False, type=bool
        )
        self.schedule_toggle.setChecked(bool(expanded))
        self._set_schedule_expanded(bool(expanded))

    def _set_schedule_expanded(self, expanded: bool) -> None:
        visible = bool(expanded)
        self.schedule_toggle.setText(
            ("⌄" if visible else "›") + "  🌙 Расписание тишины"
        )
        self.schedule_enabled.setVisible(visible)
        self.schedule_box.setVisible(visible)
        QSettings().setValue(self.QUIET_BLOCK_SETTINGS_KEY, visible)
        if hasattr(self, "root_layout"):
            self._refresh_dynamic_layout()


    def _toggle_schedule(self, enabled: bool) -> None:
        super()._toggle_schedule(enabled)
        if hasattr(self, "schedule_toggle") and not self.schedule_toggle.isChecked():
            self.schedule_box.hide()

    def _render_selected_identity(self) -> None:
        if not hasattr(self, "selected_account_identity"):
            return
        selected = int(self.adapter.get_selected_account_id() or 0)
        account = self._catalog_by_id.get(selected)
        self.selected_account_identity.setText(format_account_identity(selected, account))
        self.selected_account_delete.setEnabled(selected > 0 and account is not None)

    @staticmethod
    def _comment_profile_count(profile: Any) -> int:
        if not isinstance(profile, dict):
            return 0
        comments = profile.get("comments")
        if isinstance(comments, list):
            return sum(1 for item in comments if str(item or "").strip())
        groups = profile.get("groups") or profile.get("sets")
        if isinstance(groups, list):
            count = 0
            for group in groups:
                if isinstance(group, dict):
                    values = group.get("comments") or group.get("items") or []
                    if isinstance(values, list):
                        count += sum(1 for item in values if str(item or "").strip())
            return count
        return 0

    def _source_counts(self, accounts: list[dict]) -> tuple[dict[int, int], dict[int, int]]:
        comment_counts: dict[int, int] = {}
        channel_counts: dict[int, int] = {}
        selected = int(self.adapter.get_selected_account_id() or 0)
        for row in accounts:
            account_id = int(row.get("telegram_account_id") or row.get("id") or 0)
            if account_id <= 0 or account_id == selected:
                continue
            try:
                profile = self.adapter.get_comment_profile(account_id=account_id)
                comment_counts[account_id] = self._comment_profile_count(profile)
            except Exception:
                comment_counts[account_id] = 0
            try:
                channels = self.adapter.get_channels(account_id=account_id) or []
                channel_counts[account_id] = len(list(channels))
            except Exception:
                channel_counts[account_id] = 0
        return comment_counts, channel_counts

    def _load_account_catalog(self) -> None:
        if not self._premium_ready:
            AccountView._load_account_catalog(self)
            return
        self._account_catalog_generation += 1
        generation = self._account_catalog_generation

        def load() -> tuple[list[dict], dict[int, int], dict[int, int]]:
            accounts = list(self.adapter.list_telegram_accounts() or [])
            comments, channels = self._source_counts(accounts)
            return accounts, comments, channels

        def applied(payload) -> None:
            if generation != self._account_catalog_generation:
                return
            accounts, comments, channels = payload
            self._catalog_snapshot = list(accounts)
            self._catalog_by_id = {
                int(row.get("telegram_account_id") or row.get("id") or 0): dict(row)
                for row in accounts
                if int(row.get("telegram_account_id") or row.get("id") or 0) > 0
            }
            selected = int(self.adapter.get_selected_account_id() or 0)
            previous = int(self.adapter.get_previous_selected_account_id() or 0)
            self.account_manager.reload(
                accounts,
                selected_account_id=selected,
                previous_account_id=previous,
            )
            self.account_manager.set_data_counts(
                comment_counts=comments, channel_counts=channels
            )
            self._render_selected_identity()

        def failed(message: str) -> None:
            if generation != self._account_catalog_generation:
                return
            self._catalog_snapshot = []
            self._catalog_by_id = {}
            self._render_selected_identity()
            self._write_activity("ERROR", f"Не удалось обновить список аккаунтов: {message}")

        self._run_background(load, on_success=applied, on_error=failed)

    def _finish_account_selection(self) -> None:
        super()._finish_account_selection()
        if self._premium_ready:
            self._render_selected_identity()

    def _set_authorized_ui(self, values) -> None:
        super()._set_authorized_ui(values)
        if self._premium_ready:
            self._render_selected_identity()

    def _write_activity(self, level: str, message: str) -> None:
        try:
            database = self.adapter.api.database
            database.insert_log(
                str(level).upper(),
                str(message),
                account_id=int(self.adapter.get_selected_account_id() or 0),
            )
        except Exception:
            log.exception("Could not persist account-page activity")

    def _delete_visible_account(self) -> None:
        selected = int(self.adapter.get_selected_account_id() or 0)
        manager_selected = self.account_manager.selected_account_id()
        if selected <= 0 or manager_selected != selected:
            QMessageBox.warning(
                self,
                "Удаление аккаунта",
                "Выберите аккаунт явно и дождитесь завершения переключения.",
            )
            return
        identity = self.account_manager.selected_identity()
        dialog = DangerConfirmDialog(
            "Удалить Telegram-аккаунт?",
            f"Будет удалён только явно выбранный аккаунт «{identity}» и все его "
            "локальные данные. Другие аккаунты и их кампании не изменятся.",
            confirm_text="Удалить аккаунт",
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._delete_account_without_second_prompt(selected)

    def _delete_account_without_second_prompt(self, owner: int) -> None:
        if self.auth_worker is not None and self.auth_worker.isRunning():
            QMessageBox.warning(self, APP_NAME, "Авторизация уже выполняется")
            return
        if self._account_blocking_jobs:
            QMessageBox.warning(
                self, APP_NAME, "Дождитесь завершения операции с Telegram-аккаунтом"
            )
            return
        self.selected_account_delete.setEnabled(False)
        self.selected_account_delete.setProperty("busy", True)
        self.selected_account_delete.style().polish(self.selected_account_delete)
        self._write_activity("INFO", f"Удаление выбранного Telegram-аккаунта {owner} запущено")

        def deleted(result) -> None:
            self.selected_account_delete.setProperty("busy", False)
            self._adding_account = False
            self._reauthorizing_account_id = 0
            self._pending_session_name = ""
            self._auth_settings_snapshot = {}
            self._set_code_card_visible(False)
            self.load_settings()
            self.account_changed.emit()
            self._write_activity("INFO", "Telegram-аккаунт и его локальные данные удалены")
            QMessageBox.information(
                self,
                "Аккаунт удалён",
                str(result.get("message") or "Локальные данные аккаунта удалены."),
            )

        def failed(message: str) -> None:
            self.selected_account_delete.setProperty("busy", False)
            self._render_selected_identity()
            self._write_activity("ERROR", f"Удаление аккаунта не выполнено: {message}")
            QMessageBox.warning(self, "Удаление аккаунта", message)

        self._run_background(
            lambda: self.adapter.delete_telegram_account(owner),
            on_success=deleted,
            on_error=failed,
            blocks_account_change=True,
        )

    def _import_between_accounts(
        self,
        kind: str,
        source: int,
        target: int,
        *,
        mode: str = "replace",
    ):
        if source <= 0 or target <= 0 or source == target:
            raise ValueError("Нельзя импортировать данные аккаунта в него же")
        importer = AccountImportService(self.adapter.api.database)
        if kind == "comments":
            return importer.import_comments(
                source_account_id=source,
                target_account_id=target,
                mode=mode,
            )
        return importer.import_channels(
            source_account_id=source,
            target_account_id=target,
        )

    def _choose_source(self, kind: str) -> tuple[int, int] | None:
        sources = self.account_manager.source_accounts(kind)
        if not sources:
            QMessageBox.information(
                self,
                "Импорт недоступен",
                "Нет другого аккаунта с данными для импорта",
            )
            return None
        dialog = AccountSourceDialog(kind, sources, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        source = dialog.selected_account_id()
        target = int(self.adapter.get_selected_account_id() or 0)
        selected = dialog.selected_source() or {}
        count = int(selected.get("import_count") or 0)
        noun = "каналов" if kind == "channels" else "элементов конфигурации"
        confirmation = QMessageBox.question(
            self,
            "Подтвердите импорт",
            f"Импортировать {count} {noun} из выбранного аккаунта в текущий?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return (source, target) if confirmation == QMessageBox.StandardButton.Yes else None

    def _import_comments_from_previous(self) -> None:
        pair = self._choose_source("comments")
        if pair is None:
            return
        source, target = pair
        self.account_manager.import_comments_button.setEnabled(False)
        self.account_manager.import_comments_button.setProperty("busy", True)
        self._write_activity(
            "INFO",
            f"Импорт конфигурации комментариев из аккаунта {source} запущен",
        )

        def imported(result) -> None:
            self.account_manager.import_comments_button.setProperty("busy", False)
            self._load_account_catalog()
            count = int(result.get("imported") or result.get("changed") or 0)
            self._write_activity("INFO", f"Импорт комментариев завершён: изменено {count}")
            QMessageBox.information(
                self, "Импорт завершён", f"Изменено элементов конфигурации: {count}"
            )

        self._run_background(
            lambda: self._import_between_accounts(
                "comments", source, target, mode="replace"
            ),
            on_success=imported,
            on_error=lambda message: self._import_failed("комментариев", message),
        )

    def _import_channels_from_previous(self) -> None:
        pair = self._choose_source("channels")
        if pair is None:
            return
        source, target = pair
        self.account_manager.import_channels_button.setEnabled(False)
        self.account_manager.import_channels_button.setProperty("busy", True)
        self._write_activity("INFO", f"Импорт каналов из аккаунта {source} запущен")

        def imported(result) -> None:
            self.account_manager.import_channels_button.setProperty("busy", False)
            self._load_account_catalog()
            imported_count = int(result.get("imported") or 0)
            existing = int(result.get("existing") or 0)
            skipped = int(result.get("skipped") or 0)
            self._write_activity(
                "INFO",
                "Импорт каналов завершён: "
                f"импортировано {imported_count}, существовало {existing}, пропущено {skipped}",
            )
            answer = QMessageBox.question(
                self,
                "Импорт каналов завершён",
                "Импортировано: {imported}\nУже существовали: {existing}\n"
                "Пропущено: {skipped}\n\nЗапустить проверку связок для нового аккаунта?".format(
                    imported=imported_count, existing=existing, skipped=skipped
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.link_recheck_requested.emit(True)

        self._run_background(
            lambda: self._import_between_accounts("channels", source, target),
            on_success=imported,
            on_error=lambda message: self._import_failed("каналов", message),
        )

    def _import_failed(self, kind: str, message: str) -> None:
        self.account_manager.import_comments_button.setProperty("busy", False)
        self.account_manager.import_channels_button.setProperty("busy", False)
        self._load_account_catalog()
        self._write_activity("ERROR", f"Импорт {kind} не выполнен: {message}")
        QMessageBox.warning(self, f"Импорт {kind}", message)

    def reset_database(self) -> None:
        dialog = FactoryResetDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.set_factory_reset_pending(True)
        self._write_activity("WARNING", "Подтверждён заводской сброс локальных данных")
        QMessageBox.information(
            self,
            "Заводской сброс",
            "Программа остановит фоновые задачи, безопасно удалит локальные данные "
            "и закроется. Следующий запуск будет чистым.",
        )
        self.factory_reset_requested.emit()

    def set_compact_mode(self, compact: bool) -> None:
        super().set_compact_mode(compact)
        self.root_layout.setContentsMargins(
            16 if compact else 24,
            8 if compact else 12,
            16 if compact else 24,
            14 if compact else 18,
        )
        self.root_layout.setSpacing(8 if compact else 10)

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from gui.account_manager_panel import format_account_identity


class DangerConfirmDialog(QDialog):
    """Readable app-themed confirmation for destructive account operations."""

    def __init__(self, title: str, message: str, *, confirm_text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("dangerConfirmDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(520)

        heading = QLabel(title)
        heading.setObjectName("dangerTitle")
        warning = QLabel(message)
        warning.setObjectName("dialogText")
        warning.setWordWrap(True)

        self.buttons = QDialogButtonBox()
        self.confirm_button = QPushButton(confirm_text)
        self.confirm_button.setObjectName("dangerButton")
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setObjectName("secondaryButton")
        self.buttons.addButton(self.confirm_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.addButton(self.cancel_button, QDialogButtonBox.ButtonRole.RejectRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        layout.addWidget(heading)
        layout.addWidget(warning)
        layout.addWidget(self.buttons)


class FactoryResetDialog(QDialog):
    CONTROL_WORD = "СБРОСИТЬ"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("factoryResetDialog")
        self.setWindowTitle("Подтверждение заводского сброса")
        self.setModal(True)
        self.setMinimumWidth(560)

        heading = QLabel("Безвозвратный заводской сброс")
        heading.setObjectName("dangerTitle")
        warning = QLabel(
            "Будут удалены все локальные аккаунты, Telegram-сессии, прокси/API-данные, "
            "каналы, связки, кампании, история, ledger и настройки. Отменить действие "
            "после запуска нельзя."
        )
        warning.setObjectName("dialogText")
        warning.setWordWrap(True)
        instruction = QLabel(f"Для подтверждения введите слово {self.CONTROL_WORD}:")
        instruction.setObjectName("cardTitle")

        self.confirmation = QLineEdit()
        self.confirmation.setObjectName("factoryResetConfirmation")
        self.confirmation.setPlaceholderText(self.CONTROL_WORD)
        self.confirmation.setAccessibleName("Контрольное слово заводского сброса")
        self.confirmation.textChanged.connect(self._sync_button)

        self.reset_button = QPushButton("Сбросить")
        self.reset_button.setObjectName("dangerButton")
        self.reset_button.setEnabled(False)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setObjectName("secondaryButton")
        self.reset_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.reset_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        layout.addWidget(heading)
        layout.addWidget(warning)
        layout.addWidget(instruction)
        layout.addWidget(self.confirmation)
        layout.addLayout(actions)

    def _sync_button(self, text: str) -> None:
        self.reset_button.setEnabled(text.strip().upper() == self.CONTROL_WORD)


class AccountSourceDialog(QDialog):
    """Explicit source-account chooser for configuration-only imports."""

    def __init__(self, kind: str, sources: list[dict], parent=None):
        super().__init__(parent)
        self.kind = str(kind)
        self.sources = list(sources)
        object_name = "Комментарии" if self.kind == "comments" else "Каналы"
        self.setObjectName("accountSourceDialog")
        self.setWindowTitle(f"Импорт: {object_name.lower()}")
        self.setModal(True)
        self.setMinimumWidth(600)

        title = QLabel(f"Выберите аккаунт-источник: {object_name.lower()}")
        title.setObjectName("cardTitle")
        explanation = QLabel(
            "Импортируется только конфигурация. История, delivery ledger, результаты "
            "кампаний, ограничения, сессии и секреты не переносятся."
        )
        explanation.setObjectName("dialogText")
        explanation.setWordWrap(True)

        self.selector = QComboBox()
        self.selector.setObjectName("accountImportSource")
        for source in self.sources:
            account_id = int(source.get("telegram_account_id") or 0)
            count = int(source.get("import_count") or 0)
            identity = format_account_identity(account_id, source)
            suffix = "объектов" if self.kind == "channels" else "элементов"
            self.selector.addItem(f"{identity} · {count} {suffix}", account_id)

        self.summary = QLabel()
        self.summary.setObjectName("mutedText")
        self.summary.setWordWrap(True)
        self.selector.currentIndexChanged.connect(self._render_summary)

        self.import_button = QPushButton("Продолжить")
        self.import_button.setObjectName("primaryButton")
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setObjectName("secondaryButton")
        self.import_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.import_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(explanation)
        layout.addWidget(self.selector)
        layout.addWidget(self.summary)
        layout.addLayout(actions)
        self._render_summary()

    def selected_account_id(self) -> int:
        return max(0, int(self.selector.currentData() or 0))

    def selected_source(self) -> dict | None:
        selected = self.selected_account_id()
        return next(
            (
                dict(row)
                for row in self.sources
                if int(row.get("telegram_account_id") or 0) == selected
            ),
            None,
        )

    def _render_summary(self, _index: int = -1) -> None:
        source = self.selected_source()
        count = int((source or {}).get("import_count") or 0)
        noun = "каналов" if self.kind == "channels" else "элементов конфигурации"
        self.summary.setText(f"Будет подготовлено к импорту: {count} {noun}.")
        self.import_button.setEnabled(source is not None and count > 0)

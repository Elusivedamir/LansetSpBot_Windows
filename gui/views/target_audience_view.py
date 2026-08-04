from __future__ import annotations

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class TargetAudienceView(QWidget):
    """Entry point from LansetSpBot to the target-audience Telegram service."""

    BOT_URL = "https://t.me/TargetAudienceCommentBot"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._compact_mode = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 32)
        layout.setSpacing(18)

        self.title = QLabel("Режим поиска ЦА")
        self.title.setObjectName("pageTitle")
        self.title.setWordWrap(True)
        layout.addWidget(self.title)

        self.subtitle = QLabel(
            "Получайте новые запросы потенциальных клиентов из тематических "
            "Telegram-групп в закрытом канале выбранной ниши."
        )
        self.subtitle.setObjectName("pageSubtitle")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.subtitle)

        card = QFrame()
        card.setObjectName("infoCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 22, 24, 24)
        card_layout.setSpacing(14)

        card_title = QLabel("Поиск клиентов в Telegram")
        card_title.setObjectName("cardTitle")
        card_title.setWordWrap(True)
        card_layout.addWidget(card_title)

        description = QLabel(
            "Выберите нишу в Telegram-боте и получите доступ к закрытому "
            "каналу с подходящими сообщениями целевой аудитории."
        )
        description.setObjectName("mutedText")
        description.setWordWrap(True)
        card_layout.addWidget(description)

        benefits = QLabel(
            "• 3 дня бесплатного доступа\n"
            "• одна активная ниша\n"
            "• только новые публикации\n"
            "• смена ниши раз в час при активной подписке"
        )
        benefits.setObjectName("pageSubtitle")
        benefits.setWordWrap(True)
        card_layout.addWidget(benefits)

        actions = QHBoxLayout()
        actions.setSpacing(10)

        self.open_button = QPushButton("Открыть Telegram-бота")
        self.open_button.setObjectName("primaryButton")
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_button.setAccessibleName("Открыть Telegram-бота поиска ЦА")
        self.open_button.setToolTip(self.BOT_URL)
        self.open_button.clicked.connect(self._open_bot)
        actions.addWidget(self.open_button)
        actions.addStretch(1)
        card_layout.addLayout(actions)

        notice = QLabel(
            "Поиск сообщений и управление доступом выполняются отдельным "
            "сервисом. Аккаунты и кампании LansetSpBot не передаются боту."
        )
        notice.setObjectName("mutedText")
        notice.setWordWrap(True)
        card_layout.addWidget(notice)

        layout.addWidget(card)
        layout.addStretch(1)

    def _open_bot(self) -> None:
        QDesktopServices.openUrl(QUrl(self.BOT_URL))

    def set_compact_mode(self, compact: bool) -> None:
        self._compact_mode = bool(compact)
        margins = 18 if self._compact_mode else 32
        self.layout().setContentsMargins(margins, 22, margins, 24)

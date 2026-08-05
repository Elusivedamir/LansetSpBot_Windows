from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


_GUIDE_HTML = """
<style>
body { font-family: "Segoe UI", Arial, sans-serif; color: #F6FAFF; }
h1, h2 { color: #FFFFFF; }
p { line-height: 1.45; }
</style>
<h1>Инструкция LansetSpBot для Windows</h1>
<p>Все действия относятся к явно выбранному Telegram-аккаунту. Кампании других
аккаунтов продолжают работать в собственных изолированных runtime.</p>

<h2>1. Аккаунт Telegram</h2>
<p>Введите API ID, API Hash и номер телефона. После получения кода подтвердите вход,
а при включённой двухэтапной защите укажите пароль 2FA. Секреты не выводятся в журнал.</p>
<p>Верхняя карточка всегда показывает только выбранный аккаунт. Корзина справа удаляет
только этот аккаунт после отдельного подтверждения.</p>

<h2>2. Импорт</h2>
<p>Выберите источник в контрастном диалоге. Импорт комментариев переносит только
тексты и настройки наборов. История отправок, delivery ledger, результаты кампаний,
ошибки и состояния RUNNING/PAUSED/STOPPED не копируются.</p>
<p>Импорт каналов переносит список и пользовательские настройки, но не считает
подтверждёнными участие, права, discussion chat, access hash и результаты старой проверки.
После импорта запустите перепроверку связок.</p>

<h2>3. Каналы и связки</h2>
<p>Получите список каналов во вкладке «Каналы». Во вкладке «Связки» кнопка
«Проверить новые связки» запускает реальную проверку Telegram. Кнопка
«Перепроверить всё принудительно» игнорирует сохранённый результат проверки.</p>
<p>Между отдельными Telegram API-запросами выдерживается 2–5 секунд, между каналами
12–20 секунд, между вступлениями 2–5 минут. Локальная пауза не является FloodWait.
Настоящий FloodWait берётся только из ответа Telegram и дополняется защитным запасом.</p>

<h2>4. Расписание тишины</h2>
<p>Раскройте блок «🌙 Расписание тишины», укажите часовой пояс IANA, начало и окончание,
затем сохраните расписание. Сворачивание не удаляет введённые значения.</p>

<h2>5. Комментирование</h2>
<p>Сохраните варианты комментариев, выберите источник текста и суточный лимит.
Подтверждённые и неопределённые отправки автоматически не повторяются. Не запускайте
вторую кампанию для того же аккаунта, пока первая активна.</p>

<h2>6. Безопасное завершение и заводской сброс</h2>
<p>При закрытии приложение сначала останавливает фоновые задачи и Telegram runtime.
Заводской сброс удаляет локальные данные только после ввода контрольного слова и
безопасной остановки фоновых операций.</p>
"""


class PremiumInstructionsView(QWidget):
    """UTF-8 text-native guide; no glyphs baked into screenshots or custom fonts."""

    def __init__(self, adapter=None):
        super().__init__()
        self.adapter = adapter
        self._base_point_size = 11.0

        title = QLabel("Инструкция")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "Текст отображается системными шрифтами Windows с полной поддержкой кириллицы."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(10)

        self.browser = QTextBrowser()
        self.browser.setObjectName("instructionTextBrowser")
        self.browser.setOpenExternalLinks(True)
        self.browser.setHtml(_GUIDE_HTML)
        font = QFont("Segoe UI")
        font.setStyleHint(QFont.StyleHint.SansSerif)
        font.setPointSizeF(self._base_point_size)
        self.browser.setFont(font)

        self.fit_button = QPushButton("Вписать в окно")
        self.fit_button.setObjectName("primaryButton")
        self.actual_button = QPushButton("100%")
        self.actual_button.setObjectName("secondaryButton")
        self.zoom_in_button = QPushButton("Увеличить")
        self.zoom_in_button.setObjectName("secondaryButton")
        self.zoom_out_button = QPushButton("Уменьшить")
        self.zoom_out_button.setObjectName("secondaryButton")
        self.fit_button.clicked.connect(self._fit)
        self.actual_button.clicked.connect(self._actual)
        self.zoom_in_button.clicked.connect(self.browser.zoomIn)
        self.zoom_out_button.clicked.connect(self.browser.zoomOut)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(self.fit_button)
        controls.addWidget(self.actual_button)
        controls.addWidget(self.zoom_in_button)
        controls.addWidget(self.zoom_out_button)
        controls.addStretch(1)

        card_layout.addLayout(controls)
        card_layout.addWidget(self.browser, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 18, 28, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(card, 1)

    def _set_point_size(self, size: float) -> None:
        font = self.browser.font()
        font.setFamily("Segoe UI")
        font.setStyleHint(QFont.StyleHint.SansSerif)
        font.setPointSizeF(max(8.0, min(24.0, float(size))))
        self.browser.setFont(font)

    def _fit(self) -> None:
        self._set_point_size(10.0 if self.width() < 900 else self._base_point_size)

    def _actual(self) -> None:
        self._set_point_size(self._base_point_size)

    def set_compact_mode(self, compact: bool) -> None:
        self.layout().setContentsMargins(
            16 if compact else 28,
            10 if compact else 18,
            16 if compact else 28,
            16 if compact else 24,
        )
        self._fit()

    def set_page_active(self, active: bool) -> None:
        del active

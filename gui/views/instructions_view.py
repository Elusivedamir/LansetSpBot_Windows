from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..resources import asset_path


class InstructionsView(QWidget):
    """Scrollable slide-by-slide operator guide with annotated UI diagrams."""

    STEPS = (
        (
            "Подключение аккаунта и proxy",
            "01_account.png",
            "Введите API ID, API Hash и телефон, затем нажмите «Подключить аккаунт». "
            "Код Telegram и пароль 2FA вводятся в появившемся блоке внутри этой же вкладки. "
            "При необходимости включите SOCKS4/SOCKS5, HTTP или MTProxy. Для MTProxy V40 "
            "поддерживает обычный Secret, DD и Fake TLS EE в hex или Base64URL. Сессия и "
            "настройки хранятся локально на компьютере.",
        ),
        (
            "Смена Telegram-аккаунта",
            "01_account.png",
            "Перед выходом LansetSpBot останавливает фоновые операции и не разрешает смену аккаунта, "
            "пока остаётся активная задача. Рабочие каналы, связки, кампании, история, варианты "
            "комментариев, ограничения и «Живой журнал» изолированы по Telegram account_id. После "
            "входа в другой аккаунт журнал очищает отображение и показывает только его события. "
            "Общий сохранённый список каналов переносится между аккаунтами намеренно, но статус "
            "участия и рабочие данные для каждого аккаунта хранятся отдельно.",
        ),
        (
            "Каналы и сохранённый список",
            "02_channels.png",
            "Нажмите «Получить каналы и сохранить список». LansetSpBot проходит диалоги Telegram, "
            "но сохраняет только каналы, группы и супергруппы — личные переписки в таблицу не "
            "попадают. В V40 ход операции виден в «Живом журнале»: начало, обработанное "
            "количество, найденные каналы и итог. Верхнее число показывает сохранённый локальный "
            "список, который может содержать записи, полученные ранее или с другого аккаунта.",
        ),
        (
            "Связки и вступления",
            "03_links.png",
            "Во вкладке «Связки» нажмите «Связать каналы и вступить в обсуждения». LansetSpBot "
            "находит связанный чат каждого канала и заранее вступает туда с безопасной паузой "
            "15–25 секунд. Кампания комментариев сама не выполняет вступления. Кнопка «Стоп» "
            "останавливает новые действия, а сохранённое ожидание и FloodWait продолжаются без "
            "опасного сокращения.",
        ),
        (
            "Комментарии и источник текста",
            "04_comments.png",
            "Выберите источник: «Готовые тексты» сохраняет прежний набор из одного–десяти вариантов, "
            "а «OpenAI» создаёт комментарий по фактическому тексту конкретной публикации. Для готовых "
            "текстов действует перемешанный мешок без прямых повторов. Ползунок задаёт максимум попыток "
            "за 24 часа; рекомендуемая нагрузка — 40 в сутки. Источник и параметры фиксируются отдельно "
            "для каждой кампании.",
        ),
        (
            "Настройка OpenAI и автоматическая отправка",
            "04_comments.png",
            "Сохраните API-ключ, выберите модель, задайте system-промпт, лимит слов, temperature и timeout, "
            "затем нажмите «Проверить подключение». Ключ хранится через локальный SecretStore и показывается "
            "только маской. В рабочей кампании валидный результат отправляется автоматически через обычный "
            "TelegramService и все финальные Stop, FloodWait, restriction и delivery-проверки. При пустом посте, "
            "ошибке OpenAI или неоднозначном результате Telegram отправка не повторяется автоматически.",
        ),
        (
            "Запуск кампании V40",
            "05_start.png",
            "После подготовки связок нажмите «Запустить на 24 часа» один раз. LansetSpBot создаёт "
            "случайные слоты только для доступных уникальных каналов и показывает период, план, "
            "число выполненных попыток и число реально отправленных комментариев. В V40 повторное "
            "нажатие при уже активной кампании не создаёт вторую кампанию и не используется как "
            "ручное обновление экрана.",
        ),
        (
            "Живой таймер, история и журнал",
            "06_log_spambot.png",
            "В V40 надписи «Следующая проверка» в карточке кампании и «Живом журнале» обновляются "
            "каждую секунду в формате 00:47. Отсчёт всегда пересчитывается от абсолютного времени, "
            "поэтому после задержки интерфейс сразу догоняет реальность. После отправки строка "
            "появляется в истории, «Выполнено» и «Отправлено» обновляются, а журнал показывает "
            "найденную цель и подтверждённый результат Telegram.",
        ),
        (
            "Пауза, продолжение и остановка",
            "07_parsing.png",
            "«Пауза» запрещает запуск новых слотов и не расходует оставшийся план. «Продолжить» "
            "переносит просроченные слоты вперёд без отправки пачкой. «Остановить кампанию» "
            "запрещает новые комментарии. При паузе таймер показывает «после продолжения», а после "
            "остановки или завершения — прочерк. Изменение ползунка и текстов блокируется на время "
            "активной кампании.",
        ),
        (
            "Ограничения Telegram и @SpamBot",
            "08_restriction.png",
            "При PeerFlood, UserBanned, UserRestricted или другом серьёзном ограничении LansetSpBot "
            "включает режим RESTRICTED, останавливает комментарии и оставшиеся вступления и не "
            "повторяет неоднозначные отправки автоматически. Нажмите «Проверить блокировку "
            "@SpamBot», дождитесь подтверждения Telegram об отсутствии ограничений и только затем "
            "снимайте локальную блокировку.",
        ),
        (
            "Ярлык, данные и поддержка",
            "09_help.png",
            "После первого запуска V39–V43 создаёт LansetSpBot.app на рабочем столе; его можно "
            "перетащить в Dock. Данные, база и Telegram-сессия остаются локально. Резервная копия "
            "профиля создаётся во вкладке «Аккаунт» и должна храниться как пароль. Кнопка «Помощь» "
            "внизу левого меню показывает контакт поддержки @lansetp.",
        ),
    )

    def __init__(self, adapter=None):
        super().__init__()
        self.adapter = adapter
        self._compact = False

        title = QLabel("Инструкция")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "Актуальное руководство для V46: подключение, каналы, связки, комментарии, "
            "секундный обратный отсчёт, журнал, ограничения Telegram и поддержка."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)

        self.guide_version = QLabel("Инструкция актуальна для V46")
        self.guide_version.setObjectName("activityBadge")
        self.guide_version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.guide_version.setToolTip(
            "Включает изменения V38–V46: журнал каналов, ярлык, живой таймер, "
            "изоляцию аккаунтов, новый premium-интерфейс и OpenAI-комментарии."
        )

        self.progress_label = QLabel()
        self.progress_label.setObjectName("activityBadge")
        self.progress_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.stack = QStackedWidget()
        self.stack.setObjectName("instructionStack")
        self.stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        for index, (step_title, image_name, body) in enumerate(self.STEPS, start=1):
            self.stack.addWidget(self._make_step(index, step_title, image_name, body))

        self.back_button = QPushButton("← Назад")
        self.back_button.setObjectName("secondaryButton")
        self.back_button.clicked.connect(self.previous_step)
        self.next_button = QPushButton("Далее →")
        self.next_button.setObjectName("primaryButton")
        self.next_button.clicked.connect(self.next_step)

        navigation = QHBoxLayout()
        navigation.setSpacing(12)
        navigation.addWidget(self.back_button)
        navigation.addStretch(1)
        navigation.addWidget(self.progress_label)
        navigation.addStretch(1)
        navigation.addWidget(self.next_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 28, 34, 28)
        layout.setSpacing(16)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(self.guide_version, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.stack, 1)
        layout.addLayout(navigation)
        self._update_navigation()

    @staticmethod
    def _asset_path(name: str) -> Path:
        return asset_path("instructions", name)

    def _make_step(self, index: int, title: str, image_name: str, body: str) -> QWidget:
        # Every slide has its own vertical scroll area. On a full-size desktop
        # window the complete annotated image is visible at once; on a smaller
        # MacBook window the user can scroll the same step without losing the
        # Back/Next controls below the slideshow.
        scroll = QScrollArea()
        scroll.setObjectName("instructionStepScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        card = QFrame()
        card.setObjectName("card")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)

        heading = QLabel(f"{index}. {title}")
        heading.setObjectName("cardTitle")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        image = QLabel()
        image.setObjectName("instructionImage")
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setMinimumHeight(260)
        image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        pixmap = QPixmap(str(self._asset_path(image_name)))
        image.setProperty("sourcePixmap", pixmap)
        image.setPixmap(pixmap)
        layout.addWidget(image, 1)

        description = QLabel(body)
        description.setObjectName("pageSubtitle")
        description.setWordWrap(True)
        description.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(description)
        scroll.setWidget(card)
        return scroll

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._rescale_current_image()

    def _rescale_current_image(self) -> None:
        page = self.stack.currentWidget()
        if page is None:
            return
        image = page.findChild(QLabel, "instructionImage")
        if image is None:
            return
        source = image.property("sourcePixmap")
        if not isinstance(source, QPixmap) or source.isNull():
            return
        target_width = max(320, image.width() - 12)
        target_height = max(220, image.height() - 12)
        image.setPixmap(
            source.scaled(
                target_width,
                target_height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def set_compact_mode(self, compact: bool) -> None:
        self._compact = bool(compact)
        self.back_button.setText("←" if compact else "← Назад")
        self.next_button.setText("→" if compact else "Далее →")
        self._rescale_current_image()

    def previous_step(self) -> None:
        self.stack.setCurrentIndex(max(0, self.stack.currentIndex() - 1))
        self._update_navigation()

    def next_step(self) -> None:
        self.stack.setCurrentIndex(
            min(self.stack.count() - 1, self.stack.currentIndex() + 1)
        )
        self._update_navigation()

    def _update_navigation(self) -> None:
        index = self.stack.currentIndex()
        total = self.stack.count()
        self.progress_label.setText(f"Шаг {index + 1} из {total}")
        self.back_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < total - 1)
        self._rescale_current_image()

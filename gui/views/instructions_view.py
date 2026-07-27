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

from core.version import __version__

from ..resources import asset_path


class InstructionsView(QWidget):
    """Scrollable slide-by-slide operator guide with annotated UI diagrams."""

    IMAGE_SHARE_OF_SLIDE = 0.58

    STEPS = (
        (
            "Подключение аккаунта и proxy",
            "01_account.png",
            "Введите API ID, API Hash и телефон, затем нажмите «Подключить аккаунт». "
            "Код Telegram и пароль 2FA появятся в этой же карточке. Переключатель "
            "«Использовать proxy» открывает SOCKS5, SOCKS4, HTTP и MTProxy. Для MTProxy "
            "поддерживаются обычный Secret, DD и Fake TLS EE в hex или Base64URL. "
            "Рабочая сессия хранится локально и сохраняет вход после закрытия программы. "
            "Дополнительные резервные копии сессии не создаются.",
        ),
        (
            "Тихие часы и цвет переключателей",
            "01_account.png",
            "Все переключатели функций используют одно правило: красный означает "
            "«выключено», зелёный — «включено». Это относится к proxy, тихим часам и "
            "автоматическому продолжению кампании. Горизонтальный ползунок количества "
            "комментариев является числовым регулятором и не показывает состояние функции. "
            "Для тихих часов укажите часовой пояс, начало и окончание, затем нажмите "
            "«Сохранить расписание». Начало и окончание не могут совпадать. Отложенные "
            "слоты переносятся вперёд и не отправляются пачкой.",
        ),
        (
            "Смена Telegram-аккаунта",
            "01_account.png",
            "Перед сменой аккаунта программа останавливает фоновые операции и не разрешает "
            "переключение при активной задаче. Каналы, маршруты, кампании, история, варианты "
            "комментариев, ограничения и «Живой журнал» изолированы по Telegram account_id. "
            "После входа в другой аккаунт интерфейс показывает только его данные. Рабочую "
            "сессию повреждённого аккаунта восстановить из копии нельзя — потребуется новая "
            "авторизация по коду и 2FA.",
        ),
        (
            "Каналы и сохранённый список",
            "02_channels.png",
            "Нажмите «Получить каналы и сохранить список». Программа обновит рабочую базу "
            "и сохранит каналы, группы и супергруппы; личные переписки в таблицу не попадают. "
            "В таблице видны название, username, тип, ID и принадлежность текущему аккаунту. "
            "Ненужные строки можно выделить и удалить кнопкой «Удалить выбранные». Ход и итог "
            "операции отображаются в «Живом журнале».",
        ),
        (
            "Вступление в сохранённые",
            "02_channels.png",
            "После смены аккаунта нажмите «Вступить в сохранённые». Вступления выполняются "
            "с установленным часовым лимитом, минимальным интервалом и Telegram FloodWait. "
            "«Пауза» останавливает новые действия, «Остановить» завершает кампанию вступлений. "
            "Приватный объект без username или сохранённой ссылки остаётся в списке, но "
            "автоматически вступить в него нельзя.",
        ),
        (
            "Связки каналов и обычные группы",
            "03_links.png",
            "Во вкладке «Связки» нажмите «Связать каналы и вступить в обсуждения». Для канала "
            "программа найдёт связанный чат, проверит участие и при необходимости вступит. "
            "Обычная группа получает отдельный режим: сообщение отправляется прямо в чат без "
            "поиска поста, discussion_message_id и reply_to. Ранее проверенные объекты повторно "
            "не запрашиваются у Telegram, пока список каналов не изменится.",
        ),
        (
            "Комментарии и источник текста",
            "04_comments.png",
            "Откройте выпадающий список «Источник комментария». Раскрытое меню имеет светлый "
            "фон и тёмный текст, чтобы варианты «Готовые тексты» и «OpenAI» хорошо читались. "
            "Готовые тексты отправляются перемешанным «мешком»: за полный круг каждый вариант "
            "используется ровно один раз без повторов. В режиме OpenAI один вариант из того же "
            "мешка задаёт смысл будущего ответа, поэтому нужен хотя бы один сохранённый текст.",
        ),
        (
            "Суточный лимит и автопродление",
            "04_comments.png",
            "Ползунок задаёт максимум попыток за 24 часа от 0 до 1000, а не гарантированное "
            "число успешных отправок. Рекомендуемая нагрузка — около 40 в сутки. Зелёный "
            "переключатель «Автоматически продолжать каждые следующие 24 часа» создаёт новый "
            "суточный план после завершения периода; красный отключает продолжение. Во время "
            "паузы или отсутствия сети пропущенные слоты переносятся вперёд без burst-отправки.",
        ),
        (
            "Настройка OpenAI",
            "04_comments.png",
            "Сохраните API-ключ, модель, system-промпт, максимум слов, temperature, таймаут "
            "и число попыток генерации, затем нажмите «Проверить подключение». Ключ хранится "
            "в локальном зашифрованном хранилище и отображается маской. Пост и ваш вариант "
            "передаются как отдельные блоки данных. Тестовая публикация создаёт предпросмотр, "
            "который можно скопировать или сохранить как готовый текст.",
        ),
        (
            "Запуск кампании и маршруты",
            "04_comments.png",
            "Нажмите «Запустить на 24 часа» один раз. Повторное нажатие при активной кампании "
            "не создаёт вторую кампанию. Для канала программа берёт только новый последний "
            "пост и отправляет комментарий через связанное обсуждение. В обычную доступную "
            "группу уходит отдельное сообщение без привязки к посту. Защита доставки не "
            "повторяет подтверждённые или неопределённые отправки автоматически.",
        ),
        (
            "Живой таймер, история и журнал",
            "04_comments.png",
            "«Следующая проверка» обновляется каждую секунду и пересчитывается от абсолютного "
            "времени после сна компьютера или задержки. Карточка показывает период, "
            "«Выполнено» и «Отправлено». В истории колонка «Пост / режим» различает комментарий "
            "к публикации и сообщение в обычную группу. «Живой журнал» получает найденную цель, "
            "причину пропуска и окончательный подтверждённый результат каждого слота.",
        ),
        (
            "Пауза, продолжение и ограничения",
            "04_comments.png",
            "«Пауза» запрещает новые слоты, «Продолжить» переносит просроченные действия "
            "вперёд, а «Остановить кампанию» завершает план. При PeerFlood, UserBanned, "
            "UserRestricted или другом серьёзном ограничении включается RESTRICTED: новые "
            "вступления и отправки блокируются. Проверяйте состояние через кнопку "
            "«Проверить блокировку @SpamBot» и не снимайте ограничение без подтверждения Telegram.",
        ),
        (
            "Данные, закрытие и поддержка",
            "05_instructions.png",
            "Запускайте программу через 1_RUN_LANSETSPBOT_WINDOWS.bat или LansetSpBot.exe. "
            "Кнопка закрытия спрашивает подтверждение и действительно завершает процесс; для "
            "временного скрытия используйте сворачивание. База, настройки и Telegram-сессия "
            "остаются локально, резервные копии не создаются. «Заводской сброс» безвозвратно "
            "удаляет профиль. 3_COLLECT_DIAGNOSTICS.cmd собирает безопасный отчёт без базы, "
            "паролей и сессии. Кнопка «Помощь» показывает поддержку @lansetp.",
        ),
    )

    def __init__(self, adapter=None):
        super().__init__()
        self.adapter = adapter
        self._compact = False

        title = QLabel("Инструкция")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "Актуальный порядок работы: аккаунт, красно-зелёные переключатели, каналы, "
            "маршруты для постов и обычных групп, комментарии, журнал и поддержка."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)

        self.guide_version = QLabel(f"Инструкция для версии {__version__}")
        self.guide_version.setObjectName("activityBadge")
        self.guide_version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.guide_version.setToolTip(
            "Снимки экрана собираются из самого интерфейса "
            "(tools/capture_instruction_screenshots.py), поэтому показывают "
            "текущие экраны, а не прежнюю версию."
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
        image.setMinimumHeight(220)
        image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        pixmap = QPixmap(str(self._asset_path(image_name)))
        image.setProperty("sourcePixmap", pixmap)
        # The full-size pixmap is never handed to the label: it would set the
        # label's size hint to the screenshot's own height, push the card past
        # the visible area and leave the reader scrolling through one image.
        # _rescale_current_image() fits it to the space that actually exists.
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

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        # The first slide is built before the view has a size, so its image is
        # scaled here, once the real geometry exists.
        self._rescale_current_image()

    def _rescale_current_image(self) -> None:
        """Fit the screenshot into the room the slide actually has.

        Sizing from the label itself does not work: the label grows to whatever
        pixmap it holds, so measuring it feeds the old size straight back in.
        The budget comes from the slideshow area instead, with a share left for
        the heading and the description.
        """

        page = self.stack.currentWidget()
        if page is None:
            return
        image = page.findChild(QLabel, "instructionImage")
        if image is None:
            return
        source = image.property("sourcePixmap")
        if not isinstance(source, QPixmap) or source.isNull():
            return
        available_width = self.stack.width() - 100
        available_height = int(self.stack.height() * self.IMAGE_SHARE_OF_SLIDE)
        target_width = max(320, available_width)
        target_height = max(200, available_height)
        image.setMaximumHeight(target_height)
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

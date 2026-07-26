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
            "Код Telegram и пароль 2FA вводятся в появившемся блоке внутри этой же вкладки. "
            "При необходимости включите SOCKS4/SOCKS5, HTTP или MTProxy. Для MTProxy "
            "поддерживается обычный Secret, DD и Fake TLS EE в hex или Base64URL. Сессия и "
            "настройки хранятся локально на этом компьютере: после закрытия программы вход "
            "сохраняется, повторно вводить код не нужно.",
        ),
        (
            "Тихие часы",
            "01_account.png",
            "Переключатель «Не отправлять автоматические комментарии в тихие часы» задаёт окно, "
            "в котором отправка приостанавливается: ночью подряд идущие комментарии выглядят "
            "неестественно. Укажите часовой пояс, начало и окончание, затем «Сохранить расписание». "
            "Начало и окончание не могут совпадать — иначе активного окна не останется. Отложенные "
            "слоты переносятся на ближайшее разрешённое время, а не теряются и не отправляются "
            "пачкой после тишины.",
        ),
        (
            "Смена Telegram-аккаунта",
            "01_account.png",
            "Перед выходом программа останавливает фоновые операции и не разрешает смену аккаунта, "
            "пока остаётся активная задача. Рабочие каналы, связки, кампании, история, варианты "
            "комментариев, ограничения и «Живой журнал» изолированы по Telegram account_id. После "
            "входа в другой аккаунт журнал очищает отображение и показывает только его события. "
            "Сохранённый список каналов и варианты комментариев можно подтянуть в новый аккаунт "
            "теми же кнопками — вступления в каналы при этом выполняются заново, с интервалом.",
        ),
        (
            "Каналы и сохранённый список",
            "02_channels.png",
            "Нажмите «Получить каналы и сохранить список». Программа проходит диалоги Telegram, "
            "но сохраняет только каналы, группы и супергруппы — личные переписки в таблицу не "
            "попадают. Ход операции виден в «Живом журнале»: начало, обработанное количество, "
            "найденные каналы и итог. Верхнее число показывает сохранённый локальный список, "
            "который может содержать записи, полученные ранее или с другого аккаунта.",
        ),
        (
            "Связки и вступления",
            "03_links.png",
            "Во вкладке «Связки» нажмите «Связать каналы и вступить в обсуждения». Программа "
            "находит связанный чат каждого канала и заранее вступает туда с безопасной паузой "
            "15–25 секунд. Кампания комментариев сама вступлений не выполняет. Кнопка «Стоп» "
            "останавливает новые действия, а сохранённое ожидание и FloodWait продолжаются без "
            "опасного сокращения.",
        ),
        (
            "Комментарии и источник текста",
            "04_comments.png",
            "Выберите источник. «Готовые тексты» отправляет ваши варианты как есть: они идут "
            "перемешанным «мешком», в котором за полный круг каждый вариант используется ровно "
            "один раз, без подряд идущих повторов. «OpenAI» берёт один вариант из того же мешка "
            "и текст самой публикации, и просит модель написать комментарий, который отвечает "
            "посту, сохраняя смысл вашего варианта. Поэтому для режима OpenAI нужен хотя бы один "
            "заполненный вариант. Ползунок задаёт максимум попыток за 24 часа; рекомендуемая "
            "нагрузка — около 40 в сутки.",
        ),
        (
            "Настройка OpenAI",
            "04_comments.png",
            "Сохраните API-ключ, выберите модель, задайте system-промпт, лимит слов, temperature и "
            "timeout, затем нажмите «Проверить подключение». Ключ хранится в локальном зашифрованном "
            "хранилище и показывается только маской. Пост и ваш комментарий передаются модели как "
            "данные в отдельных размеченных блоках, поэтому текст публикации не может подменить "
            "инструкцию. При пустом посте, ошибке OpenAI или неоднозначном результате Telegram "
            "отправка не повторяется автоматически.",
        ),
        (
            "Запуск кампании",
            "04_comments.png",
            "После подготовки связок нажмите «Запустить на 24 часа» один раз. Программа создаёт "
            "случайные слоты только для доступных уникальных каналов и показывает период, план, "
            "число выполненных попыток и число реально отправленных комментариев. Повторное "
            "нажатие при уже активной кампании не создаёт вторую кампанию и не используется как "
            "ручное обновление экрана. Комментарий уходит только под новый последний пост канала "
            "через связанное обсуждение; если комментарии закрыты, слот пропускается.",
        ),
        (
            "Живой таймер, история и журнал",
            "04_comments.png",
            "Надписи «Следующая проверка» в карточке кампании и в «Живом журнале» обновляются "
            "каждую секунду в формате 00:47. Отсчёт всегда пересчитывается от абсолютного времени, "
            "поэтому после задержки или сна компьютера интерфейс сразу догоняет реальность. После "
            "отправки строка появляется в истории, «Выполнено» и «Отправлено» обновляются, а журнал "
            "показывает найденную цель и подтверждённый результат Telegram.",
        ),
        (
            "Пауза, продолжение и остановка",
            "04_comments.png",
            "«Пауза» запрещает запуск новых слотов и не расходует оставшийся план. «Продолжить» "
            "переносит просроченные слоты вперёд без отправки пачкой. «Остановить кампанию» "
            "запрещает новые комментарии. При паузе таймер показывает «после продолжения», а после "
            "остановки или завершения — прочерк. Изменение ползунка и текстов блокируется на время "
            "активной кампании.",
        ),
        (
            "Ограничения Telegram и @SpamBot",
            "04_comments.png",
            "При PeerFlood, UserBanned, UserRestricted или другом серьёзном ограничении программа "
            "включает режим RESTRICTED, останавливает комментарии и оставшиеся вступления и не "
            "повторяет неоднозначные отправки автоматически. Нажмите «Проверить блокировку "
            "@SpamBot», дождитесь подтверждения Telegram об отсутствии ограничений и только затем "
            "снимайте локальную блокировку.",
        ),
        (
            "Данные, сброс и поддержка",
            "05_instructions.png",
            "Программа запускается через 1_RUN_LANSETSPBOT_WINDOWS.bat или через собранный "
            "LansetSpBot.exe; ярлык на рабочий стол или в панель задач можно закрепить средствами "
            "Windows. База, настройки и Telegram-сессия остаются на этом компьютере и никуда не "
            "передаются; база зашифрована и привязана к вашей учётной записи Windows. Резервных "
            "копий программа не создаёт и не восстанавливает: копия файла сессии — это второй "
            "действующий ключ от аккаунта. «Заводской сброс» во вкладке «Аккаунт» удаляет локальные "
            "данные безвозвратно. Если что-то не запускается, выполните 3_COLLECT_DIAGNOSTICS.cmd — "
            "он соберёт один файл с описанием ошибки, без паролей, сессий и базы. Кнопка «Помощь» "
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
            "Подключение, тихие часы, каналы, связки, источник комментария, запуск "
            "кампании, журнал, ограничения Telegram и поддержка."
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

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QDialog,
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
from gui.instruction_assets import METADATA_FILENAME, instruction_assets_ready

from ..resources import INSTRUCTION_ASSET_STATUS_ENV, asset_path


class ClickableScreenshot(QLabel):
    """Keyboard-accessible screenshot preview that opens on activation."""

    activated = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Открыть увеличенный скриншот")

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.activated.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() in {
            Qt.Key.Key_Enter,
            Qt.Key.Key_Return,
            Qt.Key.Key_Space,
        }:
            self.activated.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ScreenshotPreviewDialog(QDialog):
    """Large screenshot viewer with fit-to-window and original-size modes."""

    def __init__(self, title: str, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self._source_pixmap = QPixmap(pixmap)
        self._fit_to_window = True

        self.setObjectName("instructionImageDialog")
        self.setWindowTitle(f"{title} — увеличенный скриншот")
        self.setModal(True)

        screen = (
            parent.screen()
            if parent is not None
            else QGuiApplication.primaryScreen()
        )
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(
                max(520, int(available.width() * 0.90)),
                max(420, int(available.height() * 0.86)),
            )
        else:
            self.resize(1100, 760)

        self.image_label = QLabel()
        self.image_label.setObjectName("instructionImagePreview")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("instructionImageScroll")
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setWidget(self.image_label)

        hint = QLabel(
            "Используйте «По размеру окна» для общего вида или «100%» "
            "для чтения мелких подписей."
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)

        self.fit_button = QPushButton("По размеру окна")
        self.fit_button.setObjectName("primaryButton")
        self.fit_button.clicked.connect(lambda: self._set_fit_mode(True))

        self.actual_size_button = QPushButton("100%")
        self.actual_size_button.setObjectName("secondaryButton")
        self.actual_size_button.clicked.connect(lambda: self._set_fit_mode(False))

        close_button = QPushButton("Закрыть")
        close_button.setObjectName("secondaryButton")
        close_button.clicked.connect(self.accept)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        controls.addWidget(hint, 1)
        controls.addWidget(self.fit_button)
        controls.addWidget(self.actual_size_button)
        controls.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        layout.addWidget(self.scroll, 1)
        layout.addLayout(controls)

        self._sync_mode_buttons()
        QTimer.singleShot(0, self._update_pixmap)

    def _set_fit_mode(self, fit_to_window: bool) -> None:
        self._fit_to_window = bool(fit_to_window)
        self._sync_mode_buttons()
        self._update_pixmap()

    def _sync_mode_buttons(self) -> None:
        self.fit_button.setEnabled(not self._fit_to_window)
        self.actual_size_button.setEnabled(self._fit_to_window)

    def _update_pixmap(self) -> None:
        if self._source_pixmap.isNull():
            return
        pixmap = self._source_pixmap
        if self._fit_to_window:
            viewport = self.scroll.viewport().size()
            pixmap = self._source_pixmap.scaled(
                max(1, viewport.width() - 24),
                max(1, viewport.height() - 24),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.image_label.setPixmap(pixmap)
        self.image_label.resize(pixmap.size())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if self._fit_to_window:
            QTimer.singleShot(0, self._update_pixmap)


class InstructionsView(QWidget):
    """Сохранённый список каналов изолирован по Telegram-аккаунтам. Данные одного аккаунта нельзя автоматически подтянуть в новый аккаунт."""
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
            "Пять изолированных аккаунтов",
            "01_account.png",
            "В блоке «Подключённые аккаунты» можно сохранить до пяти Telegram-аккаунтов. "
            "Выбор строки меняет только отображаемые данные: кампании остальных аккаунтов "
            "продолжаются через собственные сессии, proxy, очереди и ограничения.",
        ),
        (
            "Остановка и ручной импорт",
            "01_account.png",
            "«Остановить работу» завершает кампании только выбранного аккаунта и сохраняет "
            "его Telegram-сессию. Комментарии и каналы можно вручную скопировать только из "
            "непосредственно предыдущего выбранного аккаунта; proxy, история, ledger, "
            "ограничения и секреты никогда не импортируются.",
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
            "Смена строки меняет отображаемый аккаунт, но не останавливает его фоновые "
            "кампании: каждый runtime продолжает работу через собственную сессию. Каналы, "
            "маршруты, кампании, история, варианты комментариев, ограничения и «Живой "
            "журнал» изолированы по Telegram account_id. После выбора другого аккаунта "
            "интерфейс показывает только его данные. Рабочую сессию повреждённого аккаунта "
            "восстановить из копии нельзя — потребуется новая авторизация по коду и 2FA.",
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
            "Импорт каналов в другой аккаунт",
            "02_channels.png",
            "После выбора другого аккаунта откройте раздел «Аккаунт» и нажмите "
            "«Импортировать каналы из предыдущего аккаунта». Импорт копирует сохранённый "
            "список в выбранный аккаунт, но не переносит Telegram-сессию, proxy, историю, "
            "ограничения или секреты. Затем откройте «Связки», чтобы проверить маршруты "
            "каналов и обычных групп.",
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
            "не создаёт вторую кампанию. Для канала программа берёт самый последний доступный "
            "пост и отправляет комментарий через связанное обсуждение, если такой результат "
            "ещё не зафиксирован. В обычную доступную группу уходит отдельное сообщение без "
            "привязки к посту. Защита доставки не повторяет подтверждённые или неопределённые "
            "отправки автоматически.",
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
            "остаются локально, резервные копии не создаются. В карточке «Заводской сброс» "
            "кнопка «Сбросить базу данных» безвозвратно удаляет локальный профиль после "
            "двойного подтверждения. 3_COLLECT_DIAGNOSTICS.cmd собирает безопасный отчёт без "
            "базы, паролей и сессии. Кнопка «Помощь» показывает поддержку @lansetp.",
        ),
    )

    def __init__(self, adapter=None):
        super().__init__()
        self.adapter = adapter
        self._compact = False

        title = QLabel("Инструкция")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "Актуальный порядок работы: подключение аккаунта, красно-зелёные переключатели, "
            "каналы, маршруты для постов и обычных групп, комментарии, журнал и поддержка."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)

        self.guide_version = QLabel(f"Инструкция для версии {__version__}")
        self.guide_version.setObjectName("activityBadge")
        self.guide_version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        asset_directory = self._asset_path(METADATA_FILENAME).parent
        self._screenshots_ready = instruction_assets_ready(asset_directory)
        self.guide_version.setToolTip(
            (
                "Снимки проверены по исходникам и хешам текущей сборки."
                if self._screenshots_ready
                else (
                    "Снимки скрыты: автоматическая генерация в пользовательском "
                    "кэше не удалась. Основные функции приложения доступны; "
                    "перезапустите программу после проверки установки PySide6."
                    if os.environ.get(INSTRUCTION_ASSET_STATUS_ENV)
                    == "generation_failed"
                    else "Снимки скрыты до проверки хешей. При source-запуске они "
                    "создаются в пользовательском кэше без изменения checkout."
                )
            )
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

        image = ClickableScreenshot()
        image.setObjectName("instructionImage")
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setMinimumHeight(220)
        image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        pixmap = (
            QPixmap(str(self._asset_path(image_name)))
            if self._screenshots_ready
            else QPixmap()
        )
        image.setProperty("sourcePixmap", pixmap)
        if pixmap.isNull():
            image.setText(
                "Скриншот скрыт: его соответствие текущему интерфейсу не подтверждено. "
                "Source-запуск создаёт проверенную копию в пользовательском кэше; "
                "при ошибке генерации основное приложение продолжает работу."
            )
            image.setCursor(Qt.CursorShape.ArrowCursor)
            image.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        else:
            image.setToolTip("Нажмите, чтобы открыть увеличенный скриншот")
            image.activated.connect(
                lambda pixmap=pixmap, title=title: self._open_image_preview(
                    title, pixmap
                )
            )
        # The full-size pixmap is never handed to the label: it would set the
        # label's size hint to the screenshot's own height, push the card past
        # the visible area and leave the reader scrolling through one image.
        # _rescale_current_image() fits it to the space that actually exists.
        layout.addWidget(image, 1)

        image_hint = QLabel("Нажмите на изображение, чтобы увеличить")
        image_hint.setObjectName("instructionImageHint")
        image_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image_hint.setVisible(not pixmap.isNull())
        layout.addWidget(image_hint)

        description = QLabel(body)
        description.setObjectName("pageSubtitle")
        description.setWordWrap(True)
        description.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(description)
        scroll.setWidget(card)
        return scroll

    def _open_image_preview(self, title: str, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        dialog = ScreenshotPreviewDialog(title, pixmap, self)
        dialog.exec()

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

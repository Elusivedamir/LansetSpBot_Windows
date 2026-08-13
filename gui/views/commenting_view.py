# OBSERVABILITY-PACKAGE-V3
from __future__ import annotations

from typing import cast

import logging

from PySide6.QtCore import QThreadPool, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.version import APP_NAME
from core.openai_settings import SOURCE_OPENAI, SOURCE_PREWRITTEN
from core.config import MAX_COMMENT_VARIANTS
from core.countdown import countdown_label, seconds_until
from core.campaign_schedule import from_db_time
from gui.background import BackgroundCall, connect_lifecycle_safe
from services.observability import (
    campaign_statistics,
    classify_result,
    format_campaign_statistics,
    humanize_reason,
)

log = logging.getLogger(__name__)


from gui.views.commenting_parts.daily_limit import CommentingDailyLimitMixin
from gui.views.commenting_parts.openai_panel import CommentingOpenAIMixin
from gui.views.commenting_parts.profile import CommentingProfileMixin
from gui.views.commenting_parts.campaign import CommentingCampaignMixin

class CommentingView(CommentingDailyLimitMixin, CommentingOpenAIMixin, CommentingProfileMixin, CommentingCampaignMixin, QWidget):
    """Persistent 24-hour campaign UI; no long sleeps live in the worker."""

    def __init__(self, adapter):
        super().__init__()
        self.adapter = adapter
        self.current_campaign_id: int | None = None
        self.channel_names: dict[int, str] = {}
        self._last_history_rows: list[dict] = []
        self._loading_comments = False
        self._comments_dirty = False
        self._loaded_account_id: int | None = None
        self._loading_daily_limit = False
        self._daily_limit_account_id: int | None = None
        self._last_refresh_error = ""
        self._refresh_job: BackgroundCall | None = None
        self._refresh_pending = False
        self._account_generation = 0
        self._page_active = True
        self._next_check_at = None
        self._next_check_key = ""
        self._next_check_due_refresh_requested = False
        self._next_check_fallback = "Следующая проверка: —"
        self._loading_openai_settings = False
        self._openai_test_future = None
        self._openai_test_generation = -1
        self.limit_save_timer = QTimer(self)
        self.limit_save_timer.setSingleShot(True)
        self.limit_save_timer.setInterval(250)
        self.limit_save_timer.timeout.connect(self._save_daily_limit)

        title = QLabel("Комментирование")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            f"Выберите суточный лимит от 0 до 1000. {APP_NAME} распределит попытки на 24 часа, "
            "по одной цели за слот: для канала — под новым последним постом через "
            "связанное обсуждение, для обычной группы — отдельным сообщением без "
            "привязки к посту."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)

        self.section_nav = QFrame()
        self.section_nav.setObjectName("infoCard")
        section_nav_layout = QHBoxLayout(self.section_nav)
        section_nav_layout.setContentsMargins(12, 10, 12, 10)
        section_nav_layout.setSpacing(10)
        self.comments_section_button = QPushButton("Комментарии")
        self.comments_section_button.setObjectName("primaryButton")
        self.comments_section_button.clicked.connect(
            lambda _checked=False: self._show_section("comments")
        )
        self.campaign_section_button = QPushButton("Запуск кампании")
        self.campaign_section_button.setObjectName("secondaryButton")
        self.campaign_section_button.clicked.connect(
            lambda _checked=False: self._show_section("campaign")
        )
        section_nav_layout.addWidget(self.comments_section_button)
        section_nav_layout.addWidget(self.campaign_section_button)
        section_nav_layout.addStretch(1)

        source_card = QFrame()
        source_card.setObjectName("card")
        source_layout = QHBoxLayout(source_card)
        source_title = QLabel("Источник комментария")
        source_title.setObjectName("cardTitle")
        self.comment_source_combo = QComboBox()
        self.comment_source_combo.setObjectName("commentSourceCombo")
        self.comment_source_combo.addItem("Готовые тексты", SOURCE_PREWRITTEN)
        self.comment_source_combo.addItem("OpenAI", SOURCE_OPENAI)
        openai_index = self.comment_source_combo.findData(SOURCE_OPENAI)
        if openai_index >= 0:
            self.comment_source_combo.setItemData(
                openai_index,
                QColor("#39FF14"),
                Qt.ItemDataRole.ForegroundRole,
            )
        self.comment_source_combo.currentIndexChanged.connect(
            self._on_comment_source_changed
        )
        source_layout.addWidget(source_title, 1)
        source_layout.addWidget(self.comment_source_combo, 0)

        comments_card = QFrame()
        comments_card.setObjectName("card")
        comments_layout = QVBoxLayout(comments_card)
        comments_header = QHBoxLayout()
        self.comments_title = QLabel("Варианты готовых комментариев")
        self.comments_title.setObjectName("cardTitle")
        self.variant_count_label = QLabel()
        self.variant_count_label.setObjectName("mutedText")
        self.variant_count_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        comments_header.addWidget(self.comments_title, 1)
        comments_header.addWidget(self.variant_count_label, 0)
        comments_layout.addLayout(comments_header)

        self.comments_rows_layout = QVBoxLayout()
        self.comments_rows_layout.setSpacing(10)
        self.editors: list[QPlainTextEdit] = []
        self.variant_rows: list[QFrame] = []
        self.variant_selectors: list[QRadioButton] = []
        self.variant_number_labels: list[QLabel] = []
        self.variant_button_group = QButtonGroup(self)
        self.variant_button_group.setExclusive(True)
        for _ in range(MAX_COMMENT_VARIANTS):
            self._append_comment_editor(mark_dirty=False)
        comments_layout.addLayout(self.comments_rows_layout)

        variants_controls = QHBoxLayout()
        variants_controls.setSpacing(10)
        self.add_variant_button = QPushButton("+ Добавить вариант")
        self.add_variant_button.setObjectName("secondaryButton")
        self.add_variant_button.clicked.connect(self.add_comment_variant)
        self.delete_variant_button = QPushButton("Удалить выбранный")
        self.delete_variant_button.setObjectName("secondaryButton")
        self.delete_variant_button.clicked.connect(self.delete_selected_variant)
        self.import_previous_button = QPushButton(
            "Импортировать из предыдущего аккаунта"
        )
        self.import_previous_button.setObjectName("secondaryButton")
        self.import_previous_button.clicked.connect(
            self.import_previous_account_comments
        )
        self.add_variant_button.hide()
        self.delete_variant_button.hide()
        variants_controls.addWidget(self.import_previous_button)
        variants_controls.addStretch(1)
        comments_layout.addLayout(variants_controls)

        save_row = QHBoxLayout()
        save_row.setSpacing(12)
        self.save_comments_button = QPushButton("Сохранить комментарии")
        self.save_comments_button.setObjectName("saveButton")
        self.save_comments_button.clicked.connect(self.save_comments)
        self.save_status = QLabel("Комментарии загружены")
        self.save_status.setObjectName("saveStatusSaved")
        self.save_status.setWordWrap(True)
        save_row.addWidget(self.save_comments_button, 0)
        save_row.addWidget(self.save_status, 1)
        comments_layout.addLayout(save_row)

        self.campaign_settings_card = QFrame()
        self.campaign_settings_card.setObjectName("statusCard")
        campaign_settings_layout = QVBoxLayout(self.campaign_settings_card)
        campaign_settings_layout.setContentsMargins(16, 16, 16, 16)
        campaign_settings_layout.setSpacing(10)

        campaign_settings_title = QLabel("Параметры запуска")
        campaign_settings_title.setObjectName("cardTitle")
        campaign_settings_layout.addWidget(campaign_settings_title)

        self.continuous = QCheckBox("Автоматически продолжать каждые следующие 24 часа")
        self.continuous.setChecked(True)
        campaign_settings_layout.addWidget(self.continuous)

        limit_header = QHBoxLayout()
        limit_title = QLabel("Количество комментариев в сутки")
        limit_title.setObjectName("cardTitle")
        self.daily_limit_value = QLabel("40")
        self.daily_limit_value.setObjectName("statusTitle")
        self.daily_limit_value.setAlignment(Qt.AlignmentFlag.AlignRight)
        limit_header.addWidget(limit_title, 1)
        limit_header.addWidget(self.daily_limit_value, 0)
        campaign_settings_layout.addLayout(limit_header)

        self.daily_limit_slider = QSlider(Qt.Orientation.Horizontal)
        self.daily_limit_slider.setRange(0, 1000)
        self.daily_limit_slider.setSingleStep(1)
        self.daily_limit_slider.setPageStep(25)
        self.daily_limit_slider.setTickInterval(100)
        self.daily_limit_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.daily_limit_slider.setAccessibleName("Количество комментариев в сутки")
        self.daily_limit_slider.valueChanged.connect(self._on_daily_limit_changed)
        campaign_settings_layout.addWidget(self.daily_limit_slider)

        self.daily_limit_hint = QLabel()
        self.daily_limit_hint.setObjectName("mutedText")
        self.daily_limit_hint.setWordWrap(True)
        campaign_settings_layout.addWidget(self.daily_limit_hint)

        self.recommended_load = QLabel(
            "Рекомендуемая нагрузка: 40 комментариев в сутки"
        )
        self.recommended_load.setObjectName("saveStatusSaved")
        self.recommended_load.setWordWrap(True)
        campaign_settings_layout.addWidget(self.recommended_load)

        self.safety = QLabel(
            "Выбранное число — максимум попыток, а не гарантия успешных отправок. "
            "При паузе, выключенном приложении или отсутствии сети оставшиеся слоты сохраняются "
            "и после возобновления переносятся вперёд без отправки пачкой. "
            "Высокие значения могут вызвать ограничения Telegram."
        )
        self.safety.setObjectName("mutedText")
        self.safety.setWordWrap(True)
        campaign_settings_layout.addWidget(self.safety)

        self.openai_card = QFrame()
        self.openai_card.setObjectName("card")
        openai_layout = QVBoxLayout(self.openai_card)
        openai_title = QLabel("OpenAI · автоматическая генерация")
        openai_title.setObjectName("openAiTitle")
        openai_note = QLabel(
            "Для каждой отправки берётся один вариант из вашего перемешанного набора "
            "и передаётся в OpenAI вместе с текстом поста. Модель пишет один "
            "комментарий, который сохраняет смысл и тон вашего варианта и при этом "
            "относится к содержанию публикации. Ротация без повторов сохраняется. "
            "После локальной валидации комментарий отправляется автоматически через "
            "существующие Stop, FloodWait, account-isolation и dispatch-barriers. "
            "При ошибке генерации сообщение не отправляется."
        )
        openai_note.setObjectName("mutedText")
        openai_note.setWordWrap(True)
        openai_layout.addWidget(openai_title)
        openai_layout.addWidget(openai_note)

        openai_form = QFormLayout()
        self.openai_api_key = QLineEdit()
        self.openai_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_api_key.setPlaceholderText("Вставьте API-ключ; сохранённый ключ отображается маской")
        self.openai_model = QLineEdit()
        self.openai_max_words = QSpinBox()
        self.openai_max_words.setRange(3, 200)
        self.openai_temperature = QDoubleSpinBox()
        self.openai_temperature.setRange(0.0, 2.0)
        self.openai_temperature.setSingleStep(0.1)
        self.openai_temperature.setDecimals(1)
        self.openai_timeout = QDoubleSpinBox()
        self.openai_timeout.setRange(5.0, 180.0)
        self.openai_timeout.setSuffix(" сек")
        self.openai_attempts = QSpinBox()
        self.openai_attempts.setRange(1, 3)
        self.openai_system_prompt = QPlainTextEdit()
        self.openai_system_prompt.setMinimumHeight(170)
        self.openai_system_prompt.setPlaceholderText("System-промпт генерации")
        openai_form.addRow("API-ключ", self.openai_api_key)
        openai_form.addRow("Модель", self.openai_model)
        openai_form.addRow("Максимум слов", self.openai_max_words)
        openai_form.addRow("Temperature", self.openai_temperature)
        openai_form.addRow("Таймаут", self.openai_timeout)
        openai_form.addRow("Попыток генерации", self.openai_attempts)
        openai_form.addRow("System-промпт", self.openai_system_prompt)
        openai_layout.addLayout(openai_form)

        openai_actions = QHBoxLayout()
        self.openai_save_button = QPushButton("Сохранить настройки")
        self.openai_save_button.setObjectName("saveButton")
        self.openai_save_button.clicked.connect(self.save_openai_configuration)
        self.openai_test_button = QPushButton("Проверить подключение")
        self.openai_test_button.setObjectName("secondaryButton")
        self.openai_test_button.clicked.connect(self.test_openai_connection)
        self.openai_status = QLabel("OpenAI не проверен")
        self.openai_status.setObjectName("mutedText")
        self.openai_status.setWordWrap(True)
        openai_actions.addWidget(self.openai_save_button)
        openai_actions.addWidget(self.openai_test_button)
        openai_actions.addWidget(self.openai_status, 1)
        openai_layout.addLayout(openai_actions)

        test_title = QLabel("Тестовая публикация")
        test_title.setObjectName("cardTitle")
        self.openai_test_post = QPlainTextEdit()
        self.openai_test_post.setPlaceholderText(
            "Введите текст публикации для проверки генерации"
        )
        self.openai_test_post.setMinimumHeight(90)
        self.openai_preview = QPlainTextEdit()
        self.openai_preview.setReadOnly(True)
        self.openai_preview.setPlaceholderText("Здесь появится тестовый комментарий")
        self.openai_preview.setMinimumHeight(80)
        preview_actions = QHBoxLayout()
        self.openai_copy_button = QPushButton("Скопировать")
        self.openai_copy_button.setObjectName("secondaryButton")
        self.openai_copy_button.clicked.connect(self.copy_openai_preview)
        self.openai_use_button = QPushButton("Использовать как готовый текст")
        self.openai_use_button.setObjectName("secondaryButton")
        self.openai_use_button.clicked.connect(self.use_openai_preview)
        preview_actions.addWidget(self.openai_copy_button)
        preview_actions.addWidget(self.openai_use_button)
        preview_actions.addStretch(1)
        openai_layout.addWidget(test_title)
        openai_layout.addWidget(self.openai_test_post)
        openai_layout.addWidget(self.openai_preview)
        openai_layout.addLayout(preview_actions)

        self.openai_poll_timer = QTimer(self)
        self.openai_poll_timer.setInterval(120)
        self.openai_poll_timer.timeout.connect(self._poll_openai_test)

        self.start_button = QPushButton("Запустить на 24 часа")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_campaign)
        self.pause_button = QPushButton("Пауза")
        self.pause_button.setObjectName("secondaryButton")
        self.pause_button.clicked.connect(self.pause_campaign)
        self.resume_button = QPushButton("Продолжить")
        self.resume_button.setObjectName("secondaryButton")
        self.resume_button.clicked.connect(self.resume_campaign)
        self.stop_button = QPushButton("Остановить кампанию")
        self.stop_button.setObjectName("secondaryButton")
        self.stop_button.clicked.connect(self.stop_campaign)

        self.controls_grid = QGridLayout()
        self.controls_grid.setHorizontalSpacing(10)
        self.controls_grid.setVerticalSpacing(10)
        self.controls_grid.addWidget(self.start_button, 0, 0)
        self.controls_grid.addWidget(self.pause_button, 0, 1)
        self.controls_grid.addWidget(self.resume_button, 0, 2)
        self.controls_grid.addWidget(self.stop_button, 0, 3)
        self.controls_grid.setColumnStretch(4, 1)

        campaign_card = QFrame()
        campaign_card.setObjectName("statusCard")
        self.campaign_layout = QGridLayout(campaign_card)
        self.status = QLabel("Кампания не запущена")
        self.status.setObjectName("statusTitle")
        self.period_label = QLabel("Период: —")
        self.period_label.setObjectName("mutedText")
        self.next_label = QLabel("Следующая проверка: —")
        self.next_label.setObjectName("mutedText")
        self.count_label = QLabel("Выполнено: 0 из 40 · отправлено: 0")
        self.count_label.setObjectName("mutedText")
        self.campaign_layout.addWidget(self.status, 0, 0, 1, 2)
        self.campaign_layout.addWidget(self.period_label, 1, 0)
        self.campaign_layout.addWidget(self.next_label, 1, 1)
        self.campaign_layout.addWidget(self.count_label, 2, 0, 1, 2)
        self.campaign_stats_label = QLabel(
            format_campaign_statistics(campaign_statistics(None, []))
        )
        self.campaign_stats_label.setObjectName("mutedText")
        self.campaign_stats_label.setWordWrap(True)
        self.history_filter = QComboBox()
        self.history_filter.addItem("Все результаты", "all")
        self.history_filter.addItem("Успешные", "success")
        self.history_filter.addItem("Пропущенные", "skipped")
        self.history_filter.addItem("Ошибки", "failed")
        self.history_filter.addItem("Отменённые", "cancelled")
        self.history_filter.addItem("Не подтверждённые", "uncertain")
        self.campaign_layout.addWidget(self.campaign_stats_label, 3, 0, 1, 2)
        self.campaign_layout.addWidget(self.history_filter, 4, 0, 1, 2)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        history_title = QLabel("История текущей кампании")
        history_title.setObjectName("cardTitle")
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            [
                "Канал / группа",
                "Пост / режим",
                "ID комментария",
                "Текст",
                "Результат",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self.history_filter.currentIndexChanged.connect(self._rerender_history)

        self.comments_section = QWidget()
        self.comments_section.setObjectName("commentingCommentsSection")
        comments_section_layout = QVBoxLayout(self.comments_section)
        comments_section_layout.setContentsMargins(0, 0, 0, 0)
        comments_section_layout.setSpacing(14)
        comments_section_layout.addWidget(source_card)
        comments_section_layout.addWidget(comments_card)
        comments_section_layout.addWidget(self.openai_card)
        comments_section_layout.addStretch(1)

        self.campaign_section = QFrame()
        self.campaign_section.setObjectName("card")
        campaign_section_layout = QVBoxLayout(self.campaign_section)
        campaign_section_layout.setContentsMargins(18, 18, 18, 18)
        campaign_section_layout.setSpacing(12)
        campaign_heading = QLabel("Запуск и состояние кампании")
        campaign_heading.setObjectName("cardTitle")
        campaign_section_layout.addWidget(campaign_heading)
        campaign_section_layout.addWidget(self.campaign_settings_card)
        campaign_section_layout.addLayout(self.controls_grid)
        campaign_section_layout.addWidget(campaign_card)
        campaign_section_layout.addWidget(self.progress)
        campaign_section_layout.addWidget(history_title)
        self.table.setMinimumHeight(320)
        campaign_section_layout.addWidget(self.table)

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(34, 28, 34, 28)
        self.main_layout.setSpacing(14)
        self.main_layout.addWidget(title)
        self.main_layout.addWidget(subtitle)
        self.main_layout.addWidget(self.section_nav)
        self.main_layout.addWidget(self.comments_section)
        self.main_layout.addWidget(self.campaign_section)
        self.campaign_section.hide()
        self._active_section = "comments"
        self._compact = False

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(5_000)
        self.refresh_timer.timeout.connect(self.request_campaign_refresh)
        self.refresh_timer.start()

        # This timer only repaints the countdown. It never touches SQLite or
        # Telegram. The remaining time is recalculated from the absolute
        # next_scheduled_at value, so delayed Qt events cannot make it drift.
        self.countdown_timer = QTimer(self)
        self.countdown_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.countdown_timer.setInterval(1_000)
        self.countdown_timer.timeout.connect(self._update_next_check_label)
        self.countdown_timer.start()
        self.load_daily_limit()
        self.load_comments()
        self.load_openai_configuration()
        self.request_campaign_refresh()

    def _set_section_navigation(self, section: str) -> None:
        for name, button in (
            ("comments", self.comments_section_button),
            ("campaign", self.campaign_section_button),
        ):
            button.setObjectName(
                "primaryButton" if name == section else "secondaryButton"
            )
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()

    def _show_section(self, section: str) -> None:
        normalized = "campaign" if section == "campaign" else "comments"
        show_comments = normalized == "comments"
        self._active_section = normalized
        self.comments_section.setVisible(show_comments)
        self.campaign_section.setVisible(not show_comments)
        self._set_section_navigation(normalized)
        self.updateGeometry()
























    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        parent = self.parentWidget()
        viewport_width = parent.width() if parent is not None else self.width()
        self._apply_compact(viewport_width < 700)

    def set_compact_mode(self, compact: bool) -> None:
        self._apply_compact(bool(compact))

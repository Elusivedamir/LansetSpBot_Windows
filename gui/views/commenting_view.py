from __future__ import annotations

from typing import cast

import logging

from PySide6.QtCore import QThreadPool, Qt, QTimer
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

log = logging.getLogger(__name__)


class CommentingView(QWidget):
    """Persistent 24-hour campaign UI; no long sleeps live in the worker."""

    def __init__(self, adapter):
        super().__init__()
        self.adapter = adapter
        self.current_campaign_id: int | None = None
        self.channel_names: dict[int, str] = {}
        self._loading_comments = False
        self._comments_dirty = False
        self._loaded_account_id: int | None = None
        self._loading_daily_limit = False
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

        source_card = QFrame()
        source_card.setObjectName("card")
        source_layout = QHBoxLayout(source_card)
        source_title = QLabel("Источник комментария")
        source_title.setObjectName("cardTitle")
        self.comment_source_combo = QComboBox()
        self.comment_source_combo.setObjectName("commentSourceCombo")
        self.comment_source_combo.addItem("Готовые тексты", SOURCE_PREWRITTEN)
        self.comment_source_combo.addItem("OpenAI", SOURCE_OPENAI)
        source_popup = self.comment_source_combo.view()
        source_popup.setStyleSheet(
            "QAbstractItemView {"
            "background: qlineargradient("
            "x1: 0, y1: 0, x2: 0, y2: 1, "
            "stop: 0 #FFFFFF, stop: 1 #7D8B9B"
            ");"
            "color: #17202A; outline: 0;"
            "selection-background-color: #34465A; selection-color: #FFFFFF;"
            "}"
            "QAbstractItemView::item {"
            "background: transparent; color: #17202A;"
            "min-height: 28px; padding: 4px 8px;"
            "}"
            "QAbstractItemView::item:selected {"
            "background: #34465A; color: #FFFFFF;"
            "}"
        )
        source_popup.viewport().setStyleSheet(
            "background: qlineargradient("
            "x1: 0, y1: 0, x2: 0, y2: 1, "
            "stop: 0 #FFFFFF, stop: 1 #7D8B9B"
            ");"
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

        self.continuous = QCheckBox("Автоматически продолжать каждые следующие 24 часа")
        self.continuous.setChecked(True)
        comments_layout.addWidget(self.continuous)

        limit_header = QHBoxLayout()
        limit_title = QLabel("Количество комментариев в сутки")
        limit_title.setObjectName("cardTitle")
        self.daily_limit_value = QLabel("40")
        self.daily_limit_value.setObjectName("statusTitle")
        self.daily_limit_value.setAlignment(Qt.AlignmentFlag.AlignRight)
        limit_header.addWidget(limit_title, 1)
        limit_header.addWidget(self.daily_limit_value, 0)
        comments_layout.addLayout(limit_header)

        self.daily_limit_slider = QSlider(Qt.Orientation.Horizontal)
        self.daily_limit_slider.setRange(0, 1000)
        self.daily_limit_slider.setSingleStep(1)
        self.daily_limit_slider.setPageStep(25)
        self.daily_limit_slider.setTickInterval(100)
        self.daily_limit_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.daily_limit_slider.setAccessibleName("Количество комментариев в сутки")
        self.daily_limit_slider.valueChanged.connect(self._on_daily_limit_changed)
        comments_layout.addWidget(self.daily_limit_slider)

        self.daily_limit_hint = QLabel()
        self.daily_limit_hint.setObjectName("mutedText")
        self.daily_limit_hint.setWordWrap(True)
        comments_layout.addWidget(self.daily_limit_hint)

        self.recommended_load = QLabel(
            "Рекомендуемая нагрузка: 40 комментариев в сутки"
        )
        self.recommended_load.setObjectName("saveStatusSaved")
        self.recommended_load.setWordWrap(True)
        comments_layout.addWidget(self.recommended_load)

        self.safety = QLabel(
            "Выбранное число — максимум попыток, а не гарантия успешных отправок. "
            "При паузе, выключенном приложении или отсутствии сети оставшиеся слоты сохраняются "
            "и после возобновления переносятся вперёд без отправки пачкой. "
            "Высокие значения могут вызвать ограничения Telegram."
        )
        self.safety.setObjectName("mutedText")
        self.safety.setWordWrap(True)
        comments_layout.addWidget(self.safety)

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

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        history_title = QLabel("История текущей кампании")
        history_title.setObjectName("cardTitle")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Канал / группа", "Пост / режим", "Текст", "Результат"]
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
            2, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(34, 28, 34, 28)
        self.main_layout.setSpacing(14)
        self.main_layout.addWidget(title)
        self.main_layout.addWidget(subtitle)
        self.main_layout.addWidget(source_card)
        self.main_layout.addWidget(comments_card)
        self.main_layout.addWidget(self.openai_card)
        self.main_layout.addLayout(self.controls_grid)
        self.main_layout.addWidget(campaign_card)
        self.main_layout.addWidget(self.progress)
        self.main_layout.addWidget(history_title)
        self.main_layout.addWidget(self.table, 1)
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

    def _current_comment_source(self) -> str:
        value = self.comment_source_combo.currentData()
        return SOURCE_OPENAI if value == SOURCE_OPENAI else SOURCE_PREWRITTEN

    def _on_comment_source_changed(self, _index: int = -1) -> None:
        source = self._current_comment_source()
        prepared = source == SOURCE_PREWRITTEN
        # The variants are used by both sources. In prepared mode one bag item
        # is sent verbatim; in OpenAI mode the same bag item is handed to the
        # model as the meaning to preserve. They are therefore always visible
        # and always required.
        self.comments_title.setText(
            "Варианты готовых комментариев"
            if prepared
            else (
                "Ваши комментарии · смысл для "
                '<span style="color:#39FF14; font-weight:800;">OpenAI</span>'
            )
        )
        self.comment_source_combo.setProperty("openAiSelected", not prepared)
        self.comment_source_combo.style().unpolish(self.comment_source_combo)
        self.comment_source_combo.style().polish(self.comment_source_combo)
        self.comment_source_combo.update()
        self.variant_count_label.setVisible(True)
        for row in self.variant_rows:
            row.setVisible(True)
        self.import_previous_button.setVisible(True)
        self.save_comments_button.setVisible(True)
        self.save_status.setVisible(True)
        self.openai_card.setVisible(not prepared)
        if not self._loading_openai_settings:
            try:
                self.adapter.save_openai_configuration(
                    {"comment_source": source}
                )
            except Exception:
                log.exception("Could not persist comment source selection")

    def load_openai_configuration(self) -> None:
        getter = getattr(self.adapter, "get_openai_configuration", None)
        if not callable(getter):
            self.openai_card.hide()
            return
        try:
            values = dict(getter() or {})
        except Exception as exc:
            self.openai_status.setText(f"Не удалось загрузить настройки: {exc}")
            return
        self._loading_openai_settings = True
        try:
            source = values.get("comment_source")
            target = self.comment_source_combo.findData(
                SOURCE_OPENAI if source == SOURCE_OPENAI else SOURCE_PREWRITTEN
            )
            if target >= 0:
                self.comment_source_combo.setCurrentIndex(target)
            self.openai_api_key.clear()
            mask = str(values.get("api_key_mask") or "")
            self.openai_api_key.setPlaceholderText(
                f"Сохранён: {mask}" if mask else "Вставьте API-ключ OpenAI"
            )
            self.openai_model.setText(str(values.get("model") or "gpt-5.5"))
            self.openai_system_prompt.setPlainText(
                str(values.get("system_prompt") or "")
            )
            self.openai_max_words.setValue(int(values.get("max_words") or 35))
            self.openai_temperature.setValue(
                float(values.get("temperature") or 0.4)
            )
            self.openai_timeout.setValue(
                float(values.get("timeout_seconds") or 30.0)
            )
            self.openai_attempts.setValue(
                int(values.get("max_generation_attempts") or 1)
            )
            self.openai_status.setText(
                "API-ключ сохранён" if values.get("has_api_key") else "API-ключ не сохранён"
            )
        finally:
            self._loading_openai_settings = False
        self._on_comment_source_changed()

    def _openai_configuration_payload(self) -> dict:
        return {
            "comment_source": self._current_comment_source(),
            "api_key": self.openai_api_key.text().strip() or None,
            "model": self.openai_model.text().strip(),
            "system_prompt": self.openai_system_prompt.toPlainText().strip(),
            "max_words": self.openai_max_words.value(),
            "temperature": self.openai_temperature.value(),
            "timeout_seconds": self.openai_timeout.value(),
            "max_generation_attempts": self.openai_attempts.value(),
        }

    def save_openai_configuration(self, _checked: bool = False, *, quiet: bool = False) -> bool:
        saver = getattr(self.adapter, "save_openai_configuration", None)
        if not callable(saver):
            if not quiet:
                QMessageBox.warning(self, APP_NAME, "Настройки OpenAI недоступны")
            return False
        try:
            values = dict(saver(self._openai_configuration_payload()) or {})
        except Exception as exc:
            self.openai_status.setText(f"Ошибка сохранения: {exc}")
            if not quiet:
                QMessageBox.warning(self, APP_NAME, str(exc))
            return False
        self.openai_api_key.clear()
        mask = str(values.get("api_key_mask") or "")
        self.openai_api_key.setPlaceholderText(
            f"Сохранён: {mask}" if mask else "Вставьте API-ключ OpenAI"
        )
        self.openai_status.setText("Настройки OpenAI сохранены")
        return True

    def test_openai_connection(self) -> None:
        if self._openai_test_future is not None:
            return
        if not self.save_openai_configuration(quiet=True):
            return
        post_text = self.openai_test_post.toPlainText().strip()
        if not post_text:
            post_text = (
                "В приложении обновили интерфейс и добавили безопасную генерацию "
                "комментариев с помощью OpenAI."
            )
            self.openai_test_post.setPlainText(post_text)
        try:
            self._openai_test_generation = self._account_generation
            self._openai_test_future = self.adapter.submit_openai_test(post_text)
        except Exception as exc:
            self.openai_status.setText(f"Проверка не запущена: {exc}")
            return
        self.openai_test_button.setEnabled(False)
        self.openai_status.setText("Проверка подключения…")
        self.openai_poll_timer.start()

    def _poll_openai_test(self) -> None:
        future = self._openai_test_future
        if future is None:
            self.openai_poll_timer.stop()
            return
        if not future.done():
            return
        self.openai_poll_timer.stop()
        self._openai_test_future = None
        self.openai_test_button.setEnabled(True)
        if (
            not self._page_active
            or self._openai_test_generation != self._account_generation
        ):
            return
        try:
            result = future.result()
            text = str((result or {}).get("text") or "").strip()
            if not text:
                raise RuntimeError("OpenAI вернул пустой тестовый результат")
            self.openai_preview.setPlainText(text)
            model = str((result or {}).get("model") or self.openai_model.text())
            self.openai_status.setText(
                f"Подключение работает · модель {model} · {len(text.split())} слов"
            )
        except Exception as exc:
            self.openai_preview.clear()
            self.openai_status.setText(f"Ошибка проверки: {exc}")

    def _cancel_openai_test(self) -> None:
        future = self._openai_test_future
        self._openai_test_future = None
        self._openai_test_generation = -1
        self.openai_poll_timer.stop()
        if future is not None and not future.done():
            future.cancel()
        if hasattr(self, "openai_test_button"):
            self.openai_test_button.setEnabled(True)

    def copy_openai_preview(self) -> None:
        text = self.openai_preview.toPlainText().strip()
        if text:
            QApplication.clipboard().setText(text)
            self.openai_status.setText("Тестовый комментарий скопирован")

    def use_openai_preview(self) -> None:
        text = self.openai_preview.toPlainText().strip()
        if not text:
            return
        self.editors[0].setPlainText(text)
        target = self.comment_source_combo.findData(SOURCE_PREWRITTEN)
        if target >= 0:
            self.comment_source_combo.setCurrentIndex(target)
        self._comments_dirty = True
        self._set_save_status("Тестовый результат добавлен в первое поле", saved=False)

    def set_page_active(self, active: bool) -> None:
        self._page_active = bool(active)
        if self._page_active:
            if not self.refresh_timer.isActive():
                self.refresh_timer.start()
            if not self.countdown_timer.isActive():
                self.countdown_timer.start()
            self.load_comments()
            self.load_openai_configuration()
            self.request_campaign_refresh()
        else:
            # Reject a queued result from a page that is no longer the active
            # owner and cancel the network request when possible.
            self._cancel_openai_test()
            self._account_generation += 1
            self._refresh_pending = False
            self.refresh_timer.stop()
            self.countdown_timer.stop()

    def _current_account_id(self) -> int:
        getter = getattr(self.adapter, "get_current_account_id", None)
        if not callable(getter):
            return 0
        try:
            return max(0, int(getter() or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def _set_next_check(self, value, *, fallback: str = "Следующая проверка: —") -> None:
        parsed = from_db_time(value)
        key = parsed.isoformat() if parsed is not None else ""
        if key != self._next_check_key:
            self._next_check_due_refresh_requested = False
        self._next_check_at = parsed
        self._next_check_key = key
        self._next_check_fallback = str(fallback or "Следующая проверка: —")
        self._update_next_check_label()

    def _clear_next_check(self, text: str = "Следующая проверка: —") -> None:
        self._next_check_at = None
        self._next_check_key = ""
        self._next_check_due_refresh_requested = False
        self._next_check_fallback = str(text or "Следующая проверка: —")
        self.next_label.setText(self._next_check_fallback)

    def _update_next_check_label(self) -> None:
        target = self._next_check_at
        if target is None:
            self.next_label.setText(self._next_check_fallback)
            return
        self.next_label.setText(
            countdown_label(
                "Следующая проверка",
                target,
                include_deadline=True,
                include_date=True,
            )
        )
        remaining = seconds_until(target)
        if (
            remaining == 0
            and self._page_active
            and self._refresh_job is None
            and not self._next_check_due_refresh_requested
        ):
            self._next_check_due_refresh_requested = True
            self.request_campaign_refresh()

    def _append_comment_editor(
        self, *, text: str = "", mark_dirty: bool = True
    ) -> None:
        if len(self.editors) >= MAX_COMMENT_VARIANTS:
            return
        index = len(self.editors)
        row = QFrame()
        row.setObjectName("commentVariantRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(10, 8, 10, 8)
        row_layout.setSpacing(10)

        selector = QRadioButton()
        selector.hide()
        selector.setToolTip("Служебный выбор варианта")
        selector.setAccessibleName(f"Выбрать комментарий {index + 1}")
        number = QLabel(str(index + 1))
        number.setObjectName("mutedText")
        number.setFixedWidth(24)
        number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        editor = QPlainTextEdit()
        editor.setPlaceholderText(f"Комментарий {index + 1}")
        editor.setMinimumHeight(70)
        editor.setMaximumHeight(92)
        editor.setMinimumWidth(0)
        editor.textChanged.connect(self._mark_comments_dirty)

        row_layout.addWidget(selector, 0)
        row_layout.addWidget(number, 0)
        row_layout.addWidget(editor, 1)
        self.comments_rows_layout.addWidget(row)
        self.variant_button_group.addButton(selector)
        self.variant_rows.append(row)
        self.variant_selectors.append(selector)
        self.variant_number_labels.append(number)
        self.editors.append(editor)
        if text:
            previous = self._loading_comments
            self._loading_comments = True
            try:
                editor.setPlainText(text)
            finally:
                self._loading_comments = previous
        if len(self.variant_selectors) == 1:
            selector.setChecked(True)
        self._refresh_variant_controls()
        if mark_dirty:
            self._mark_comments_dirty()

    def _remove_comment_editor(self, index: int, *, mark_dirty: bool = True) -> None:
        if len(self.editors) <= MAX_COMMENT_VARIANTS:
            return
        row = self.variant_rows.pop(index)
        selector = self.variant_selectors.pop(index)
        self.variant_button_group.removeButton(selector)
        self.variant_number_labels.pop(index)
        self.editors.pop(index)
        self.comments_rows_layout.removeWidget(row)
        row.deleteLater()
        for position, (number, editor, selector) in enumerate(
            zip(self.variant_number_labels, self.editors, self.variant_selectors),
            start=1,
        ):
            number.setText(str(position))
            editor.setPlaceholderText(f"Комментарий {position}")
            selector.setAccessibleName(f"Выбрать комментарий {position}")
        if self.variant_selectors and not any(
            selector.isChecked() for selector in self.variant_selectors
        ):
            self.variant_selectors[
                min(index, len(self.variant_selectors) - 1)
            ].setChecked(True)
        self._refresh_variant_controls()
        if mark_dirty:
            self._mark_comments_dirty()

    def _set_editor_count(self, count: int) -> None:
        del count
        target = MAX_COMMENT_VARIANTS
        while len(self.editors) < target:
            self._append_comment_editor(mark_dirty=False)
        while len(self.editors) > target:
            self._remove_comment_editor(len(self.editors) - 1, mark_dirty=False)
        self._refresh_variant_controls()

    def _refresh_variant_controls(self) -> None:
        self.variant_count_label.setText(f"{MAX_COMMENT_VARIANTS} полей")
        if hasattr(self, "add_variant_button"):
            self.add_variant_button.setEnabled(False)
        if hasattr(self, "delete_variant_button"):
            self.delete_variant_button.setEnabled(False)

    def add_comment_variant(self) -> None:
        """Compatibility slot: the current UI always contains ten fields."""
        self._set_editor_count(MAX_COMMENT_VARIANTS)

    def delete_selected_variant(self) -> None:
        """Compatibility slot: fixed ten-field profiles cannot remove rows."""
        self._set_editor_count(MAX_COMMENT_VARIANTS)

    def import_previous_account_comments(self) -> None:
        importer = getattr(self.adapter, "import_previous_comment_profile", None)
        if not callable(importer):
            QMessageBox.information(
                self,
                "Импорт комментариев",
                "Импорт из предыдущего аккаунта недоступен в этой сборке.",
            )
            return
        try:
            profile = importer()
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if not profile:
            QMessageBox.information(
                self,
                "Импорт комментариев",
                "Не найден предыдущий аккаунт с сохранёнными вариантами.",
            )
            return
        self._apply_comment_profile(profile, force=True)
        source_account_id = int(profile.get("source_account_id") or 0)
        self._comments_dirty = False
        self._set_save_status(
            f"✓ Импортировано из аккаунта {source_account_id}", saved=True
        )

    def handle_account_changed(self) -> None:
        """Invalidate old async reads and synchronously clear the previous owner."""

        self._cancel_openai_test()
        self._account_generation += 1
        self._refresh_pending = self._refresh_job is not None
        account_id = self._current_account_id()
        self._apply_campaign_snapshot(
            {
                "account_id": account_id,
                "generation": self._account_generation,
                "state": None,
                "channels": [],
                "history": [],
            }
        )
        self.load_comments(force=True)
        self.load_openai_configuration()
        self.refresh_campaign()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        parent = self.parentWidget()
        viewport_width = parent.width() if parent is not None else self.width()
        self._apply_compact(viewport_width < 700)

    def set_compact_mode(self, compact: bool) -> None:
        self._apply_compact(bool(compact))

    def _apply_compact(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        for editor in self.editors:
            editor.setMinimumHeight(62 if compact else 70)
            editor.setMaximumHeight(84 if compact else 92)

        for button in (
            self.start_button,
            self.pause_button,
            self.resume_button,
            self.stop_button,
        ):
            self.controls_grid.removeWidget(button)
        if compact:
            self.controls_grid.addWidget(self.start_button, 0, 0)
            self.controls_grid.addWidget(self.pause_button, 0, 1)
            self.controls_grid.addWidget(self.resume_button, 1, 0)
            self.controls_grid.addWidget(self.stop_button, 1, 1)
            self.controls_grid.setColumnStretch(2, 0)
            self.main_layout.setContentsMargins(18, 20, 18, 20)
        else:
            self.controls_grid.addWidget(self.start_button, 0, 0)
            self.controls_grid.addWidget(self.pause_button, 0, 1)
            self.controls_grid.addWidget(self.resume_button, 0, 2)
            self.controls_grid.addWidget(self.stop_button, 0, 3)
            self.controls_grid.setColumnStretch(4, 1)
            self.main_layout.setContentsMargins(34, 28, 34, 28)

        for widget in (
            self.status,
            self.period_label,
            self.next_label,
            self.count_label,
        ):
            self.campaign_layout.removeWidget(widget)
        if compact:
            self.campaign_layout.addWidget(self.status, 0, 0)
            self.campaign_layout.addWidget(self.period_label, 1, 0)
            self.campaign_layout.addWidget(self.next_label, 2, 0)
            self.campaign_layout.addWidget(self.count_label, 3, 0)
        else:
            self.campaign_layout.addWidget(self.status, 0, 0, 1, 2)
            self.campaign_layout.addWidget(self.period_label, 1, 0)
            self.campaign_layout.addWidget(self.next_label, 1, 1)
            self.campaign_layout.addWidget(self.count_label, 2, 0, 1, 2)

    def load_comments(self, *, force: bool = False):
        profile_getter = getattr(self.adapter, "get_comment_profile", None)
        if callable(profile_getter):
            profile = profile_getter()
        else:
            comments = list(self.adapter.get_main_comments() or [])
            profile = {
                "account_id": 0,
                "visible_count": MAX_COMMENT_VARIANTS,
                "comments": comments,
            }
        current_account_id = int(profile.get("account_id") or 0)
        account_changed = (
            self._loaded_account_id is not None
            and current_account_id != self._loaded_account_id
        )
        # Periodic/page reloads must preserve unsaved text for the same account.
        # An actual account switch must never display or save the previous
        # account's unsaved variants under the new Telegram identity.
        if self._comments_dirty and not force and not account_changed:
            return
        self._apply_comment_profile(profile, force=True)

    def _apply_comment_profile(self, profile: dict, *, force: bool) -> None:
        comments = list(profile.get("comments") or [])[:MAX_COMMENT_VARIANTS]
        self._set_editor_count(MAX_COMMENT_VARIANTS)
        self._loading_comments = True
        try:
            for index, editor in enumerate(self.editors):
                text = str(comments[index] if index < len(comments) else "")
                if force or not editor.hasFocus():
                    editor.setPlainText(text)
        finally:
            self._loading_comments = False
        self._loaded_account_id = int(profile.get("account_id") or 0)
        self._comments_dirty = False
        self._set_save_status("Комментарии загружены", saved=True)

    def _all_comments(self) -> list[str]:
        return [editor.toPlainText().strip() for editor in self.editors]

    def _active_comments(self) -> list[str]:
        return [text for text in self._all_comments() if text]

    def _mark_comments_dirty(self):
        if self._loading_comments:
            return
        self._comments_dirty = True
        self._set_save_status("Есть несохранённые изменения", saved=False)

    def _set_save_status(self, text: str, *, saved: bool) -> None:
        self.save_status.setText(text)
        self.save_status.setObjectName(
            "saveStatusSaved" if saved else "saveStatusDirty"
        )
        self.save_status.style().unpolish(self.save_status)
        self.save_status.style().polish(self.save_status)

    def load_daily_limit(self) -> None:
        try:
            value = int(self.adapter.get_comment_daily_limit())
        except Exception:
            value = 40
        self._loading_daily_limit = True
        try:
            self.daily_limit_slider.setValue(max(0, min(1000, value)))
            self._update_daily_limit_text(self.daily_limit_slider.value())
        finally:
            self._loading_daily_limit = False

    def _on_daily_limit_changed(self, value: int) -> None:
        self._update_daily_limit_text(int(value))
        if not self._loading_daily_limit:
            self.limit_save_timer.start()

    def _update_daily_limit_text(self, value: int) -> None:
        value = max(0, min(1000, int(value)))
        self.daily_limit_value.setText(str(value))
        if value <= 0:
            hint = "0 — кампания отключена. Для запуска выберите значение от 1 до 1000."
        else:
            average_seconds = 86_400 / value
            if average_seconds >= 3600:
                interval = f"примерно {average_seconds / 3600:.1f} ч"
            elif average_seconds >= 60:
                interval = f"примерно {average_seconds / 60:.1f} мин"
            else:
                interval = f"примерно {average_seconds:.0f} сек"
            hint = (
                f"Темп: до {value} комментариев за 24 часа; "
                f"средний интервал — {interval}. Для каждого комментария "
                "создаётся отдельное случайное время внутри его части суток — "
                "интервал не фиксированный. Вступления выполняются заранее во "
                "вкладке «Связки» с паузой 15–25 секунд; кампания комментариев "
                "сама не вступает в обсуждения. "
                "FLOOD_WAIT и сетевые сбои заранее не рассчитываются и могут "
                "сдвинуть завершение. Фактически будут запланированы только "
                "доступные уникальные каналы и группы."
            )
            if value > 200:
                hint += " Очень высокая частота повышает риск ограничений Telegram."
        self.daily_limit_hint.setText(hint)
        if value <= 40:
            self.recommended_load.setText(
                "Рекомендуемая нагрузка: 40 комментариев в сутки"
            )
        else:
            self.recommended_load.setText(
                "Рекомендуемая нагрузка: 40 комментариев в сутки · выбранное значение выше рекомендации"
            )

    def _save_daily_limit(self) -> bool:
        try:
            saved = int(
                self.adapter.set_comment_daily_limit(self.daily_limit_slider.value())
            )
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return False
        if saved != self.daily_limit_slider.value():
            self._loading_daily_limit = True
            try:
                self.daily_limit_slider.setValue(saved)
            finally:
                self._loading_daily_limit = False
            self._update_daily_limit_text(saved)
        return True

    def save_comments(self) -> bool:
        try:
            saver = getattr(self.adapter, "save_comment_profile", None)
            if callable(saver):
                profile = saver(
                    self._all_comments(),
                    visible_count=MAX_COMMENT_VARIANTS,
                    account_id=self._loaded_account_id,
                )
                if isinstance(profile, dict):
                    self._loaded_account_id = int(
                        profile.get("account_id") or self._loaded_account_id or 0
                    )
            else:
                self.adapter.save_comment_template(self._all_comments())
        except Exception as exc:
            self._set_save_status("Не удалось сохранить комментарии", saved=False)
            QMessageBox.warning(self, APP_NAME, str(exc))
            return False
        self._comments_dirty = False
        self._set_save_status("✓ Комментарии сохранены", saved=True)
        return True

    def start_campaign(self):
        self.limit_save_timer.stop()
        current = self.adapter.get_comment_campaign_state()
        if current and str(current.get("status") or "") in {
            "running",
            "paused",
            "network_wait",
            "cycle_wait",
        }:
            # A stale enabled button must never act as a manual "refresh" or
            # create a second campaign. Repaint the existing state instead.
            self._apply_campaign_snapshot(self._load_campaign_snapshot())
            return
        daily_limit = int(self.daily_limit_slider.value())
        if daily_limit <= 0:
            QMessageBox.information(
                self,
                "Кампания отключена",
                "Выберите количество комментариев в сутки от 1 до 1000",
            )
            return
        if not self._save_daily_limit():
            return
        source = self._current_comment_source()
        all_comments = self._all_comments()
        comments = [text for text in all_comments if text]
        if source == SOURCE_PREWRITTEN and not comments:
            QMessageBox.information(
                self, "Нет комментариев", "Добавьте хотя бы один вариант комментария"
            )
            return
        if source == SOURCE_OPENAI and not self.save_openai_configuration(quiet=True):
            return
        if not self.adapter.get_channels():
            QMessageBox.information(
                self, "Нет каналов", "Сначала получите список каналов"
            )
            return
        if not self.adapter.get_commenting_channels():
            QMessageBox.information(
                self,
                "Нет рабочих целей",
                "Сначала получите каналы и группы, затем выполните проверку во вкладке «Связки»",
            )
            return
        try:
            campaign = self.adapter.start_comment_campaign(
                all_comments,
                continuous=self.continuous.isChecked(),
                daily_limit=daily_limit,
                comment_source=source,
            )
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.current_campaign_id = int(campaign["id"])
        self._comments_dirty = False
        if source == SOURCE_PREWRITTEN:
            self._set_save_status("✓ Комментарии сохранены и кампания запущена", saved=True)
        else:
            self.openai_status.setText("OpenAI-кампания запущена · отправка автоматическая")
        self.table.setRowCount(0)
        self.refresh_campaign()

    def pause_campaign(self):
        if not self.adapter.pause_comment_campaign():
            QMessageBox.information(
                self, "Кампания", "Активную кампанию не удалось поставить на паузу"
            )
        self.refresh_campaign()

    def resume_campaign(self):
        if not self.adapter.resume_comment_campaign():
            QMessageBox.information(
                self, "Кампания", "Нет приостановленной кампании для продолжения"
            )
        self.refresh_campaign()

    def stop_campaign(self):
        state = self.adapter.get_comment_campaign_state()
        if not state or state.get("status") not in {
            "running",
            "paused",
            "network_wait",
            "cycle_wait",
        }:
            return
        answer = QMessageBox.question(
            self,
            "Остановить кампанию",
            "Будущие слоты будут отменены. Уже начатая операция завершится безопасно. Продолжить?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.adapter.stop_comment_campaign()
        self.refresh_campaign()

    def _load_campaign_snapshot(
        self,
        *,
        account_id: int | None = None,
        generation: int | None = None,
    ) -> dict[str, object]:
        owner_account_id = (
            self._current_account_id() if account_id is None else max(0, int(account_id))
        )
        owner_generation = (
            self._account_generation if generation is None else int(generation)
        )

        state_getter = self.adapter.get_comment_campaign_state
        try:
            state = state_getter(account_id=owner_account_id)
        except TypeError:  # compatibility with lightweight test adapters
            state = state_getter()
        channels: list[dict] = []
        history: list[dict] = []
        if state and owner_account_id > 0:
            campaign_id = int(state["id"])
            try:
                channels = list(
                    self.adapter.get_channels(account_id=owner_account_id) or []
                )
            except TypeError:
                channels = list(self.adapter.get_channels() or [])
            try:
                history = list(
                    self.adapter.get_comment_history(
                        campaign_id=campaign_id,
                        limit=200,
                        account_id=owner_account_id,
                    )
                    or []
                )
            except TypeError:
                history = list(
                    self.adapter.get_comment_history(
                        campaign_id=campaign_id, limit=200
                    )
                    or []
                )
        return {
            "account_id": owner_account_id,
            "generation": owner_generation,
            "state": state,
            "channels": channels,
            "history": history,
        }

    def _snapshot_is_current(self, snapshot: dict[str, object]) -> bool:
        try:
            snapshot_account_id = max(0, int(cast(int, snapshot.get("account_id")) or 0))
            snapshot_generation = int(
                cast(int, snapshot.get("generation", self._account_generation))
            )
        except (TypeError, ValueError, OverflowError):
            return False
        return (
            snapshot_account_id == self._current_account_id()
            and snapshot_generation == self._account_generation
        )

    def request_campaign_refresh(self) -> None:
        """Poll SQLite in the thread pool without accepting stale account data."""

        if not self._page_active:
            return
        if self._refresh_job is not None:
            self._refresh_pending = True
            return
        account_id = self._current_account_id()
        generation = self._account_generation
        cleanup = getattr(self.adapter, "close_thread_connection", None)

        def load_snapshot() -> dict[str, object]:
            return self._load_campaign_snapshot(
                account_id=account_id, generation=generation
            )

        job = BackgroundCall(
            load_snapshot,
            cleanup=cleanup if callable(cleanup) else None,
        )
        self._refresh_job = job

        def succeeded(view: CommentingView, snapshot: object) -> None:
            if not isinstance(snapshot, dict):
                if generation == view._account_generation:
                    view._handle_refresh_error("Некорректный снимок состояния")
                return
            if not view._snapshot_is_current(snapshot):
                view._refresh_pending = True
                return
            try:
                view._apply_campaign_snapshot(snapshot)
            except Exception as exc:  # noqa: BLE001 - Qt signal boundary
                view._handle_refresh_error(f"{type(exc).__name__}: {exc}")
            else:
                view._last_refresh_error = ""

        def failed(view: CommentingView, message: str) -> None:
            if (
                generation == view._account_generation
                and account_id == view._current_account_id()
            ):
                view._handle_refresh_error(message)

        def finished(view: CommentingView) -> None:
            if view._refresh_job is job:
                view._refresh_job = None
            if view._refresh_pending and view._page_active:
                view._refresh_pending = False
                QTimer.singleShot(0, view.request_campaign_refresh)

        connect_lifecycle_safe(
            job,
            self,
            succeeded=succeeded,
            failed=failed,
            finished=finished,
        )
        QThreadPool.globalInstance().start(job)

    def _handle_refresh_error(self, message: str) -> None:
        message = str(message or "Неизвестная ошибка")
        if message != self._last_refresh_error:
            log.warning("Could not refresh comment campaign UI: %s", message)
            self._last_refresh_error = message
        self.status.setText("Не удалось обновить статус кампании")
        if self._next_check_at is None:
            self.next_label.setText("Следующая проверка: временно недоступна")

    def refresh_campaign(self):
        """Synchronous refresh for explicit actions; timer polling is asynchronous."""

        try:
            self._refresh_campaign()
        except Exception as exc:  # noqa: BLE001 - explicit UI callback boundary
            CommentingView._handle_refresh_error(self, f"{type(exc).__name__}: {exc}")
            return
        self._last_refresh_error = ""

    def _refresh_campaign(self):
        self._apply_campaign_snapshot(self._load_campaign_snapshot())

    def _apply_campaign_snapshot(self, snapshot: dict[str, object]) -> bool:
        if ("account_id" in snapshot or "generation" in snapshot) and not self._snapshot_is_current(snapshot):
            return False
        state = snapshot.get("state")
        if not isinstance(state, dict):
            self.current_campaign_id = None
            self.status.setText("Кампания не запущена")
            self.period_label.setText("Период: —")
            self._clear_next_check("Следующая проверка: —")
            selected_limit = int(self.daily_limit_slider.value())
            self.count_label.setText(
                f"Выполнено: 0 из {selected_limit} · отправлено: 0"
            )
            self.progress.setValue(0)
            self._set_buttons("none")
            self.table.setRowCount(0)
            self.channel_names = {}
            return True

        self.current_campaign_id = int(state["id"])
        status = str(state.get("status") or "")
        labels = {
            "running": "Кампания работает",
            "paused": "Кампания на паузе",
            "network_wait": "Кампания ожидает сеть",
            "cycle_wait": "Текущий цикл выполнен",
            "completed": "Суточный период завершён",
            "stopped": "Кампания остановлена",
        }
        title = labels.get(status, f"Статус: {status}")
        reason = str(state.get("pause_reason") or "").strip()
        self.status.setText(title + (f" · {reason}" if reason else ""))
        self.period_label.setText(
            f"Период: {state.get('started_display', '—')} — {state.get('ends_display', '—')}"
        )
        if status == "paused":
            self._clear_next_check("Следующая проверка: после продолжения")
        elif status in {"completed", "stopped"}:
            self._clear_next_check("Следующая проверка: —")
        else:
            self._set_next_check(
                state.get("next_scheduled_at"),
                fallback="Следующая проверка: ожидается планирование",
            )
        attempted = int(state.get("attempted_count") or 0)
        sent = int(state.get("sent_count") or 0)
        limit = max(1, int(state.get("daily_limit") or 40))
        planned = max(1, int(state.get("planned_count") or limit))
        self.count_label.setText(
            f"Выполнено: {attempted} из {planned} · отправлено: {sent} · темп: {limit}/24 ч"
        )
        self.progress.setValue(min(100, round(attempted * 100 / planned)))
        self._set_buttons(status)

        channel_rows = snapshot.get("channels")
        self.channel_names = {
            int(row["channel_id"]): row.get("title") or str(row["channel_id"])
            for row in (list(channel_rows) if isinstance(channel_rows, list) else [])
            if isinstance(row, dict) and row.get("channel_id") is not None
        }
        history_rows = snapshot.get("history")
        rows = list(history_rows) if isinstance(history_rows, list) else []
        self._render_history(rows)
        return True

    def _render_history(self, rows: list[dict]) -> None:
        self.table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            channel_id = item.get("channel_id")
            values = [
                self.channel_names.get(int(channel_id), str(channel_id))
                if channel_id is not None
                else "—",
                str(item.get("post_id") or "обычное сообщение"),
                str(item.get("comment_text") or "—"),
                str(item.get("status") or ""),
            ]
            for column, value in enumerate(values):
                widget_item = QTableWidgetItem(value)
                if column == 1:
                    widget_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, widget_item)

    def _set_buttons(self, status: str):
        if status in {"running", "paused", "network_wait", "cycle_wait"}:
            self.limit_save_timer.stop()
        self.start_button.setEnabled(
            status not in {"running", "paused", "network_wait", "cycle_wait"}
        )
        self.pause_button.setEnabled(status == "running")
        self.resume_button.setEnabled(status == "paused")
        self.stop_button.setEnabled(
            status in {"running", "paused", "network_wait", "cycle_wait"}
        )
        editors_enabled = status not in {
            "running",
            "paused",
            "network_wait",
            "cycle_wait",
        }
        for editor in self.editors:
            editor.setEnabled(editors_enabled)
        self.save_comments_button.setEnabled(editors_enabled)
        self.add_variant_button.setEnabled(False)
        self.delete_variant_button.setEnabled(False)
        self.import_previous_button.setEnabled(editors_enabled)
        self.continuous.setEnabled(editors_enabled)
        self.daily_limit_slider.setEnabled(editors_enabled)

    def load_history(self):
        if self.current_campaign_id is None:
            self.table.setRowCount(0)
            return
        self.channel_names = {
            int(row["channel_id"]): row.get("title") or str(row["channel_id"])
            for row in self.adapter.get_channels()
        }
        rows = self.adapter.get_comment_history(
            campaign_id=self.current_campaign_id,
            limit=200,
        )
        self._render_history(list(rows or []))

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

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from gui.views.commenting_view import CommentingView

class CommentingOpenAIMixin:
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

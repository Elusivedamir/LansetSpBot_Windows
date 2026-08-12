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

class CommentingDailyLimitMixin:
    def load_daily_limit(self) -> None:
        account_id = self._current_account_id()
        self._daily_limit_account_id = (
            account_id if account_id > 0 else None
        )
        try:
            value = int(
                self.adapter.get_comment_daily_limit(
                    account_id=self._daily_limit_account_id
                )
            )
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
                "вкладке «Связки» с паузой 45–70 секунд; кампания комментариев "
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
    def _save_daily_limit(
        self,
        *,
        account_id: int | None = None,
    ) -> bool:
        target_account_id = int(
            account_id
            or self._daily_limit_account_id
            or self._current_account_id()
            or 0
        )
        if target_account_id <= 0:
            QMessageBox.warning(
                self,
                APP_NAME,
                "Сначала выберите Telegram-аккаунт",
            )
            return False
        try:
            saved = int(
                self.adapter.set_comment_daily_limit(
                    self.daily_limit_slider.value(),
                    account_id=target_account_id,
                )
            )
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return False
        if (
            saved != self.daily_limit_slider.value()
            and self._daily_limit_account_id == target_account_id
        ):
            self._loading_daily_limit = True
            try:
                self.daily_limit_slider.setValue(saved)
            finally:
                self._loading_daily_limit = False
            self._update_daily_limit_text(saved)
        return True

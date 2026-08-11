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

class CommentingProfileMixin:
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
            account_getter = getattr(self, "_current_account_id", None)
            if callable(account_getter) and account_getter() <= 0:
                # The combo emits while the page is being constructed before an
                # account is selected. UI state may change, but there is no
                # account-scoped setting to persist yet.
                return
            try:
                self.adapter.save_openai_configuration(
                    {"comment_source": source}
                )
            except Exception:
                log.exception("Could not persist comment source selection")
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

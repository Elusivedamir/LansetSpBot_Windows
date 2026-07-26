"""Rendering regressions on the first page the user ever sees.

Both defects here were found by actually rendering the account page offscreen
and measuring the widgets, not by reading the stylesheet.
"""

from __future__ import annotations

import sys

import pytest
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QLabel,
    QLineEdit,
    QTimeEdit,
)

from core.composition import ApplicationContainer
from core.config import Config
from gui.app import LansetSpBotApp


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture()
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLEN_DATA_DIR", str(tmp_path))
    application = _app()
    config = Config()
    container = ApplicationContainer(config)
    container.database.reset_running_tasks()
    main = LansetSpBotApp(container.adapter, container.queue_worker, config)
    main.resize(1360, 940)
    main.show()
    application.processEvents()
    try:
        yield main, application
    finally:
        try:
            main._tray.hide()  # noqa: SLF001
        except Exception:
            pass
        main.deleteLater()
        application.processEvents()
        container.shutdown(timeout_ms=15_000)


def _clipped_labels(root) -> list[tuple[str, int, int]]:
    clipped: list[tuple[str, int, int]] = []
    for label in root.findChildren(QLabel):
        text = label.text()
        if not text or len(text) > 60 or not label.isVisible() or label.wordWrap():
            continue
        needed = label.fontMetrics().horizontalAdvance(text)
        available = label.width()
        if available > 0 and needed > available + 1:
            clipped.append((text, needed, available))
    return clipped


def test_no_label_on_the_first_page_is_clipped(window) -> None:
    """The sidebar used to render the product name as "LANSETSPBO"."""

    main, application = window
    main.stack.setCurrentIndex(0)
    application.processEvents()
    assert _clipped_labels(main) == []


def test_the_product_name_is_rendered_in_full(window) -> None:
    main, application = window
    application.processEvents()
    brand = next(
        label
        for label in main.findChildren(QLabel)
        if label.objectName() == "brandTitle"
    )
    assert brand.text() == "LANSETSPBOT"
    needed = brand.fontMetrics().horizontalAdvance(brand.text())
    assert needed <= brand.width(), (
        f"the sidebar clips the product name by {needed - brand.width()}px"
    )


def test_time_fields_are_themed_like_every_other_input(window) -> None:
    """QTimeEdit derives from QAbstractSpinBox, not QSpinBox.

    The stylesheet used to list QSpinBox explicitly, so the quiet-hours fields
    fell through to the native light widget and collapsed to 19px tall on a
    dark page.
    """

    main, application = window
    application.processEvents()
    account = main.account_view
    reference = account.api_id
    assert isinstance(reference, QLineEdit)

    for field in (account.quiet_start, account.quiet_end):
        assert isinstance(field, QTimeEdit)
        assert isinstance(field, QAbstractSpinBox)
        assert field.height() >= reference.height() - 2, (
            f"{field.objectName() or 'time field'} is {field.height()}px tall "
            f"while a themed input is {reference.height()}px"
        )


def test_the_stylesheet_targets_the_spin_box_base_class() -> None:
    """Guard the selector itself so the regression cannot silently return."""

    from gui.theme import TELEGRAM_PREMIUM_QSS

    assert "QAbstractSpinBox" in TELEGRAM_PREMIUM_QSS
    assert "QAbstractSpinBox::up-button" in TELEGRAM_PREMIUM_QSS

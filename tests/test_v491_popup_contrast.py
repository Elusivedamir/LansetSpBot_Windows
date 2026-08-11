# Popup and functional-toggle contrast must match the operator-facing design.
#
# The tray remains dark. Selection popups intentionally use a light surface
# with dark text, and every functional QCheckBox uses red for OFF and green
# for ON. The checks render real Qt surfaces instead of trusting text alone.

from __future__ import annotations

import sys

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QComboBox

from core.composition import ApplicationContainer
from core.config import Config
from gui.app import LansetSpBotApp

MAX_DARK_BACKGROUND_LUMINANCE = 120.0
MIN_LIGHT_BACKGROUND_LUMINANCE = 150.0
MIN_DARK_CONTRAST_RANGE = 120.0
MIN_LIGHT_CONTRAST_RANGE = 70.0


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


def _luminances(image: QImage) -> list[float]:
    values = []
    for x in range(0, image.width(), 4):
        for y in range(0, image.height(), 4):
            colour = image.pixelColor(x, y)
            values.append(
                0.2126 * colour.red()
                + 0.7152 * colour.green()
                + 0.0722 * colour.blue()
            )
    return values


def _surface_metrics(image: QImage, what: str) -> tuple[float, float]:
    assert image.width() > 0 and image.height() > 0, f"{what} rendered nothing"
    values = _luminances(image)
    return sum(values) / len(values), max(values) - min(values)


def test_the_tray_menu_remains_dark_and_readable(window) -> None:
    main, application = window
    menu = main._tray.contextMenu()  # noqa: SLF001
    assert menu is not None
    menu.show()
    application.processEvents()
    menu.resize(menu.sizeHint())
    application.processEvents()
    try:
        mean, spread = _surface_metrics(menu.grab().toImage(), "the tray menu")
        assert mean <= MAX_DARK_BACKGROUND_LUMINANCE
        assert spread >= MIN_DARK_CONTRAST_RANGE
    finally:
        menu.hide()
        application.processEvents()


def test_the_comment_source_list_is_light_with_dark_readable_text(window) -> None:
    main, application = window
    combo = main.commenting_view.findChild(QComboBox)
    assert combo is not None
    assert [combo.itemText(index) for index in range(combo.count())] == [
        "Готовые тексты",
        "OpenAI",
    ]
    combo.showPopup()
    for _ in range(3):
        application.processEvents()
    try:
        mean, spread = _surface_metrics(
            combo.view().viewport().grab().toImage(), "the source list"
        )
        assert mean >= MIN_LIGHT_BACKGROUND_LUMINANCE
        assert spread >= MIN_LIGHT_CONTRAST_RANGE
    finally:
        combo.hidePopup()
        application.processEvents()


def test_the_stylesheet_covers_popup_surfaces_and_toggle_states() -> None:
    from gui.theme import TELEGRAM_PREMIUM_QSS

    for selector in (
        "QMenu {",
        "QMenu::item",
        "QMenu::item:selected",
        "QComboBox QAbstractItemView",
        "QMessageBox {",
        "QMessageBox QLabel",
        "QMessageBox QPushButton",
        "QToolTip",
    ):
        assert selector in TELEGRAM_PREMIUM_QSS, f"{selector} is not styled"

    assert "background: #E6EBF1;" in TELEGRAM_PREMIUM_QSS
    assert "color: #17202A;" in TELEGRAM_PREMIUM_QSS
    assert "QCheckBox::indicator:checked" in TELEGRAM_PREMIUM_QSS
    assert "background: #9C3542;" in TELEGRAM_PREMIUM_QSS
    assert "background: #238B57;" in TELEGRAM_PREMIUM_QSS

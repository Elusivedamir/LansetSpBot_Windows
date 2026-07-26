"""Popups must be readable, and readability is measured, not assumed.

Reported from a real screen: the tray menu and the comment-source list showed
near-white text on a near-white background. Both are separate top-level
windows, and neither had a rule in the stylesheet, so they fell back to the
system palette while inheriting the dark theme's light text colour.

Rendering them offscreen and measuring luminance is what catches this; reading
the stylesheet cannot tell you what a popup ends up looking like. Before the
fix the tray menu measured a mean luminance of 240 out of 255 - a white sheet.
"""

from __future__ import annotations

import sys

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QComboBox

from core.composition import ApplicationContainer
from core.config import Config
from gui.app import LansetSpBotApp

# A popup whose whole surface sits above this is a light rectangle; the dark
# theme's own panels measure around 20-40.
MAX_BACKGROUND_LUMINANCE = 120.0
# Text has to stand out from the surface it is drawn on.
MIN_CONTRAST_RANGE = 120.0


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
                0.2126 * colour.red() + 0.7152 * colour.green() + 0.0722 * colour.blue()
            )
    return values


def _assert_readable(image: QImage, what: str) -> None:
    assert image.width() > 0 and image.height() > 0, f"{what} rendered nothing"
    values = _luminances(image)
    mean = sum(values) / len(values)
    spread = max(values) - min(values)
    assert mean <= MAX_BACKGROUND_LUMINANCE, (
        f"{what} is a light sheet (mean luminance {mean:.1f}); "
        "text on it is invisible against the dark theme"
    )
    assert spread >= MIN_CONTRAST_RANGE, (
        f"{what} has no contrast (range {spread:.1f}): "
        "text and background are the same shade"
    )


def test_the_tray_menu_is_readable(window) -> None:
    main, application = window
    menu = main._tray.contextMenu()  # noqa: SLF001
    assert menu is not None
    menu.show()
    application.processEvents()
    menu.resize(menu.sizeHint())
    application.processEvents()
    try:
        _assert_readable(menu.grab().toImage(), "the tray menu")
    finally:
        menu.hide()
        application.processEvents()


def test_the_comment_source_list_is_readable(window) -> None:
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
        _assert_readable(combo.view().viewport().grab().toImage(), "the source list")
    finally:
        combo.hidePopup()
        application.processEvents()


def test_the_stylesheet_covers_every_popup_surface() -> None:
    """Guard the rules themselves so the popups cannot lose them silently."""

    from gui.theme import TELEGRAM_PREMIUM_QSS

    for selector in (
        "QMenu {",
        "QMenu::item",
        "QMenu::item:selected",
        "QComboBox QAbstractItemView",
        "QToolTip",
    ):
        assert selector in TELEGRAM_PREMIUM_QSS, f"{selector} is not styled"

"""A switch must say which way it is set, by colour, at a glance.

Both states used to be shades of the same grey-blue - #464B55 off and #6E84AD
on - which on a dark page is a difference an operator has to look for. On is
now green and off is red, and the test measures the rendered pixels rather
than trusting the stylesheet text.
"""

from __future__ import annotations

import sys

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QCheckBox

from core.composition import ApplicationContainer
from core.config import Config
from gui.app import LansetSpBotApp

CHANNEL_MARGIN = 40


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


def _indicator_colour(box: QCheckBox, application: QApplication):
    application.processEvents()
    image: QImage = box.grab().toImage()
    # The indicator is drawn at the leading edge of the row.
    return image.pixelColor(20, image.height() // 2)


def test_an_enabled_switch_reads_green(window) -> None:
    main, application = window
    box = main.account_view.proxy_enabled
    box.setChecked(True)
    colour = _indicator_colour(box, application)
    assert colour.green() > colour.red() + CHANNEL_MARGIN, (
        f"an enabled switch is not green: {colour.getRgb()[:3]}"
    )
    assert colour.green() > colour.blue() + CHANNEL_MARGIN


def test_a_disabled_switch_reads_red(window) -> None:
    main, application = window
    box = main.account_view.proxy_enabled
    box.setChecked(False)
    colour = _indicator_colour(box, application)
    assert colour.red() > colour.green() + CHANNEL_MARGIN, (
        f"a switch that is off is not red: {colour.getRgb()[:3]}"
    )
    assert colour.red() > colour.blue() + CHANNEL_MARGIN


def test_the_two_states_are_far_apart(window) -> None:
    """The regression was two shades of one hue, not an absent colour."""

    main, application = window
    box = main.account_view.proxy_enabled
    box.setChecked(False)
    off = _indicator_colour(box, application)
    box.setChecked(True)
    on = _indicator_colour(box, application)
    distance = (
        abs(on.red() - off.red())
        + abs(on.green() - off.green())
        + abs(on.blue() - off.blue())
    )
    assert distance > 200, f"the two states look alike (distance {distance})"


def test_every_switch_on_the_account_page_follows_the_same_rule(window) -> None:
    main, application = window
    boxes = [
        box
        for box in main.account_view.findChildren(QCheckBox)
        if box.isVisible() and box.isEnabled()
    ]
    assert boxes, "the account page has no switches to check"
    for box in boxes:
        previous = box.isChecked()
        try:
            box.setChecked(True)
            colour = _indicator_colour(box, application)
            assert colour.green() > colour.red(), (
                f"{box.text()!r} is not green when enabled"
            )
            box.setChecked(False)
            colour = _indicator_colour(box, application)
            assert colour.red() > colour.green(), (
                f"{box.text()!r} is not red when disabled"
            )
        finally:
            box.setChecked(previous)

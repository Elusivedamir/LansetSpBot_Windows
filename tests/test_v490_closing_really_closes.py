"""Closing the window must close the application, not hide it.

Hiding into the tray left a live process holding the database and the Telegram
session while the operator believed the program was closed. Starting it again
was the natural thing to do, and copies accumulated - an operator found three
running at once, and the next authorization attempt failed with
"OperationalError: database is locked".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from core.composition import ApplicationContainer
from core.config import Config
from gui.app import LansetSpBotApp

ROOT = Path(__file__).resolve().parents[1]


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


def test_closing_asks_first_and_then_quits(window, monkeypatch) -> None:
    main, application = window
    asked: list[bool] = []
    quit_called: list[bool] = []
    monkeypatch.setattr(main, "confirm_close", lambda: asked.append(True) or True)
    monkeypatch.setattr(main, "quit_application", lambda: quit_called.append(True))

    event = QCloseEvent()
    main.closeEvent(event)
    application.processEvents()

    assert asked == [True], "closing must ask before ending the session"
    assert quit_called == [True], "a confirmed close must quit, not hide"


def test_declining_the_prompt_keeps_the_application_running(
    window, monkeypatch
) -> None:
    main, application = window
    quit_called: list[bool] = []
    monkeypatch.setattr(main, "confirm_close", lambda: False)
    monkeypatch.setattr(main, "quit_application", lambda: quit_called.append(True))

    main.show()
    application.processEvents()
    event = QCloseEvent()
    main.closeEvent(event)
    application.processEvents()

    assert quit_called == []
    assert not event.isAccepted()
    assert main.isVisible(), "declining must leave the window exactly as it was"


def test_closing_never_hides_the_window(window, monkeypatch) -> None:
    """The regression itself: close used to hide and keep the process alive."""

    main, application = window
    monkeypatch.setattr(main, "confirm_close", lambda: False)
    main.show()
    application.processEvents()
    main.closeEvent(QCloseEvent())
    application.processEvents()
    assert main.isVisible()

    source = (ROOT / "gui" / "app.py").read_text(encoding="utf-8")
    close_body = source[source.index("def closeEvent") :]
    close_body = (
        close_body[: close_body.index("\n    def ", 10)]
        if "\n    def " in close_body[10:]
        else close_body
    )
    assert "self.hide()" not in close_body
    assert "продолжает работать" not in close_body


def test_a_shutdown_in_progress_ignores_further_close_requests(window) -> None:
    main, application = window
    main._quitting = True  # noqa: SLF001
    event = QCloseEvent()
    main.closeEvent(event)
    application.processEvents()
    assert not event.isAccepted()


def test_the_window_offers_ordinary_minimize(window) -> None:
    from PySide6.QtCore import Qt

    main, application = window
    main.show()
    application.processEvents()
    flags = main.windowFlags()
    assert flags & Qt.WindowType.WindowMinimizeButtonHint
    assert flags & Qt.WindowType.WindowCloseButtonHint

    main.showMinimized()
    application.processEvents()
    assert main.isMinimized(), "minimize must put the window away without quitting"
    assert not main.isHidden(), "minimize is not the same as hiding into the tray"


def test_the_close_shortcut_goes_through_the_prompt() -> None:
    source = (ROOT / "gui" / "app.py").read_text(encoding="utf-8")
    assert "self._close_shortcut.activated.connect(self.close)" in source
    assert "self._close_shortcut.activated.connect(self.hide)" not in source


def test_a_second_instance_activates_primary_then_exits_without_popup() -> None:
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = main_source.index("instance = SingleInstance()")
    guard_start = main_source.index("if not instance.acquire():", start)
    guard_end = main_source.index("\n        config = Config()", guard_start)
    guard = main_source[guard_start:guard_end]

    single_source = (ROOT / "core" / "single_instance.py").read_text(encoding="utf-8")
    acquire = single_source[
        single_source.index("    def acquire(self) -> bool:") :
        single_source.index("    def _notify_primary(self) -> None:")
    ]
    notify = single_source[
        single_source.index("    def _notify_primary(self) -> None:") :
        single_source.index("    def _release_notification_socket(")
    ]

    assert "self._notify_primary()" in acquire
    assert 'probe.write(b"activate")' in notify
    assert "QMessageBox" not in guard
    assert "return 0" in guard


def test_a_profile_that_blocks_the_guard_stops_startup() -> None:
    """Without the guard two copies would share one database and one session."""

    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "instance = SingleInstance()" in source
    guard = source[source.index("instance = SingleInstance()") :][:900]
    assert "except Exception" in guard
    assert "return 1" in guard

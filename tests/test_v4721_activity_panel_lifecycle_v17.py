from __future__ import annotations

import sys
import threading
import time

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool
from PySide6.QtWidgets import QApplication

from gui.activity_panel import ActivityPanel


class _BlockingAdapter:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def get_comment_campaign_state(self, *, account_id=None):
        del account_id
        self.started.set()
        assert self.release.wait(timeout=5)
        return None

    def get_join_campaign_state(self, *, account_id=None):
        del account_id
        return None

    def get_account_restriction_state(self, *, account_id=None):
        del account_id
        return {}

    def get_tasks(self, limit=100):
        del limit
        return []

    def get_logs(self, limit=150):
        del limit
        return []

    def get_scheduler_error(self):
        return ""

    def close_thread_connection(self):
        return None


class _ImmediateAdapter:
    def get_comment_campaign_state(self, *, account_id=None):
        del account_id
        return None

    def get_join_campaign_state(self, *, account_id=None):
        del account_id
        return None

    def get_account_restriction_state(self, *, account_id=None):
        del account_id
        return {"active": True, "code": "test_restricted"}

    def get_tasks(self, limit=100):
        del limit
        return []

    def get_logs(self, limit=150):
        del limit
        return []

    def get_scheduler_error(self):
        return ""

    def close_thread_connection(self):
        return None


def _drain_events(app: QApplication, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if QThreadPool.globalInstance().activeThreadCount() == 0:
            app.processEvents()
            return
        time.sleep(0.01)
    raise AssertionError("background refresh did not finish")


def test_activity_refresh_result_is_ignored_after_panel_deletion(monkeypatch):
    app = QApplication.instance() or QApplication([])
    adapter = _BlockingAdapter()
    callback_errors: list[BaseException] = []

    def capture_exception(exc_type, exc_value, traceback):
        del exc_type, traceback
        callback_errors.append(exc_value)

    monkeypatch.setattr(sys, "excepthook", capture_exception)

    panel = ActivityPanel(adapter)
    panel.timer.stop()
    assert adapter.started.wait(timeout=5)

    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    assert not shiboken6.isValid(panel)

    adapter.release.set()
    _drain_events(app)

    assert callback_errors == []


def test_activity_refresh_still_applies_result_while_panel_is_alive():
    app = QApplication.instance() or QApplication([])
    panel = ActivityPanel(_ImmediateAdapter())
    panel.timer.stop()

    _drain_events(app)

    assert panel._refresh_job is None
    assert not hasattr(panel, "spambot_button")
    assert "RESTRICTED" in panel.state_label.text()

    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()

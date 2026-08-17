from __future__ import annotations

import sys
import threading

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool
from PySide6.QtWidgets import QApplication

from gui.views.account_health_card import AccountHealthCard


class _BlockingCardAdapter:
    """Adapter whose observability read blocks until the test releases it."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def get_selected_account_id(self) -> int:
        return 1

    def get_account_observability(self, account_id: int) -> dict[str, str]:
        del account_id
        self.started.set()
        assert self.release.wait(timeout=10)
        return {"status": "ok"}

    def close_thread_connection(self) -> None:
        return None


def test_deferred_health_card_refresh_never_touches_deleted_card(monkeypatch):
    """A refresh deferred while a job was running must not reach a deleted card.

    Regression test: ``finished``/``succeeded`` used to arm a static
    ``QTimer.singleShot(0, card.refresh)``. A static singleShot keeps the bound
    method alive after Qt deleted the card, so the deferred refresh touched
    deleted widgets (libshiboken RuntimeError). Delivery of the job signals via
    ``sendPostedEvents`` arms the timer without dispatching it, which makes the
    deletion-then-fire order deterministic.
    """

    app = QApplication.instance() or QApplication([])
    callback_errors: list[BaseException] = []

    def capture_exception(exc_type, exc_value, traceback):
        del exc_type, traceback
        callback_errors.append(exc_value)

    monkeypatch.setattr(sys, "excepthook", capture_exception)

    adapter = _BlockingCardAdapter()
    card = AccountHealthCard(adapter)

    # Fire the initial refresh: the job blocks inside the adapter.
    app.processEvents()
    assert adapter.started.wait(timeout=5)

    # Ask for another refresh while the job is still running.
    card.refresh()
    assert card._refresh_pending is True

    adapter.release.set()
    assert QThreadPool.globalInstance().waitForDone(10_000)

    # Deliver succeeded/finished without dispatching timers: the deferred
    # refresh timer is armed while the card is still alive.
    QCoreApplication.sendPostedEvents()

    card.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(card)

    # The armed deferred refresh fires now, after the card was deleted.
    app.processEvents()

    assert callback_errors == []

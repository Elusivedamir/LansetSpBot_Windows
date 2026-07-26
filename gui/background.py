from __future__ import annotations

import logging
import threading
import weakref
from collections.abc import Callable
from typing import Any, ClassVar, TypeVar, cast

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from core.redaction import sanitize_exception


QObjectT = TypeVar("QObjectT", bound=QObject)
log = logging.getLogger(__name__)


class BackgroundCallSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class BackgroundCall(QRunnable):
    """Run a blocking callable in Qt's global thread pool.

    ``QThreadPool.globalInstance()`` reuses native worker threads. Database
    access in those threads therefore also reuses the thread-local SQLite
    connection unless the owner explicitly closes it. ``cleanup`` provides a
    guaranteed per-job release hook without coupling this generic helper to the
    storage layer.

    The class tracks only BackgroundCall jobs created by LansetSpBot. Shutdown must
    not depend on ``QThreadPool.activeThreadCount()`` because that global count
    can include unrelated Qt/library work and previously caused a safe factory
    reset to remain stuck in the shutdown state.
    """

    _pending_lock: ClassVar[threading.RLock] = threading.RLock()
    _pending_jobs: ClassVar[weakref.WeakSet[BackgroundCall]] = weakref.WeakSet()

    def __init__(
        self,
        callback: Callable[[], Any],
        *,
        cleanup: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__()
        self.callback = callback
        self.cleanup = cleanup
        self.signals = BackgroundCallSignals()
        self.setAutoDelete(True)
        with type(self)._pending_lock:
            type(self)._pending_jobs.add(self)

    @classmethod
    def pending_count(cls) -> int:
        """Return submitted LansetSpBot background calls that have not fully finished."""
        with cls._pending_lock:
            return len(cls._pending_jobs)

    @classmethod
    def has_pending_jobs(cls) -> bool:
        return cls.pending_count() > 0

    def _release_pending_registration(self) -> None:
        with type(self)._pending_lock:
            type(self)._pending_jobs.discard(self)

    def _emit_if_alive(self, signal_name: str, *args: Any) -> None:
        """Ignore only the normal Qt teardown case after the signal owner died."""
        try:
            getattr(self.signals, signal_name).emit(*args)
        except RuntimeError as exc:
            if "deleted" not in str(exc).lower():
                raise

    @Slot()
    def run(self) -> None:
        result: Any = None
        failure = ""
        try:
            try:
                result = self.callback()
            except Exception as exc:
                failure = sanitize_exception(exc)
                # Never attach ``exc_info`` here: the original exception args
                # may contain API hashes, proxy credentials, login codes or
                # session paths.  The sanitized chain is the only value allowed
                # to cross the QRunnable boundary or reach stderr/log handlers.
                log.error("Background call failed: %s", failure)
            finally:
                if self.cleanup is not None:
                    try:
                        self.cleanup()
                    except Exception as exc:
                        # Cleanup must not hide the callback result or produce a
                        # second contradictory success/failure signal.
                        log.error(
                            "Background call cleanup failed: %s",
                            sanitize_exception(exc),
                        )

            if failure:
                self._emit_if_alive("failed", failure)
            else:
                self._emit_if_alive("succeeded", result)
            self._emit_if_alive("finished")
        finally:
            # Keep the job registered through callback cleanup and signal
            # emission. At this point no SQLite work owned by the QRunnable is
            # left, so factory reset may safely continue.
            self._release_pending_registration()


def live_qobject(
    reference: "weakref.ReferenceType[QObjectT]",
) -> QObjectT | None:
    """Return a QObject only while both Python and C++ owners are alive."""

    owner = reference()
    if owner is None:
        return None
    try:
        import shiboken6

        if not shiboken6.isValid(owner):
            return None
    except ImportError:  # pragma: no cover - PySide6 always bundles shiboken6
        pass
    return owner


def connect_lifecycle_safe(
    job: BackgroundCall,
    owner: QObjectT,
    *,
    succeeded: Callable[[QObjectT, Any], Any] | None = None,
    failed: Callable[[QObjectT, str], Any] | None = None,
    finished: Callable[[QObjectT], Any] | None = None,
    orphaned_finished: Callable[[], Any] | None = None,
) -> None:
    """Connect BackgroundCall signals without dereferencing deleted QObjects.

    Callbacks receive the live owner explicitly. Call sites should therefore use
    unbound/local functions instead of closing over a QWidget. This single
    contract covers success, error and bookkeeping completion paths.
    """

    owner_ref = weakref.ref(owner)

    if succeeded is not None:

        def deliver_success(value: Any) -> None:
            live = live_qobject(owner_ref)
            if live is not None:
                succeeded(cast(QObjectT, live), value)

        job.signals.succeeded.connect(deliver_success)

    if failed is not None:

        def deliver_failure(message: str) -> None:
            live = live_qobject(owner_ref)
            if live is not None:
                failed(cast(QObjectT, live), message)

        job.signals.failed.connect(deliver_failure)

    if finished is not None or orphaned_finished is not None:

        def deliver_finished() -> None:
            live = live_qobject(owner_ref)
            if live is not None:
                if finished is not None:
                    finished(cast(QObjectT, live))
            elif orphaned_finished is not None:
                orphaned_finished()

        job.signals.finished.connect(deliver_finished)

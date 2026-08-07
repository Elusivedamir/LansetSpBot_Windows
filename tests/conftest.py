from __future__ import annotations

import asyncio
import base64
import os
import sys
from functools import wraps
from pathlib import Path

import pytest

# Linux is not a production target. Tests use a deterministic synthetic key
# through the explicit test-only gate so encrypted local storage can be verified
# without pretending to exercise Windows DPAPI.
os.environ.setdefault("LANSETSPBOT_ALLOW_TEST_MASTER_KEY", "1")
os.environ.setdefault("LANSETSPBOT_ALLOW_PLAINTEXT_TEST_DB", "1")
os.environ.setdefault(
    "LANSETSPBOT_TEST_MASTER_KEY_B64",
    base64.b64encode(b"LansetSpBot-test-master-key-v1!!").decode("ascii"),
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def open_project_database(path, **kwargs):
    """Open a database file exactly the way production opens it.

    A file created by ``Database`` is real SQLCipher whenever the sqlcipher3
    extension is installed, which is the case in the documented Windows
    environment (``requirements-runtime.lock`` pins ``sqlcipher3``). Reopening
    such a file with the standard library ``sqlite3`` module raises
    ``file is not a database``, so tests that inspect or mutate a database
    created by production code must go through the keyed driver.

    The helper transparently falls back to the standard library when SQLCipher
    is absent, so the same test body works in both environments.
    """

    from storage.sqlcipher_driver import connect_encrypted_database

    return connect_encrypted_database(str(path), **kwargs)


def export_plaintext_copy(source, destination):
    """Write a genuine plaintext SQLite copy of an encrypted database.

    Legacy ``format_version 1`` profile backups were produced by releases that
    predate SQLCipher, so their archived ``marlen.db`` is ordinary SQLite.
    Tests that build such an archive must therefore decrypt the fixture first
    instead of packing the current encrypted file.
    """

    from pathlib import Path

    from storage.sqlcipher_driver import SQLCIPHER_AVAILABLE

    source = Path(source)
    destination = Path(destination)
    if not SQLCIPHER_AVAILABLE:
        destination.write_bytes(source.read_bytes())
        return destination

    connection = open_project_database(source)
    try:
        connection.execute(
            "ATTACH DATABASE ? AS plaintext KEY ''", (str(destination),)
        )
        try:
            connection.execute("SELECT sqlcipher_export('plaintext')").fetchone()
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            connection.execute(f"PRAGMA plaintext.user_version = {int(user_version)}")
        finally:
            connection.execute("DETACH DATABASE plaintext")
    finally:
        connection.close()
    return destination


def project_row_factory():
    """Return the ``Row`` class that matches the active database driver.

    ``sqlite3.Row`` cannot wrap a ``sqlcipher3`` cursor, so tests that set a row
    factory on a connection from :func:`open_project_database` must use this.
    """

    from storage.sqlcipher_driver import dbapi

    return dbapi.Row


def pytest_sessionstart(session):
    """Prevent pytest-asyncio from creating an unowned legacy event loop.

    On Python 3.13, ``asyncio.get_event_loop()`` can still create an implicit
    loop before the first async test. pytest-asyncio preserves that loop as the
    previous policy state, and a later synchronous ``asyncio.run()`` can make it
    unreachable without closing it. Marking the main thread as having no current
    loop keeps the plugin on the explicit per-test lifecycle.
    """

    del session
    asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def _close_test_database_connections(monkeypatch):
    """Close every Database created by a test before sqlite3's GC finalizer.

    Production owners already close their thread-local connection during worker
    and application shutdown. Tests intentionally construct many short-lived
    repositories, so the fixture gives those temporary owners the same explicit
    lifecycle and keeps Python 3.13 ``ResourceWarning`` checks meaningful.
    """

    from storage.database import Database

    instances: list[Database] = []
    original_init = Database.__init__

    @wraps(original_init)
    def tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(Database, "__init__", tracked_init)
    yield

    # Wait only for LansetSpBot-owned BackgroundCall jobs.  Waiting for the
    # complete Qt global thread pool can include unrelated Qt/platform work and
    # can block the Windows test process indefinitely.
    try:
        import time

        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication

        from gui.background import BackgroundCall
    except ImportError:
        pass
    else:
        pending = BackgroundCall.pending_count()
        app = QApplication.instance() if pending else None
        if app is not None:
            # Stop timers before pumping deferred deletes, otherwise a teardown
            # event can enqueue another database refresh while we are draining.
            for widget in tuple(app.topLevelWidgets()):
                suspend = getattr(widget, "suspend_runtime_updates", None)
                if callable(suspend):
                    suspend()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()

        deadline = time.monotonic() + 10.0
        while pending and time.monotonic() < deadline:
            if app is not None:
                app.processEvents()
            time.sleep(0.01)
            pending = BackgroundCall.pending_count()

        if app is not None:
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()

        pending = BackgroundCall.pending_count()
        if pending:
            pytest.fail(
                f"{pending} LansetSpBot BackgroundCall job(s) did not finish during test teardown",
                pytrace=False,
            )

    for database in reversed(instances):
        try:
            database.close_thread_connection()
        except Exception:
            pass
    instances.clear()

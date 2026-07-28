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

    # GUI tests create short-lived BackgroundCall jobs (activity snapshots,
    # account catalog/settings reads and maintenance calls).  On Windows the Qt
    # global thread pool may still be inside SQLCipher/ACL work when the next
    # test starts tearing down a Database object.  That race previously ended in
    # a native ``Windows fatal exception: access violation`` instead of a normal
    # pytest failure.  Drain only already-submitted jobs before closing their
    # thread-local database connections.
    try:
        from PySide6.QtCore import QThreadPool
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        pool = QThreadPool.globalInstance()
        pool.waitForDone(10_000)
        if app is not None:
            app.processEvents()
        # Signal delivery can enqueue one final follow-up refresh.
        pool.waitForDone(2_000)
    except Exception:
        # Connection cleanup below remains the fail-safe even in tests that
        # deliberately replace or destroy Qt globals.
        pass

    for database in reversed(instances):
        try:
            database.close_thread_connection()
        except Exception:
            pass
    instances.clear()

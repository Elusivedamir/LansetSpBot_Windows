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
    for database in reversed(instances):
        try:
            database.close_thread_connection()
        except Exception:
            pass
    instances.clear()

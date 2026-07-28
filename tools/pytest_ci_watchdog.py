"""Fail-closed watchdog for GitHub Actions pytest diagnostics.

The release build loads this module explicitly with ``-p``. It does not affect
normal application runtime or ordinary developer test runs.
"""

from __future__ import annotations

import faulthandler
import sys

COLLECTION_TIMEOUT_SECONDS = 300
TEST_TIMEOUT_SECONDS = 180


def _cancel() -> None:
    try:
        faulthandler.cancel_dump_traceback_later()
    except (RuntimeError, ValueError):
        pass


def _arm(seconds: int) -> None:
    _cancel()
    faulthandler.enable(file=sys.stderr, all_threads=True)
    faulthandler.dump_traceback_later(
        max(1, int(seconds)),
        repeat=False,
        file=sys.stderr,
        exit=True,
    )


def pytest_sessionstart(session) -> None:
    del session
    _arm(COLLECTION_TIMEOUT_SECONDS)


def pytest_collection_finish(session) -> None:
    del session
    _cancel()


def pytest_runtest_logstart(nodeid, location) -> None:
    del location
    print(
        f"[pytest-ci-watchdog] armed {TEST_TIMEOUT_SECONDS}s for {nodeid}",
        file=sys.stderr,
        flush=True,
    )
    _arm(TEST_TIMEOUT_SECONDS)


def pytest_runtest_logfinish(nodeid, location) -> None:
    del nodeid, location
    _cancel()


def pytest_sessionfinish(session, exitstatus) -> None:
    del session, exitstatus
    _cancel()

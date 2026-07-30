"""Fail-closed watchdog for GitHub Actions pytest diagnostics.

The release build loads this module explicitly with ``-p``. It does not affect
normal application runtime or ordinary developer test runs.
"""

from __future__ import annotations

import faulthandler
import os
import sys
from pathlib import Path

COLLECTION_TIMEOUT_SECONDS = 300
DEFAULT_TEST_TIMEOUT_SECONDS = 180
SLOW_TEST_TIMEOUT_SECONDS = 600
CURRENT_TEST_FILE = os.environ.get("PYTEST_CURRENT_TEST_FILE", "").strip()

# Known deterministic integration/simulation tests legitimately perform large
# SQLCipher or Qt workloads on hosted Windows runners. They remain fail-closed,
# but use a larger per-test budget instead of being killed by the unit-test
# ceiling. Keep this list explicit so an accidentally hanging ordinary test
# still fails after three minutes.
SLOW_TEST_NODE_FRAGMENTS = (
    "test_24_virtual_hour_schedule_and_ledger_simulation_has_no_duplicates",
    "test_model_based_100_independent_business_state_sequences",
    "test_file_logs_use_two_mib_total_and_cleanup_old_rotations",
)


def _timeout_for_node(nodeid: str) -> int:
    return (
        SLOW_TEST_TIMEOUT_SECONDS
        if any(fragment in nodeid for fragment in SLOW_TEST_NODE_FRAGMENTS)
        else DEFAULT_TEST_TIMEOUT_SECONDS
    )


def _record_current_test(nodeid: str) -> None:
    if not CURRENT_TEST_FILE:
        return
    try:
        path = Path(CURRENT_TEST_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(str(nodeid) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


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
    _record_current_test(nodeid)
    timeout_seconds = _timeout_for_node(str(nodeid))
    print(
        f"[pytest-ci-watchdog] armed {timeout_seconds}s for {nodeid}",
        file=sys.stderr,
        flush=True,
    )
    _arm(timeout_seconds)


def pytest_runtest_logfinish(nodeid, location) -> None:
    del nodeid, location
    _cancel()


def pytest_sessionfinish(session, exitstatus) -> None:
    del session, exitstatus
    _cancel()

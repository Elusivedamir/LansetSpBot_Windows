from __future__ import annotations

import asyncio
import os
import time
from datetime import timedelta
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from core.campaign_schedule import to_db_time, utc_now
from gui.views.channels_view import ChannelsView
from gui.views.commenting_view import CommentingView
from gui.views.common import TaskWatcher
from gui.views.links_view import LinksView
from storage.database import Database
from storage.db_settings import PERSISTENT_LOG_BUDGET_BYTES
from workers.queue_worker import QueueWorker


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_pending_task_deadline_supports_event_driven_worker(tmp_path):
    db = Database(tmp_path / "deadlines.db")
    assert db.seconds_until_next_pending_task() is None

    due_id = db.insert_task("noop", {}, 0)
    assert due_id > 0
    assert db.seconds_until_next_pending_task() == pytest.approx(0.0, abs=0.05)
    assert db.claim_next_pending_task() is not None
    db.set_done(due_id)

    future_id = db.insert_task("noop", {}, 0)
    retry_at = utc_now() + timedelta(seconds=2)
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET not_before=? WHERE id=?",
            (to_db_time(retry_at), future_id),
        )
    delay = db.seconds_until_next_pending_task()
    assert delay is not None
    assert 0.5 <= delay <= 2.1


@pytest.mark.asyncio
async def test_worker_event_wait_wakes_immediately_without_polling():
    worker = QueueWorker(lambda: {}, persistent_idle=True)
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, worker.notify_task_available)
    started = time.monotonic()

    assert await worker._wait_for_task_available(5.0)
    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_persistent_idle_worker_does_not_poll_empty_sqlite_queue():
    class EmptyQueue:
        def __init__(self):
            self.claims = 0

        def claim_next_pending_task(self, *_args, **_kwargs):
            self.claims += 1
            return None

        def seconds_until_next_pending_task(self):
            return None

    queue = EmptyQueue()
    worker = QueueWorker(lambda: {}, persistent_idle=True)
    worker._db = queue  # type: ignore[assignment]
    interrupted = False
    worker.isInterruptionRequested = lambda: interrupted  # type: ignore[method-assign]

    async def stop_after_idle_wait():
        nonlocal interrupted
        await asyncio.sleep(0.08)
        interrupted = True
        worker.notify_task_available()

    stopper = asyncio.create_task(stop_after_idle_wait())
    await worker._run_async()
    await stopper

    assert queue.claims == 1


def test_posix_database_permission_hardening_is_cached(tmp_path, monkeypatch):
    import storage.database as database_module

    path = tmp_path / "private.db"
    path.write_bytes(b"db")
    os.chmod(path, 0o600)
    db = Database.__new__(Database)
    db.path = path
    db._artifact_security_lock = __import__("threading").RLock()
    db._artifact_security_identities = {}
    db._artifact_security_last_check = 0.0

    calls: list[Path] = []

    def harden(candidate):
        target = Path(candidate)
        calls.append(target)
        os.chmod(target, 0o600)
        return True

    monkeypatch.setattr(database_module, "harden_private_file", harden)
    db._harden_database_artifacts(force=True)
    db._harden_database_artifacts()
    assert calls.count(path) == 2

    os.chmod(path, 0o644)
    db._harden_database_artifacts()
    assert calls.count(path) == 3
    assert path.stat().st_mode & 0o777 == 0o600


def test_persistent_log_pruning_is_batched_not_scanned_per_insert(tmp_path):
    db = Database(tmp_path / "logs.db")
    statements: list[str] = []
    with db.get_connection() as conn:
        conn.set_trace_callback(statements.append)

    for index in range(100):
        db.insert_log("INFO", f"small-{index}")

    budget_scans = [
        statement
        for statement in statements
        if "FROM logs ORDER BY id ASC" in statement
    ]
    assert budget_scans == []
    with db.get_connection() as conn:
        small_exact = int(
            conn.execute(
                """SELECT COALESCE(SUM(
                       length(CAST(level AS BLOB))
                     + length(CAST(message AS BLOB))
                     + length(CAST(created_at AS BLOB))
                     + 48), 0)
                   FROM logs"""
            ).fetchone()[0]
        )
        small_cached = int(
            conn.execute(
                "SELECT value FROM settings WHERE key='internal.logs.retained_bytes'"
            ).fetchone()[0]
        )
    assert small_cached == small_exact

    payload = "Ж" * 4_000
    for index in range(800):
        db.insert_log("INFO", f"{index}:{payload}")

    budget_scans = [
        statement
        for statement in statements
        if "FROM logs ORDER BY id ASC" in statement
    ]
    assert 1 <= len(budget_scans) < 20

    with db.get_connection() as conn:
        exact = int(
            conn.execute(
                """SELECT COALESCE(SUM(
                       length(CAST(level AS BLOB))
                     + length(CAST(message AS BLOB))
                     + length(CAST(created_at AS BLOB))
                     + 48), 0)
                   FROM logs"""
            ).fetchone()[0]
        )
        cached = int(
            conn.execute(
                "SELECT value FROM settings WHERE key='internal.logs.retained_bytes'"
            ).fetchone()[0]
        )
    assert exact <= PERSISTENT_LOG_BUDGET_BYTES
    assert cached == exact


class _ViewAdapter:
    def close_thread_connection(self):
        return None

    def get_saved_dialogs(self):
        return []

    def get_channels(self):
        return []

    def get_join_campaign_state(self):
        return None

    def get_comment_daily_limit(self):
        return 40

    def get_comment_variants(self):
        return []

    def get_main_comments(self):
        return []

    def get_comment_template(self):
        return []

    def get_comment_campaign_state(self):
        return None

    def get_active_link_task(self):
        return None

    def count_unchecked_link_targets(self):
        return 0


def test_hidden_page_timers_stop_and_resume(qapp):
    adapter = _ViewAdapter()
    channels = ChannelsView(adapter)
    commenting = CommentingView(adapter)
    links = LinksView(adapter)

    channels.set_page_active(False)
    commenting.set_page_active(False)
    links.set_page_active(False)
    assert not channels.timer.isActive()
    assert not commenting.refresh_timer.isActive()
    assert not commenting.countdown_timer.isActive()
    assert not channels.watcher.timer.isActive()
    assert not links.watcher.timer.isActive()

    channels.set_page_active(True)
    commenting.set_page_active(True)
    links.set_page_active(True)
    assert channels.timer.isActive()
    assert commenting.refresh_timer.isActive()
    assert commenting.countdown_timer.isActive()

    channels.deleteLater()
    commenting.deleteLater()
    links.deleteLater()


def test_task_watcher_preserves_task_while_hidden(qapp):
    class Adapter:
        def get_task(self, task_id):
            return {"id": task_id, "status": "pending"}

        def close_thread_connection(self):
            return None

    watcher = TaskWatcher(Adapter())
    watcher.set_active(False)
    watcher.watch(7)
    assert watcher.task_id == 7
    assert not watcher.timer.isActive()

    watcher.set_active(True)
    assert watcher.timer.isActive()
    watcher.stop()

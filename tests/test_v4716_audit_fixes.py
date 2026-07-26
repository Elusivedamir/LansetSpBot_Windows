from __future__ import annotations

import os
from pathlib import Path

import pytest

import core.secret_store as secret_store_module
from core.secret_store import SecretStore
from storage.database import Database


def test_local_secret_store_round_trip_uses_owner_only_file(tmp_path):
    target = tmp_path / "secrets.json"
    store = SecretStore(target)

    store.set("telegram.api_hash", "very-secret")
    assert store.get("telegram.api_hash") == "very-secret"
    store.delete("telegram.api_hash")
    assert store.get("telegram.api_hash") == ""
    assert target.exists()


def test_fallback_write_does_not_use_predictable_tmp_path(monkeypatch, tmp_path):
    target = tmp_path / "secrets.json"
    predictable = target.with_suffix(".tmp")
    victim = tmp_path / "victim.txt"
    victim.write_text("do-not-touch", encoding="utf-8")

    try:
        predictable.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable in this environment")

    SecretStore(target).set("token", "secret")

    assert victim.read_text(encoding="utf-8") == "do-not-touch"
    assert predictable.is_symlink()
    assert target.is_file()
    if os.name != "nt":
        assert target.stat().st_mode & 0o077 == 0


def test_fallback_permission_failure_leaves_no_secret_file(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_store_module, "harden_private_file", lambda _path: False)
    target = tmp_path / "secrets.json"

    with pytest.raises(RuntimeError, match="restrict temporary secret store"):
        SecretStore(target).set("token", "secret")

    assert not target.exists()
    assert list(tmp_path.glob(".secrets.json.*.tmp")) == []


def test_import_rows_reports_only_rows_applied_to_database(tmp_path):
    db = Database(tmp_path / "import-count.db")
    db.insert_channel({"channel_id": 10, "title": "Import target"})
    duplicate_rows = [
        {
            "channel_id": 10,
            "message_id": 20,
            "text": "same",
            "date": "2026-07-14T10:00:00+00:00",
            "author_id": 30,
        },
        {
            "channel_id": 10,
            "message_id": 20,
            "text": "same",
            "date": "2026-07-14T10:00:00+00:00",
            "author_id": 30,
        },
    ]

    assert db.import_rows("messages", duplicate_rows, batch_size=1) == 1
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_quality_configuration_matches_python_313_and_branch_coverage():
    root = Path(__file__).resolve().parents[1]
    mypy = (root / "mypy.ini").read_text(encoding="utf-8")
    coverage = (root / ".coveragerc").read_text(encoding="utf-8")
    builder = (root / "build/build_windows_x64.ps1").read_text(encoding="utf-8-sig")

    assert "python_version = 3.13" in mypy
    assert "branch = True" in coverage
    assert "--require-hashes" in builder


def test_queue_worker_wakeup_database_and_scope_branches(tmp_path):
    from workers.queue_worker import QueueWorker

    worker = QueueWorker(lambda: {})
    assert worker._consume_task_wakeup() is False
    worker.notify_task_available()
    assert worker._consume_task_wakeup() is True
    assert worker._consume_task_wakeup() is False

    with pytest.raises(RuntimeError, match="only inside"):
        worker.get_db()
    db = Database(tmp_path / "worker-helpers.db")
    worker._db = db
    assert worker.get_db() is db

    assert worker._normalize_scope("campaign", 1) == ("campaign", 1)
    with pytest.raises(ValueError):
        worker._normalize_scope("", 1)
    with pytest.raises(ValueError):
        worker._normalize_scope("campaign", 0)

    worker._cancelled_scopes = {
        ("expired", 1): 1.0,
        ("current", 2): 100.0,
    }
    worker._cancelled_scope_retention_seconds = 50.0
    worker._prune_cancelled_scopes_locked(120.0)
    assert ("expired", 1) not in worker._cancelled_scopes
    assert ("current", 2) in worker._cancelled_scopes

    worker.clear_scope_cancellation("current", 2)
    assert worker._cancelled_scopes == {}


@pytest.mark.asyncio
async def test_queue_worker_safe_sleep_decision_branches(monkeypatch):
    from workers.queue_worker import QueueWorker

    worker = QueueWorker(lambda: {})
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: False)
    assert await worker.safe_sleep(0) is True

    monkeypatch.setattr(worker, "is_scope_cancelled", lambda *_args: False)
    assert await worker.safe_sleep(0, cancel_scope=("campaign", 1)) is True

    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: True)
    assert await worker.safe_sleep(0) is False

    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: False)
    monkeypatch.setattr(worker, "is_scope_cancelled", lambda *_args: True)
    assert await worker.safe_sleep(1, step=1, cancel_scope=("campaign", 1)) is False


@pytest.mark.asyncio
async def test_queue_worker_lifecycle_accepts_async_factory_and_cleanup(monkeypatch):
    from unittest.mock import AsyncMock

    from workers.queue_worker import QueueWorker

    cleaned = []

    async def cleanup():
        cleaned.append(True)

    async def factory():
        return ({"noop": AsyncMock()}, cleanup)

    worker = QueueWorker(factory)
    run_async = AsyncMock()
    monkeypatch.setattr(worker, "_run_async", run_async)

    await worker._run_lifecycle()

    run_async.assert_awaited_once()
    assert cleaned == [True]
    assert worker.lifecycle_state == worker.STATE_CLEANUP


@pytest.mark.asyncio
async def test_queue_worker_rejects_invalid_handler_factory_result(monkeypatch):
    from unittest.mock import AsyncMock

    from workers.queue_worker import QueueWorker

    worker = QueueWorker(lambda: [])
    monkeypatch.setattr(worker, "_run_async", AsyncMock())

    with pytest.raises(TypeError, match="handlers dict"):
        await worker._run_lifecycle()

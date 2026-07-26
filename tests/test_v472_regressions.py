from __future__ import annotations

import asyncio
from contextlib import closing
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError

from core.campaign_schedule import utc_now
from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from services.telegram_service import TelegramService
from storage.database import Database
from workers.queue_worker import QueueWorker


class _Limiter:
    async def acquire(self):
        return None


@pytest.mark.asyncio
async def test_public_join_response_loss_is_unknown_without_membership_recheck(
    monkeypatch,
):
    class Client:
        def __init__(self):
            self.join_calls = 0
            self.permission_calls = 0

        def is_connected(self):
            return True

        async def get_entity(self, chat_id):
            return chat_id

        async def __call__(self, request):
            self.join_calls += 1
            raise ConnectionError("response lost after server accepted request")

        async def get_permissions(self, chat_id, who):  # pragma: no cover
            self.permission_calls += 1
            raise AssertionError("membership recheck must not run")

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = _Limiter()
    service._connected = True
    service._status_callback = None

    async def no_op():
        return None

    monkeypatch.setattr(service, "disconnect", no_op)
    monkeypatch.setattr(service, "ensure_connected", no_op)
    monkeypatch.setattr(
        service, "safe_sleep", lambda seconds: asyncio.sleep(0, result=True)
    )

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.join(123)

    assert raised.value.code == "join_result_unknown"
    assert service.client.join_calls == 1
    assert service.client.permission_calls == 0


@pytest.mark.asyncio
async def test_invite_join_response_loss_is_never_replayed():
    class Client:
        def __init__(self):
            self.calls = 0

        def is_connected(self):
            return True

        async def __call__(self, request):
            self.calls += 1
            raise ConnectionError("ambiguous invite join")

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = _Limiter()
    service._connected = True
    service._status_callback = None

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.join_saved_dialog(invite_link="https://t.me/+abcdef")

    assert raised.value.code == "join_result_unknown"
    assert service.client.calls == 1


@pytest.mark.asyncio
async def test_long_flood_wait_defers_without_occupying_worker(monkeypatch):
    class Client:
        def is_connected(self):
            return True

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = _Limiter()
    service._connected = True
    service._status_callback = None
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise FloodWaitError(None, capture=90)

    monkeypatch.setattr("services.telegram_service.random.randint", lambda _a, _b: 20)

    with pytest.raises(DeferredTelegramError) as raised:
        await service.execute(operation)
    assert raised.value.code == "flood_wait_deferred"
    assert raised.value.retry_after == 110
    assert calls == 1


@pytest.mark.asyncio
async def test_worker_persists_deferred_task_without_marking_failure(tmp_path):
    db = Database(tmp_path / "deferred-task.db")
    task_id = db.insert_task("noop", {})
    task = db.claim_next_pending_task()
    assert task is not None

    async def defer(_task):
        raise DeferredTelegramError(
            "Telegram asked to wait", code="flood_wait_deferred", retry_after=120
        )

    worker = QueueWorker(lambda: {})
    worker._db = db
    worker._handlers = {"noop": defer}
    await worker._process_task(task)

    persisted = db.get_task(task_id)
    assert persisted is not None
    assert persisted["status"] == "pending"
    assert persisted["not_before"] is not None
    assert db.claim_next_pending_task() is None
    assert worker.failed_count == 0
    assert worker.retry_count == 1


@pytest.mark.asyncio
async def test_due_deferred_task_can_be_claimed_later(tmp_path):
    db = Database(tmp_path / "due-task.db")
    task_id = db.insert_task("noop", {})
    task = db.claim_next_pending_task()
    assert task is not None
    db.defer_task(
        task_id,
        retry_at=utc_now() - timedelta(seconds=1),
        error="ready again",
    )

    claimed = db.claim_next_pending_task()
    assert claimed is not None
    assert claimed["id"] == task_id


def test_session_backups_rotate_and_corrupt_session_is_restored(tmp_path):
    source = tmp_path / "main.session"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("CREATE TABLE sample(value INTEGER)")
        conn.execute("INSERT INTO sample(value) VALUES(7)")
        conn.commit()

    service = object.__new__(TelegramService)
    service.client = SimpleNamespace(session=SimpleNamespace(filename=str(source)))
    service.settings = SimpleNamespace(session_backup_enabled=True)

    for _ in range(7):
        assert service.backup_session() is not None

    backup_dir = tmp_path / "backups"
    backups = list(backup_dir.glob("main.session.*.bak"))
    assert len(backups) == TelegramService.SESSION_BACKUP_LIMIT

    source.write_bytes(b"not a sqlite database")
    TelegramService._prepare_session_file(source)

    assert TelegramService._session_is_healthy(source) is True
    with closing(sqlite3.connect(source)) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == 7
    assert list(tmp_path.glob("main.session.corrupt.*"))


def test_session_restore_falls_back_to_in_place_overwrite(tmp_path, monkeypatch):
    source = tmp_path / "main.session"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("CREATE TABLE sample(value INTEGER)")
        conn.execute("INSERT INTO sample(value) VALUES(11)")
        conn.commit()

    service = object.__new__(TelegramService)
    service.client = SimpleNamespace(session=SimpleNamespace(filename=str(source)))
    service.settings = SimpleNamespace(session_backup_enabled=True)
    assert service.backup_session() is not None

    source.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(
        TelegramService,
        "_replace_with_windows_retry",
        staticmethod(lambda _source, _destination: False),
    )

    TelegramService._prepare_session_file(source)

    assert TelegramService._session_is_healthy(source) is True
    with closing(sqlite3.connect(source)) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == 11


def test_batch_channel_upsert_uses_one_public_operation(tmp_path):
    db = Database(tmp_path / "batch.db")
    rows = [
        {
            "channel_id": index,
            "username": f"channel_{index}",
            "title": f"Channel {index}",
            "linked_chat_id": None,
        }
        for index in range(1, 451)
    ]
    assert db.upsert_channels_batch(rows) == 450
    assert len(db.get_channels()) == 450


def test_batch_saved_dialog_upsert_sets_memberships(tmp_path):
    db = Database(tmp_path / "saved-batch.db")
    rows = [
        {
            "peer_id": 10_000 + index,
            "username": f"saved_{index}",
            "title": f"Saved {index}",
            "kind": "channel",
        }
        for index in range(250)
    ]
    ids = db.upsert_saved_dialogs_batch(rows, account_id=5, phone="+100")
    assert len(ids) == 250
    saved = db.get_saved_dialogs(5)
    assert len(saved) == 250
    assert {row["membership_status"] for row in saved} == {"member"}


def test_schema_v12_is_migrated_with_deferred_task_column(tmp_path):
    path = tmp_path / "v12.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT)")
        connection.execute("PRAGMA user_version=12")
        connection.commit()

    db = Database(path)

    assert db.get_version() == Database.SCHEMA_VERSION
    with closing(sqlite3.connect(path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(tasks)")}
    assert {
        "not_before",
        "defer_count",
        "first_deferred_at",
        "last_deferred_at",
    } <= columns
    assert {"idx_tasks_due", "idx_tasks_retention"} <= indexes

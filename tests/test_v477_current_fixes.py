from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication
from telethon.errors import FloodWaitError

from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from services.api import ServiceAPI
from services.telegram_service import TelegramService
from storage.database import Database, DatabaseError
from workers.queue_worker import QueueWorker
from tests.conftest import open_project_database

UTC = timezone.utc


class _Limiter:
    async def acquire(self):
        return None


class _Secrets:
    def get(self, _key, default=""):
        return default

    def set(self, _key, _value):
        return None


def _core_app():
    return QApplication.instance() or QApplication([])


def test_periodic_delivery_reconciliation_moves_only_stale_rows_to_uncertain(tmp_path):
    path = tmp_path / "delivery-recovery.db"
    db = Database(path)
    db.insert_channel({"channel_id": 10, "linked_chat_id": 20, "title": "A"})
    assert db.reserve_comment_delivery(10, 30, linked_chat_id=20, text="hello")
    task_id = db.insert_task("direct_message", {"chat_id": 99, "text": "hello"})
    assert db.reserve_direct_message_delivery(task_id, 99, "hello")

    fresh = db.recover_stale_deliveries(stale_after_seconds=300)
    assert fresh["total"] == 0

    with db.get_connection() as conn:
        conn.execute(
            "UPDATE comment_deliveries SET reserved_at=datetime('now','-10 minutes')"
        )
        conn.execute(
            "UPDATE direct_message_deliveries SET reserved_at=datetime('now','-10 minutes')"
        )

    recovered = db.recover_stale_deliveries(stale_after_seconds=300)
    assert recovered == {
        "comment_deliveries": 1,
        "direct_message_deliveries": 1,
        "accounts": {
            0: {
                "comment_deliveries": 1,
                "direct_message_deliveries": 1,
                "total": 2,
            }
        },
        "total": 2,
    }
    with db.get_connection() as conn:
        comment_status = conn.execute(
            "SELECT status FROM comment_deliveries WHERE channel_id=10 AND post_id=30"
        ).fetchone()[0]
        direct_status = conn.execute(
            "SELECT status FROM direct_message_deliveries WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert comment_status == "uncertain"
    assert direct_status == "uncertain"
    assert task_count == 1
    assert db.recover_stale_deliveries(stale_after_seconds=300)["total"] == 0


def test_service_api_installs_five_minute_delivery_recovery_timer(tmp_path):
    _core_app()
    db = Database(tmp_path / "timer.db")
    api = ServiceAPI(db, secret_store=_Secrets())
    try:
        assert api._delivery_recovery_timer.interval() == 300_000
        assert api._delivery_recovery_timer.isActive()
    finally:
        api.prepare_shutdown()


def test_bootstrap_forces_wal_for_existing_current_schema_database(tmp_path):
    path = tmp_path / "existing-delete-mode.db"
    db = Database(path)
    db.set_setting("wal.fixture", "kept")
    db.close_thread_connection()

    connection = open_project_database(path)
    try:
        assert (
            connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        )
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        connection.close()

    reopened = Database(path, bootstrap=True)
    try:
        with reopened.get_connection() as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert reopened.get_setting("wal.fixture") == "kept"
    finally:
        reopened.close_thread_connection()


def test_realistic_v14_to_v15_migration_preserves_data_and_is_idempotent(tmp_path):
    path = tmp_path / "filled-v14.db"
    db = Database(path)
    task_id = db.insert_task("noop", {"source": "v14"})
    direct_task_id = db.insert_task("direct_message", {"chat_id": 7, "text": "x"})
    db.insert_channel({"channel_id": 100, "linked_chat_id": 200, "title": "Channel"})
    comment_campaign = db.create_comment_campaign(
        ["hello"], daily_limit=1, slot_count=1, continuous=False
    )
    comment_slot = db.get_comment_schedule(comment_campaign["id"], limit=1)[0]
    db.add_comment_history(
        task_id,
        100,
        300,
        "hello",
        "sent",
        campaign_id=comment_campaign["id"],
        slot_id=comment_slot["id"],
    )
    db.reserve_comment_delivery(100, 300, linked_chat_id=200, text="hello")
    dialog_id = db.upsert_saved_dialog(
        {
            "peer_id": 555,
            "username": "migration_target",
            "title": "Target",
            "kind": "channel",
        },
        account_id=1,
    )
    db.set_saved_dialog_membership(dialog_id, 1, "left")
    join_campaign = db.create_join_campaign(1, max_per_hour=40)
    db.set_setting("migration.fixture", "kept")
    db.close_thread_connection()

    connection = open_project_database(path)
    connection.execute("DROP TABLE direct_message_deliveries")
    connection.execute("DROP INDEX IF EXISTS uq_join_campaign_active")
    connection.execute("DROP INDEX IF EXISTS uq_saved_dialog_username_ci")
    connection.execute("DROP INDEX IF EXISTS idx_join_events_account_time")
    for name in (
        "validate_join_schedule_insert",
        "validate_join_schedule_update",
        "validate_join_campaign_insert",
        "validate_join_campaign_update",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    connection.execute("DELETE FROM migrations WHERE version=15")
    connection.execute("PRAGMA user_version=14")
    connection.commit()
    connection.close()

    tracked_tables = (
        "tasks",
        "channels",
        "comment_campaigns",
        "comment_schedule",
        "join_campaigns",
        "join_schedule",
        "comment_history",
        "settings",
        "comment_deliveries",
    )
    before_conn = open_project_database(path)
    before = {
        table: before_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in tracked_tables
    }
    before_conn.close()

    migrated = Database(path)
    assert migrated.get_version() == Database.SCHEMA_VERSION
    with migrated.get_connection() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tracked_tables
        }
        indexes = {
            row[1]
            for table in (
                "direct_message_deliveries",
                "join_campaigns",
                "saved_dialogs",
                "join_events",
            )
            for row in conn.execute(f"PRAGMA index_list({table})")
        }
    assert after == before
    assert {
        "idx_direct_delivery_status",
        "uq_join_campaign_active_account",
        "uq_saved_dialog_username_ci",
        "idx_join_events_account_time",
    } <= indexes
    assert migrated.reserve_direct_message_delivery(direct_task_id, 7, "x")
    with pytest.raises(DatabaseError):
        with migrated.get_connection() as conn:
            conn.execute(
                """INSERT INTO join_campaigns(account_id,status,started_at,ends_at,max_per_hour,total_count)
                   VALUES(1,'running',CURRENT_TIMESTAMP,datetime('now','+1 hour'),40,1)"""
            )

    snapshot = migrated.get_setting("migration.fixture")
    migrated.close_thread_connection()

    class Reopen(Database):
        def _migrate_to_v15(self):  # pragma: no cover - must not run
            raise AssertionError("v15 migration repeated")

    reopened = Reopen(path)
    assert reopened.get_version() == Database.SCHEMA_VERSION
    assert reopened.get_setting("migration.fixture") == snapshot == "kept"
    assert reopened.get_join_campaign(join_campaign["id"]) is not None


@pytest.mark.asyncio
async def test_queue_worker_cancelled_idempotent_task_is_requeued():
    class DB:
        def __init__(self):
            self.requeued = []

        def requeue_task(self, task_id, error):
            self.requeued.append((task_id, error))

    async def handler(_task):
        raise asyncio.CancelledError

    worker = QueueWorker(lambda: {})
    worker._db = DB()
    worker._handlers = {"noop": handler}
    with pytest.raises(asyncio.CancelledError):
        await worker._process_task({"id": 1, "type": "noop", "payload": {}})
    assert worker._db.requeued == [(1, "Worker cancelled before completion")]


@pytest.mark.asyncio
async def test_queue_worker_cancelled_mutating_task_is_manual_review_failure():
    class DB:
        def __init__(self):
            self.failed = []

        def set_failed(self, task_id, message, retry=False):
            self.failed.append((task_id, message, retry))

    async def handler(_task):
        raise asyncio.CancelledError

    worker = QueueWorker(lambda: {})
    worker._db = DB()
    worker._handlers = {"direct_message": handler}
    with pytest.raises(asyncio.CancelledError):
        await worker._process_task({"id": 2, "type": "direct_message", "payload": {}})
    assert worker._db.failed[0][0] == 2
    assert worker._db.failed[0][2] is False
    assert "uncertain external result" in worker._db.failed[0][1]


@pytest.mark.asyncio
async def test_queue_worker_stops_after_bounded_claim_failures(monkeypatch):
    class DB:
        calls = 0

        def claim_next_pending_task(self, *_args, **_kwargs):
            self.calls += 1
            raise DatabaseError("locked")

    worker = QueueWorker(lambda: {})
    worker._db = DB()
    monkeypatch.setattr(
        worker, "safe_sleep", lambda *_a, **_k: asyncio.sleep(0, result=True)
    )
    with pytest.raises(DatabaseError, match="locked"):
        await worker._run_async()
    assert worker._db.calls == 1
    assert worker.lifecycle_state == worker.STATE_ACTIVE


@pytest.mark.asyncio
async def test_defer_limit_message_contains_actionable_diagnostics():
    class DB:
        def defer_task(self, *_a, **_k):
            return "exhausted"

        def get_task_defer_diagnostics(self, _task_id):
            return {"defer_count": 10, "elapsed_since_first_defer_seconds": 3600}

    async def handler(_task):
        raise DeferredTelegramError("wait", code="flood_wait_deferred", retry_after=642)

    worker = QueueWorker(lambda: {})
    worker._db = DB()
    worker._handlers = {"noop": handler}
    messages = []
    worker.task_failed.connect(lambda _task_id, message: messages.append(message))
    await worker._process_task({"id": 9, "type": "noop", "payload": {}})
    _core_app().processEvents()
    assert messages
    message = messages[-1]
    assert "Flood" in message or "flood_wait_deferred" in message
    assert "642" in message
    assert "10" in message
    assert "task_id=9" in message
    assert "Автоматический повтор отключён" in message


@pytest.mark.asyncio
async def test_telegram_reconnect_before_read_only_request_runs_operation_once(
    monkeypatch,
):
    class Client:
        def __init__(self):
            self.connected = False

        def is_connected(self):
            return self.connected

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = _Limiter()
    service._connected = False
    connected = 0
    calls = 0

    async def connect():
        nonlocal connected
        connected += 1
        service.client.connected = True

    async def operation():
        nonlocal calls
        calls += 1
        return "ok"

    monkeypatch.setattr(service, "connect", connect)
    assert await service.execute(operation) == "ok"
    assert connected == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_telegram_long_floodwait_defers_same_task(monkeypatch):
    class Client:
        def is_connected(self):
            return True

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = _Limiter()
    service._connected = True
    service._status_callback = None
    monkeypatch.setattr("services.telegram_service.random.randint", lambda _a, _b: 20)
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise FloodWaitError(None, capture=30)

    with pytest.raises(DeferredTelegramError) as raised:
        await service.execute(operation)
    assert raised.value.code == "flood_wait_deferred"
    assert raised.value.retry_after == 180
    assert calls == 1


@pytest.mark.asyncio
async def test_unauthorized_session_does_not_start_interactive_auth():
    class Client:
        start_calls = 0
        disconnect_calls = 0

        def is_connected(self):
            return False

        async def connect(self):
            return None

        async def is_user_authorized(self):
            return False

        async def disconnect(self):
            self.disconnect_calls += 1

        async def start(self):
            self.start_calls += 1
            raise AssertionError("interactive start must not be called")

    service = object.__new__(TelegramService)
    service.settings = SimpleNamespace(configured=True)
    service.client = Client()
    service._connected = False
    service.backup_session = lambda: None
    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.connect()
    assert raised.value.code == "authorization_required"
    assert service.client.start_calls == 0
    assert service.client.disconnect_calls == 1


@pytest.mark.asyncio
async def test_disconnect_is_idempotent():
    class Client:
        def __init__(self):
            self.connected = True
            self.calls = 0

        def is_connected(self):
            return self.connected

        async def disconnect(self):
            self.calls += 1
            self.connected = False

    service = object.__new__(TelegramService)
    service.client = Client()
    service._connected = True
    await service.disconnect()
    await service.disconnect()
    assert service.client.calls == 1
    assert service._connected is False


def test_database_maintenance_helpers_are_safe_and_observable(
    tmp_path, monkeypatch, caplog
):
    db = Database(tmp_path / "maintenance-helpers.db")
    assert db._recover_stale_deliveries()["total"] == 0
    monkeypatch.setattr("storage.database.wal_size_bytes", lambda _path: 1234)
    assert db.log_wal_size_if_large(warning_bytes=1000) == 1234
    assert "SQLite WAL is large" in caplog.text
    db.set_version(Database.SCHEMA_VERSION)
    assert db.get_version() == Database.SCHEMA_VERSION
    assert db.restore_processing_tasks() == 0


def test_insert_log_surfaces_database_error(tmp_path):
    db = Database(tmp_path / "insert-log-error.db")
    with db.get_connection() as conn:
        conn.execute("DROP TABLE logs")

    with pytest.raises(DatabaseError, match="Database error"):
        db.insert_log("INFO", "must not be swallowed")


def test_redistribution_returns_actual_updated_row_count(tmp_path):
    db = Database(tmp_path / "redistribution-rowcount.db")
    start = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=7,
        slot_count=7,
        duration_hours=24,
        continuous=False,
        start_at=start,
        rng=random.Random(71),
    )

    moved = db.redistribute_pending_comment_slots(
        campaign["id"],
        now=start,
        grace_seconds=0,
        force=True,
        rng=random.Random(72),
    )
    summary = db.get_comment_schedule_summary(campaign["id"])

    assert moved == 7
    assert summary["counts"].get("pending") == 7

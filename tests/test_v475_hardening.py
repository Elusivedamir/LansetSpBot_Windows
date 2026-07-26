from __future__ import annotations

import asyncio
from datetime import timezone
from unittest.mock import MagicMock

import pytest

from core.exceptions import DeferredTelegramError
from services.comment_service import CommentService
from storage.database import Database

UTC = timezone.utc


def _delivery_status(db: Database, channel_id: int, post_id: int) -> str | None:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM comment_deliveries WHERE channel_id=? AND post_id=?",
            (channel_id, post_id),
        ).fetchone()
    return str(row[0]) if row is not None else None


@pytest.mark.asyncio
async def test_unexpected_send_exception_keeps_duplicate_guard(tmp_path):
    db = Database(tmp_path / "unexpected-send.db")

    class Telegram:
        async def send_comment(self, *args, **kwargs):
            raise RuntimeError("transport wrapper crashed after dispatch")

    service = CommentService(Telegram(), linked_chat_service=None, db=db)
    with pytest.raises(RuntimeError):
        await service.ensure_and_send_comment(
            channel_id=10,
            linked_chat_id=20,
            post_message_id=30,
            text="hello",
            membership_ready=True,
        )

    assert _delivery_status(db, 10, 30) == "uncertain"
    assert db.has_commented(10, 30) is True


@pytest.mark.asyncio
async def test_cancelled_send_keeps_duplicate_guard(tmp_path):
    db = Database(tmp_path / "cancelled-send.db")

    class Telegram:
        async def send_comment(self, *args, **kwargs):
            raise asyncio.CancelledError

    service = CommentService(Telegram(), linked_chat_service=None, db=db)
    with pytest.raises(asyncio.CancelledError):
        await service.ensure_and_send_comment(
            channel_id=11,
            linked_chat_id=21,
            post_message_id=31,
            text="hello",
            membership_ready=True,
        )

    assert _delivery_status(db, 11, 31) == "uncertain"


@pytest.mark.asyncio
async def test_deferred_pre_execution_send_releases_reservation(tmp_path):
    db = Database(tmp_path / "deferred-send.db")

    class Telegram:
        async def send_comment(self, *args, **kwargs):
            raise DeferredTelegramError(
                "Telegram asked to wait", code="flood_wait_deferred", retry_after=90
            )

    service = CommentService(Telegram(), linked_chat_service=None, db=db)
    with pytest.raises(DeferredTelegramError):
        await service.ensure_and_send_comment(
            channel_id=12,
            linked_chat_id=22,
            post_message_id=32,
            text="hello",
            membership_ready=True,
        )

    assert _delivery_status(db, 12, 32) is None


def test_campaign_pause_uses_scope_cancellation_not_global_worker_stop(tmp_path):
    from PySide6.QtWidgets import QApplication
    from services.api import ServiceAPI

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "scoped-pause.db")
    db.insert_channel({"channel_id": 1, "linked_chat_id": 2, "title": "A"})
    campaign = db.create_comment_campaign(
        ["hello"], daily_limit=1, slot_count=1, continuous=False
    )
    worker = MagicMock()
    worker.isRunning.return_value = True
    api = ServiceAPI(db, queue_worker=worker)
    api._campaign_timer.stop()

    assert api.pause_comment_campaign() is True
    worker.request_scope_cancellation.assert_called_once_with(
        "comment_campaign", campaign["id"]
    )
    worker.requestInterruption.assert_not_called()
    app.processEvents()
    api.prepare_shutdown()


def test_cancel_comment_slot_does_not_consume_attempt(tmp_path):
    db = Database(tmp_path / "cancel-slot.db")
    db.insert_channel({"channel_id": 10, "linked_chat_id": 20, "title": "A"})
    campaign = db.create_comment_campaign(
        ["hello"], daily_limit=1, slot_count=1, continuous=False
    )
    slot = db.get_comment_schedule(campaign["id"], limit=1)[0]
    task_id = db.insert_task(
        "auto_comment_slot",
        {"campaign_id": campaign["id"], "slot_id": slot["id"]},
        max_retries=0,
    )
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE comment_schedule SET status='queued', task_id=? WHERE id=?",
            (task_id, slot["id"]),
        )

    assert db.cancel_comment_slot(slot["id"], result="stopped") is True
    state = db.get_comment_campaign(campaign["id"])
    schedule = db.get_comment_schedule(campaign["id"], limit=1)[0]
    assert state["attempted_count"] == 0
    assert schedule["status"] == "cancelled"


def test_identical_setting_write_does_not_touch_updated_at(tmp_path):
    db = Database(tmp_path / "settings-noop.db")
    db.set_setting("scheduler.comment_error", "")
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE settings SET updated_at='2000-01-01 00:00:00' "
            "WHERE key='scheduler.comment_error'"
        )
    db.set_setting("scheduler.comment_error", "")
    with db.get_connection() as conn:
        updated_at = conn.execute(
            "SELECT updated_at FROM settings WHERE key='scheduler.comment_error'"
        ).fetchone()[0]
    assert updated_at == "2000-01-01 00:00:00"


def test_v14_database_reopen_skips_schema_migrations(tmp_path):
    path = tmp_path / "migration-once.db"
    first = Database(path)
    assert first.get_version() == Database.SCHEMA_VERSION
    first.close_thread_connection()

    class ReopenDatabase(Database):
        def _upgrade_legacy_to_v13(self):  # pragma: no cover - must not run
            raise AssertionError("legacy migration repeated")

        def _migrate_to_v14(self):  # pragma: no cover - must not run
            raise AssertionError("v14 migration repeated")

    reopened = ReopenDatabase(path)
    assert reopened.get_version() == Database.SCHEMA_VERSION


def test_worker_mode_requires_prebootstrapped_schema(tmp_path):
    path = tmp_path / "worker-schema.db"
    with pytest.raises(Exception, match="requires bootstrap"):
        Database(path, bootstrap=False)
    Database(path)
    worker_db = Database(path, bootstrap=False)
    assert worker_db.get_version() == Database.SCHEMA_VERSION


def test_v14_removes_duplicate_indexes_and_adds_query_indexes(tmp_path):
    db = Database(tmp_path / "indexes.db")
    with db.get_connection() as conn:
        indexes = {
            table: {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}
            for table in (
                "channels",
                "messages",
                "comments",
                "comment_templates",
                "settings",
                "comment_history",
                "comment_schedule",
                "join_schedule",
            )
        }
    assert "uq_channels_channel_id" not in indexes["channels"]
    assert "idx_channels_id" not in indexes["channels"]
    assert "uq_messages_channel_message" not in indexes["messages"]
    assert "idx_messages_channel" not in indexes["messages"]
    assert "idx_comments_post" in indexes["comments"]
    assert "idx_comment_history_task" in indexes["comment_history"]
    assert "idx_comment_schedule_campaign_status_due" in indexes["comment_schedule"]
    assert "idx_join_schedule_campaign_status_due" in indexes["join_schedule"]


def test_query_plans_use_v14_indexes(tmp_path):
    db = Database(tmp_path / "query-plan.db")
    with db.get_connection() as conn:
        history_plan = " ".join(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM comment_history "
                "WHERE task_id=? ORDER BY id ASC LIMIT 20",
                (1,),
            )
        )
        comment_plan = " ".join(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM comments "
                "WHERE channel_id=? AND post_message_id=? "
                "AND comment_message_id IS NOT NULL LIMIT 1",
                (1, 2),
            )
        )
    assert "idx_comment_history_task" in history_plan
    assert "idx_comments_post" in comment_plan


def test_reference_guards_reject_orphans_and_cascade_channel_children(tmp_path):
    from storage.database import DatabaseError

    db = Database(tmp_path / "references.db")
    with pytest.raises(DatabaseError, match="missing channel"):
        db.insert_message(
            {
                "channel_id": 999,
                "message_id": 1,
                "text": "orphan",
                "date": None,
                "author_id": None,
            }
        )

    db.insert_channel({"channel_id": 10, "linked_chat_id": 11, "title": "A"})
    assert db.insert_message(
        {
            "channel_id": 10,
            "message_id": 1,
            "text": "ok",
            "date": None,
            "author_id": None,
        }
    )
    with db.get_connection() as conn:
        conn.execute("DELETE FROM channels WHERE channel_id=10")
        remaining = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE channel_id=10"
        ).fetchone()[0]
    assert remaining == 0


def test_retention_prunes_only_expired_terminal_data(tmp_path):
    db = Database(tmp_path / "retention.db")
    old_task = db.insert_task("noop", {})
    fresh_task = db.insert_task("noop", {})
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET status='completed', updated_at=datetime('now','-120 days') WHERE id=?",
            (old_task,),
        )
        conn.execute(
            "UPDATE tasks SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (fresh_task,),
        )
        conn.execute(
            "INSERT INTO logs(level,message,created_at) VALUES('INFO','old',datetime('now','-40 days'))"
        )
        conn.execute(
            "INSERT INTO logs(level,message,created_at) VALUES('INFO','fresh',CURRENT_TIMESTAMP)"
        )
    deleted = db.prune_old_data()
    assert deleted["tasks"] == 1
    assert deleted["logs"] == 1
    assert db.get_task(old_task) is None
    assert db.get_task(fresh_task) is not None
    assert db.run_daily_maintenance() is not None
    assert db.run_daily_maintenance() is None


def test_deferred_task_has_bounded_lifetime(tmp_path):
    from core.campaign_schedule import utc_now

    db = Database(tmp_path / "bounded-defer.db")
    task_id = db.insert_task("noop", {})
    assert db.claim_next_pending_task()["id"] == task_id
    assert (
        db.defer_task(
            task_id,
            retry_at=utc_now(),
            error="wait once",
            max_defer_count=1,
        )
        == "deferred"
    )
    assert db.claim_next_pending_task()["id"] == task_id
    assert (
        db.defer_task(
            task_id,
            retry_at=utc_now(),
            error="wait twice",
            max_defer_count=1,
        )
        == "exhausted"
    )
    task = db.get_task(task_id)
    assert task["status"] == "failed"
    assert task["defer_count"] == 1
    assert "defer_limit_exceeded" in task["error"]


def test_database_reuses_connection_only_within_owner_thread(tmp_path):
    import queue
    import threading

    db = Database(tmp_path / "thread-connections.db")
    with db.get_connection() as first:
        first_id = id(first)
    with db.get_connection() as second:
        assert id(second) == first_id

    result = queue.Queue()

    def other_thread():
        with db.get_connection() as conn:
            result.put(id(conn))
        db.close_thread_connection()

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert result.get_nowait() != first_id


def test_large_batch_import_and_prune_avoid_bind_variable_limit(tmp_path):
    db = Database(tmp_path / "large-batch.db")
    rows = [
        {
            "channel_id": value,
            "username": f"channel_{value}",
            "title": f"Channel {value}",
            "linked_chat_id": value + 10_000,
        }
        for value in range(1, 1_501)
    ]
    assert db.import_rows("channels", rows) == 1_500
    db.prune_channels_except(range(1, 1_201))
    assert len(db.get_channels()) == 1_200
    assert db.get_channel_by_id(1_500) is None


def test_comment_handler_is_extracted_from_composition():
    import inspect

    from core.composition import ApplicationContainer
    from workers.handlers.comment_slot import create_comment_slot_handler

    composition_source = inspect.getsource(ApplicationContainer._create_worker_handlers)
    handler_source = inspect.getsource(create_comment_slot_handler)
    assert "async def auto_comment_slot" not in composition_source
    assert "async def auto_comment_slot" in handler_source
    assert len(composition_source.splitlines()) < 450


def test_service_api_uses_configured_campaign_duration(tmp_path):
    from core.campaign_schedule import from_db_time
    from PySide6.QtWidgets import QApplication
    from services.api import ServiceAPI

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "campaign-hours.db")
    db.set_setting("telegram.account_id", 101)
    db.insert_channel({"channel_id": 1, "linked_chat_id": 2, "title": "A"})
    api = ServiceAPI(db, campaign_hours=6)
    api._campaign_timer.stop()
    campaign = api.start_comment_campaign(["hello"], continuous=False, daily_limit=1)
    started = from_db_time(campaign["started_at"])
    ends = from_db_time(campaign["ends_at"])
    assert started is not None and ends is not None
    assert 5.9 <= (ends - started).total_seconds() / 3600 <= 6.1
    app.processEvents()
    api.prepare_shutdown()


@pytest.mark.asyncio
async def test_worker_marks_task_failed_when_defer_budget_is_exhausted(tmp_path):
    from workers.queue_worker import QueueWorker

    db = Database(tmp_path / "worker-defer-exhausted.db")
    task_id = db.insert_task("noop", {})
    task = db.claim_next_pending_task()
    assert task is not None
    with db.get_connection() as conn:
        conn.execute("UPDATE tasks SET defer_count=10 WHERE id=?", (task_id,))

    async def always_defer(_task):
        raise DeferredTelegramError(
            "wait again", code="flood_wait_deferred", retry_after=60
        )

    worker = QueueWorker(lambda: {})
    worker._db = db
    worker._handlers = {"noop": always_defer}
    await worker._process_task(task)

    persisted = db.get_task(task_id)
    assert persisted["status"] == "failed"
    assert worker.failed_count == 1
    assert worker.retry_count == 0

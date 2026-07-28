from __future__ import annotations

import asyncio
import json
import random
from datetime import timedelta
from types import SimpleNamespace

import pytest

from core.campaign_schedule import to_db_time, utc_now
from core.exceptions import NonRetryableTelegramError
from services.multiaccount_scheduler import AccountCampaignDatabaseView
from storage.database import Database
from workers.handler_registry import create_worker_handlers


def _register(db: Database, account_id: int) -> None:
    db.register_telegram_account(
        telegram_account_id=account_id,
        session_name=f"account_{account_id}",
        display_name=str(account_id),
    )


def test_campaign_queue_is_account_scoped_and_persists_owner(tmp_path) -> None:
    db = Database(tmp_path / "scheduler-scope.db")
    first_account = 5101
    second_account = 5102
    _register(db, first_account)
    _register(db, second_account)

    now = utc_now()
    first = db.create_comment_campaign(
        ["first"],
        daily_limit=1,
        slot_count=1,
        duration_hours=1,
        continuous=False,
        start_at=now - timedelta(minutes=5),
        rng=random.Random(1),
        account_id=first_account,
    )
    second = db.create_comment_campaign(
        ["second"],
        daily_limit=1,
        slot_count=1,
        duration_hours=1,
        continuous=False,
        start_at=now - timedelta(minutes=5),
        rng=random.Random(2),
        account_id=second_account,
    )
    with db.get_connection() as conn:
        conn.execute(
            """UPDATE comment_schedule
               SET scheduled_at=?
               WHERE campaign_id IN (?, ?)""",
            (
                to_db_time(now - timedelta(seconds=1)),
                int(first["id"]),
                int(second["id"]),
            ),
        )

    second_view = AccountCampaignDatabaseView(db, second_account)
    first_view = AccountCampaignDatabaseView(db, first_account)

    queued_second = second_view.queue_due_comment_slot(now=now)
    assert queued_second is not None
    assert second_view.has_pending_task_type("auto_comment_slot") is True
    assert first_view.has_pending_task_type("auto_comment_slot") is False

    queued_first = first_view.queue_due_comment_slot(now=now)
    assert queued_first is not None
    assert first_view.has_pending_task_type("auto_comment_slot") is True

    with db.get_connection() as conn:
        rows = conn.execute(
            """SELECT account_id, payload
               FROM tasks
               WHERE type='auto_comment_slot'
               ORDER BY id"""
        ).fetchall()

    assert [int(row["account_id"]) for row in rows] == [
        second_account,
        first_account,
    ]
    assert [int(json.loads(row["payload"])["account_id"]) for row in rows] == [
        second_account,
        first_account,
    ]


def test_unconfigured_handler_registry_exposes_health_handler() -> None:
    class FakeQueueWorker:
        @staticmethod
        def get_db():
            return object()

    class FakeSelf:
        queue_worker = FakeQueueWorker()
        api = None

        @staticmethod
        def _telegram_settings(_db):
            return SimpleNamespace(configured=False)

        @staticmethod
        def _strict_secret_value(_key):
            return None

    class FakeImportService:
        def __init__(self, _db):
            pass

    handlers, cleanup = create_worker_handlers(
        FakeSelf(),
        TelegramService=object,
        ImportService=FakeImportService,
        LinkedChatService=object,
        CommentService=object,
    )

    assert cleanup is None
    assert "telegram_health" in handlers
    with pytest.raises(NonRetryableTelegramError) as error:
        asyncio.run(handlers["telegram_health"]({"id": 0, "payload": {}}))
    assert error.value.code == "telegram_not_configured"

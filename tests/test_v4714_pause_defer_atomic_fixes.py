from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import QApplication

from core.campaign_schedule import from_db_time, to_db_time, utc_now
from core.exceptions import DeferredTelegramError
from services.api import ServiceAPI
from storage.database import Database, DatabaseError
from workers.queue_worker import QueueWorker

UTC = timezone.utc


class _Secrets:
    def get(self, _key, default=""):
        return default

    def set(self, _key, _value):
        return None


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _insert_linked_channels(db: Database, count: int) -> None:
    for index in range(count):
        db.insert_channel(
            {
                "channel_id": 10_000 + index,
                "linked_chat_id": 20_000 + index,
                "title": f"Channel {index}",
            }
        )


def test_saved_limit_is_snapshot_for_next_campaign_and_locked_while_active(tmp_path):
    _app()
    db = Database(tmp_path / "limit-snapshot.db")
    db.set_setting("telegram.account_id", 101)
    _insert_linked_channels(db, 3)
    api = ServiceAPI(db, secret_store=_Secrets())
    api._campaign_timer.stop()
    api._delivery_recovery_timer.stop()

    assert api.set_comment_daily_limit(73) == 73
    campaign = api.start_comment_campaign(["Комментарий"], continuous=False)
    stored = db.get_comment_campaign(campaign["id"])

    assert campaign["requested_daily_limit"] == 73
    assert stored["daily_limit"] == 73
    assert campaign["planned_count"] == 3

    with pytest.raises(ValueError, match="активной кампании"):
        api.set_comment_daily_limit(99)
    assert api.get_comment_daily_limit() == 73

    assert api.stop_comment_campaign() is True
    assert api.set_comment_daily_limit(99) == 99
    assert api.get_comment_daily_limit() == 99
    api.prepare_shutdown()


@pytest.mark.parametrize("daily_limit", [1, 40, 1000])
def test_any_length_pause_preserves_all_pending_slots(tmp_path, daily_limit):
    db = Database(tmp_path / f"pause-{daily_limit}.db")
    start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=daily_limit,
        slot_count=daily_limit,
        duration_hours=24,
        continuous=False,
        start_at=start,
        rng=random.Random(daily_limit),
    )
    assert db.pause_comment_campaign(campaign["id"])

    resumed_at = start + timedelta(days=10)
    assert db.resume_comment_campaign(
        campaign["id"], now=resumed_at, rng=random.Random(daily_limit + 1)
    )

    rows = db.get_comment_schedule(campaign["id"], limit=daily_limit + 10)
    assert len(rows) == daily_limit
    assert {row["status"] for row in rows} == {"pending"}
    scheduled = [from_db_time(row["scheduled_at"]) for row in rows]
    target_interval = (24 * 60 * 60) / daily_limit
    assert resumed_at + timedelta(seconds=target_interval * 0.10) <= min(scheduled)
    assert min(scheduled) <= resumed_at + timedelta(seconds=target_interval * 0.90)

    refreshed = db.get_comment_campaign(campaign["id"])
    assert refreshed["status"] == "running"
    assert from_db_time(refreshed["ends_at"]) > max(scheduled)


def test_api_resume_after_original_end_does_not_complete_or_miss_slots(tmp_path):
    _app()
    db = Database(tmp_path / "resume-after-end.db")
    # Campaigns are account-scoped since schema v18; account 0 is the
    # unauthenticated sentinel and is never returned as an active campaign.
    db.set_setting("telegram.account_id", 501)
    start = utc_now() - timedelta(days=3)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=1,
        slot_count=1,
        duration_hours=24,
        continuous=False,
        start_at=start,
        rng=random.Random(1),
    )
    assert db.pause_comment_campaign(campaign["id"])

    api = ServiceAPI(db, secret_store=_Secrets())
    api._campaign_timer.stop()
    api._delivery_recovery_timer.stop()
    assert api.resume_comment_campaign() is True

    slot = db.get_comment_schedule(campaign["id"], limit=2)[0]
    refreshed = db.get_comment_campaign(campaign["id"])
    assert slot["status"] == "pending"
    assert refreshed["status"] == "running"
    assert from_db_time(refreshed["ends_at"]) > utc_now()
    api.prepare_shutdown()


@pytest.mark.asyncio
async def test_deferred_task_releases_worker_for_next_due_task(tmp_path):
    db = Database(tmp_path / "nonblocking-worker.db")
    delayed_id = db.insert_task("defer_me", {})
    next_id = db.insert_task("noop", {})
    delayed_task = db.claim_next_pending_task()
    assert delayed_task and delayed_task["id"] == delayed_id

    async def defer_handler(_task):
        raise DeferredTelegramError(
            "Telegram asked to wait",
            code="flood_wait_deferred",
            retry_after=120,
        )

    worker = QueueWorker(lambda: {}, database_path=db.path)
    worker._db = db
    worker._handlers = {"defer_me": defer_handler}
    await worker._process_task_impl(delayed_task)

    delayed_row = db.get_task(delayed_id)
    assert delayed_row["status"] == "pending"
    assert delayed_row["defer_count"] == 1
    assert from_db_time(delayed_row["not_before"]) > utc_now()

    next_task = db.claim_next_pending_task()
    assert next_task and next_task["id"] == next_id


def test_comment_finalization_is_one_transaction_with_rollback(tmp_path):
    db = Database(tmp_path / "atomic-comment.db")
    channel_id = 101
    db.insert_channel(
        {"channel_id": channel_id, "linked_chat_id": 202, "title": "Atomic"}
    )
    now = utc_now()
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=1,
        slot_count=1,
        duration_hours=24,
        continuous=False,
        start_at=now - timedelta(hours=1),
        rng=random.Random(2),
    )
    slot_id = db.get_comment_schedule(campaign["id"], limit=2)[0]["id"]
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE comment_schedule SET scheduled_at=? WHERE id=?",
            (to_db_time(now - timedelta(minutes=1)), slot_id),
        )

    queued = db.queue_due_comment_slot(now=now)
    assert queued is not None
    task = db.claim_next_pending_task()
    assert task and task["id"] == queued["task_id"]
    assert db.mark_comment_slot_running(slot_id, task["id"])

    with pytest.raises(DatabaseError):
        db.finalize_comment_slot_outcome(
            task["id"] + 1000,
            slot_id,
            status="sent",
            result="Отправлено",
            channel_id=channel_id,
            post_id=303,
            selected_text="Комментарий",
            sent=True,
            consume_channel=True,
        )

    rolled_back_slot = db.get_comment_schedule(campaign["id"], limit=2)[0]
    rolled_back_campaign = db.get_comment_campaign(campaign["id"])
    rolled_back_channel = next(
        row for row in db.get_channels() if row["channel_id"] == channel_id
    )
    assert rolled_back_slot["status"] == "running"
    assert rolled_back_campaign["attempted_count"] == 0
    assert rolled_back_campaign["sent_count"] == 0
    assert db.get_comment_history(campaign_id=campaign["id"]) == []
    assert rolled_back_channel["last_comment_check_at"] is None
    assert db.get_task(task["id"])["status"] == "running"

    assert db.finalize_comment_slot_outcome(
        task["id"],
        slot_id,
        status="sent",
        result="Отправлено",
        channel_id=channel_id,
        post_id=303,
        selected_text="Комментарий",
        sent=True,
        consume_channel=True,
    )

    final_slot = db.get_comment_schedule(campaign["id"], limit=2)[0]
    final_campaign = db.get_comment_campaign(campaign["id"])
    final_channel = next(
        row for row in db.get_channels() if row["channel_id"] == channel_id
    )
    history = db.get_comment_history(campaign_id=campaign["id"])
    assert final_slot["status"] == "sent"
    assert final_campaign["attempted_count"] == 1
    assert final_campaign["sent_count"] == 1
    assert len(history) == 1
    assert history[0]["task_id"] == task["id"]
    assert final_channel["last_comment_check_at"] is not None
    assert db.get_task(task["id"])["status"] == "completed"

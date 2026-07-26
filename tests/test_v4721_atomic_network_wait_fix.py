from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from core.campaign_schedule import from_db_time
from storage.database import Database, DatabaseError

UTC = timezone.utc


def _add_comment_channels(db: Database, count: int) -> None:
    for index in range(1, count + 1):
        db.insert_channel(
            {
                "channel_id": 10_000 + index,
                "title": f"Channel {index}",
                "username": f"channel_{index}",
                "linked_chat_id": 20_000 + index,
            }
        )


def _queue_running_comment_slot(db: Database, campaign_id: int) -> tuple[dict, int]:
    slot = db.get_comment_schedule(campaign_id, limit=10)[0]
    due = from_db_time(slot["scheduled_at"]) + timedelta(seconds=1)
    queued = db.queue_due_comment_slot(now=due)
    assert queued is not None
    task_id = int(queued["task_id"])
    assert db.set_processing(task_id)
    assert db.mark_comment_slot_running(slot["id"], task_id)
    return slot, task_id


def _queue_running_join_slot(db: Database, campaign_id: int) -> tuple[dict, int]:
    slot = db.get_join_schedule(campaign_id, limit=10)[0]
    due = from_db_time(slot["scheduled_at"]) + timedelta(seconds=1)
    queued = db.queue_due_join_slot(now=due)
    assert queued is not None
    task_id = int(queued["task_id"])
    assert db.set_processing(task_id)
    assert db.mark_join_slot_running(slot["id"], task_id)
    return slot, task_id


def test_comment_network_wait_entry_rolls_back_slot_when_campaign_update_fails(
    tmp_path,
) -> None:
    db = Database(tmp_path / "comment-network-entry.db")
    _add_comment_channels(db, 2)
    start = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["Comment"],
        daily_limit=2,
        slot_count=2,
        start_at=start,
        rng=random.Random(1),
    )
    campaign_id = int(campaign["id"])
    slot, task_id = _queue_running_comment_slot(db, campaign_id)
    retry_at = start + timedelta(hours=12)

    with db.get_connection() as conn:
        conn.execute(
            """CREATE TRIGGER fail_comment_network_wait
               BEFORE UPDATE OF status ON comment_campaigns
               WHEN OLD.status='running' AND NEW.status='network_wait'
               BEGIN
                   SELECT RAISE(ABORT, 'injected comment network wait failure');
               END"""
        )

    with pytest.raises(DatabaseError, match="injected comment network wait failure"):
        db.defer_comment_slot_and_set_network_wait(
            task_id,
            slot["id"],
            campaign_id,
            scheduled_at=retry_at,
            slot_result="Waiting for network",
            reason="No network",
        )

    failed_campaign = db.get_comment_campaign(campaign_id)
    failed_slot = db.get_comment_schedule(campaign_id, limit=10)[0]
    assert failed_campaign["status"] == "running"
    assert failed_campaign["network_failure_count"] == 0
    assert failed_slot["status"] == "running"
    assert failed_slot["task_id"] == task_id
    assert failed_slot["scheduled_at"] == slot["scheduled_at"]

    with db.get_connection() as conn:
        conn.execute("DROP TRIGGER fail_comment_network_wait")
    assert db.defer_comment_slot_and_set_network_wait(
        task_id,
        slot["id"],
        campaign_id,
        scheduled_at=retry_at,
        slot_result="Waiting for network",
        reason="No network",
    )
    final_campaign = db.get_comment_campaign(campaign_id)
    final_slot = db.get_comment_schedule(campaign_id, limit=10)[0]
    assert final_campaign["status"] == "network_wait"
    assert final_campaign["network_failure_count"] == 1
    assert final_slot["status"] == "pending"
    assert final_slot["task_id"] is None
    assert from_db_time(final_slot["scheduled_at"]) == retry_at


def test_join_network_wait_entry_rolls_back_slot_when_campaign_update_fails(
    tmp_path,
) -> None:
    db = Database(tmp_path / "join-network-entry.db")
    db.upsert_saved_dialog(
        {
            "peer_id": 101,
            "username": "join_target",
            "title": "Join target",
            "kind": "channel",
        },
        account_id=1,
    )
    start = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    campaign = db.create_join_campaign(2, max_per_hour=40, start_at=start)
    campaign_id = int(campaign["id"])
    slot, task_id = _queue_running_join_slot(db, campaign_id)
    retry_at = start + timedelta(minutes=10)

    with db.get_connection() as conn:
        conn.execute(
            """CREATE TRIGGER fail_join_network_wait
               BEFORE UPDATE OF status ON join_campaigns
               WHEN OLD.status='running' AND NEW.status='network_wait'
               BEGIN
                   SELECT RAISE(ABORT, 'injected join network wait failure');
               END"""
        )

    with pytest.raises(DatabaseError, match="injected join network wait failure"):
        db.defer_join_slot_and_set_network_wait(
            task_id,
            slot["id"],
            campaign_id,
            scheduled_at=retry_at,
            slot_result="Waiting for network",
            reason="No network",
        )

    failed_campaign = db.get_join_campaign(campaign_id)
    failed_slot = db.get_join_schedule(campaign_id, limit=10)[0]
    assert failed_campaign["status"] == "running"
    assert failed_campaign["network_failure_count"] == 0
    assert failed_slot["status"] == "running"
    assert failed_slot["task_id"] == task_id
    assert failed_slot["scheduled_at"] == slot["scheduled_at"]

    with db.get_connection() as conn:
        conn.execute("DROP TRIGGER fail_join_network_wait")
    assert db.defer_join_slot_and_set_network_wait(
        task_id,
        slot["id"],
        campaign_id,
        scheduled_at=retry_at,
        slot_result="Waiting for network",
        reason="No network",
    )
    final_campaign = db.get_join_campaign(campaign_id)
    final_slot = db.get_join_schedule(campaign_id, limit=10)[0]
    assert final_campaign["status"] == "network_wait"
    assert final_campaign["network_failure_count"] == 1
    assert final_slot["status"] == "pending"
    assert final_slot["task_id"] is None
    assert from_db_time(final_slot["scheduled_at"]) == retry_at


def test_comment_network_wait_resume_rolls_back_running_when_redistribution_fails(
    tmp_path, monkeypatch
) -> None:
    db = Database(tmp_path / "comment-network-resume.db")
    _add_comment_channels(db, 3)
    start = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["Comment"],
        daily_limit=3,
        slot_count=3,
        start_at=start,
        rng=random.Random(2),
    )
    campaign_id = int(campaign["id"])
    retry_at = start + timedelta(hours=2)
    assert db.set_campaign_network_wait(
        campaign_id, retry_at=retry_at, reason="No network"
    )

    before_campaign = db.get_comment_campaign(campaign_id)
    before_schedule = db.get_comment_schedule(campaign_id, limit=10)
    original = db._redistribute_pending_comment_slots_in_transaction

    def fail_after_status_update(_conn, _campaign_id, **_kwargs):
        raise DatabaseError("injected network resume redistribution failure")

    monkeypatch.setattr(
        db,
        "_redistribute_pending_comment_slots_in_transaction",
        fail_after_status_update,
    )
    resume_at = start + timedelta(days=1)
    with pytest.raises(
        DatabaseError, match="injected network resume redistribution failure"
    ):
        db.resume_network_wait_campaign(campaign_id, now=resume_at)

    failed_campaign = db.get_comment_campaign(campaign_id)
    failed_schedule = db.get_comment_schedule(campaign_id, limit=10)
    assert failed_campaign["status"] == "network_wait"
    assert failed_campaign["pause_reason"] == before_campaign["pause_reason"]
    assert failed_campaign["network_retry_at"] == before_campaign["network_retry_at"]
    assert failed_campaign["ends_at"] == before_campaign["ends_at"]
    assert [row["scheduled_at"] for row in failed_schedule] == [
        row["scheduled_at"] for row in before_schedule
    ]

    monkeypatch.setattr(
        db, "_redistribute_pending_comment_slots_in_transaction", original
    )
    retry_resume_at = resume_at + timedelta(minutes=1)
    assert db.resume_network_wait_campaign(campaign_id, now=retry_resume_at)
    final_campaign = db.get_comment_campaign(campaign_id)
    final_schedule = db.get_comment_schedule(campaign_id, limit=10)
    assert final_campaign["status"] == "running"
    assert final_campaign["network_retry_at"] is None
    assert all(
        from_db_time(row["scheduled_at"]) > retry_resume_at
        for row in final_schedule
        if row["status"] == "pending"
    )

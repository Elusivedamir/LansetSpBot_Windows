from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from core.account_restriction import (
    build_account_restriction_kwargs,
    get_account_restriction_state,
)
from core.campaign_schedule import to_db_time, utc_now
from storage.database import Database
from storage.db_common import DatabaseError

UTC = timezone.utc


def _due_comment_slot(db: Database, account_id: int):
    db.set_setting("telegram.account_id", account_id)
    db.insert_channel(
        {
            "channel_id": 10_001,
            "linked_chat_id": 20_001,
            "title": "Restricted comment target",
        }
    )
    campaign = db.create_comment_campaign(
        ["comment"],
        daily_limit=1,
        slot_count=1,
        continuous=False,
        start_at=utc_now() - timedelta(hours=1),
        account_id=account_id,
        rng=random.Random(1),
    )
    slot = db.get_comment_schedule(campaign["id"], limit=1)[0]
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE comment_schedule SET scheduled_at=? WHERE id=?",
            (to_db_time(utc_now() - timedelta(seconds=1)), int(slot["id"])),
        )
    queued = db.queue_due_comment_slot(now=utc_now())
    assert queued is not None
    task = db.claim_next_pending_task()
    assert task is not None
    assert db.mark_comment_slot_running(slot["id"], task["id"])
    return campaign, slot, task


def _due_join_slot(db: Database, account_id: int):
    db.set_setting("telegram.account_id", account_id)
    dialog_id = db.upsert_saved_dialog(
        {
            "peer_id": 30_001,
            "username": "restricted_join_target",
            "title": "Restricted join target",
            "kind": "channel",
        },
        account_id=account_id,
    )
    db.set_saved_dialog_membership(dialog_id, account_id, "left")
    campaign = db.create_join_campaign(account_id, rng=random.Random(2))
    slot = db.get_join_schedule(campaign["id"], limit=1)[0]
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE join_schedule SET scheduled_at=? WHERE id=?",
            (to_db_time(utc_now() - timedelta(seconds=1)), int(slot["id"])),
        )
    queued = db.queue_due_join_slot(now=utc_now())
    assert queued is not None
    task = db.claim_next_pending_task()
    assert task is not None
    assert db.mark_join_slot_running(slot["id"], task["id"])
    return campaign, slot, task, dialog_id


def _restriction_kwargs(db: Database, account_id: int) -> dict:
    return build_account_restriction_kwargs(
        db,
        account_id=account_id,
        code="peer_flood",
        message="Telegram restricted the account",
        details={"audit": True},
    )


def test_comment_restricted_finalization_rolls_back_everything(tmp_path) -> None:
    db = Database(tmp_path / "comment-restricted-rollback.db")
    campaign, slot, task = _due_comment_slot(db, 101)
    with db.get_connection() as conn:
        conn.execute(
            """CREATE TRIGGER fail_comment_restriction
               BEFORE INSERT ON account_restrictions
               BEGIN SELECT RAISE(ABORT, 'forced restricted rollback'); END"""
        )

    with pytest.raises(DatabaseError, match="forced restricted rollback"):
        db.finalize_comment_slot_outcome_with_restriction(
            task["id"],
            slot["id"],
            restriction_kwargs=_restriction_kwargs(db, 101),
            status="failed",
            result="Peer flood",
            channel_id=10_001,
            post_id=777,
            selected_text="comment",
            sent=False,
            consume_channel=False,
            campaign_pause_reason="Restricted",
        )

    stored_slot = db.get_comment_schedule(campaign["id"], limit=1)[0]
    stored_campaign = db.get_comment_campaign(campaign["id"])
    assert stored_slot["status"] == "running"
    assert stored_campaign["status"] == "running"
    assert stored_campaign["attempted_count"] == 0
    assert db.get_task(task["id"])["status"] == "running"
    assert db.get_comment_history(campaign_id=campaign["id"]) == []
    assert get_account_restriction_state(db, account_id=101)["active"] is False


def test_comment_restricted_finalization_commits_everything(tmp_path) -> None:
    db = Database(tmp_path / "comment-restricted-commit.db")
    campaign, slot, task = _due_comment_slot(db, 102)

    state = db.finalize_comment_slot_outcome_with_restriction(
        task["id"],
        slot["id"],
        restriction_kwargs=_restriction_kwargs(db, 102),
        status="failed",
        result="Peer flood",
        channel_id=10_001,
        post_id=778,
        selected_text="comment",
        sent=False,
        consume_channel=False,
        campaign_pause_reason="Restricted",
    )

    assert state["active"] is True
    assert state["finalized"] is True
    assert db.get_comment_schedule(campaign["id"], limit=1)[0]["status"] == "failed"
    assert db.get_comment_campaign(campaign["id"])["status"] == "stopped"
    assert db.get_task(task["id"])["status"] == "completed"
    assert len(db.get_comment_history(campaign_id=campaign["id"])) == 1
    assert get_account_restriction_state(db, account_id=102)["active"] is True


def test_join_restricted_finalization_rolls_back_everything(tmp_path) -> None:
    db = Database(tmp_path / "join-restricted-rollback.db")
    campaign, slot, task, dialog_id = _due_join_slot(db, 201)
    with db.get_connection() as conn:
        conn.execute(
            """CREATE TRIGGER fail_join_restriction
               BEFORE INSERT ON account_restrictions
               BEGIN SELECT RAISE(ABORT, 'forced restricted rollback'); END"""
        )

    with pytest.raises(DatabaseError, match="forced restricted rollback"):
        db.finalize_join_slot_outcome_with_restriction(
            task["id"],
            slot["id"],
            restriction_kwargs=_restriction_kwargs(db, 201),
            status="failed",
            result="Peer flood",
            joined=False,
            saved_dialog_id=dialog_id,
            account_id=201,
            membership_status="failed",
            membership_error="Peer flood",
            campaign_pause_reason="Restricted",
        )

    stored_slot = db.get_join_schedule(campaign["id"], limit=1)[0]
    stored_campaign = db.get_join_campaign(campaign["id"])
    membership = next(
        row for row in db.get_saved_dialogs(201) if row["id"] == dialog_id
    )
    assert stored_slot["status"] == "running"
    assert stored_campaign["status"] == "running"
    assert stored_campaign["attempted_count"] == 0
    assert db.get_task(task["id"])["status"] == "running"
    assert membership["membership_status"] == "left"
    assert get_account_restriction_state(db, account_id=201)["active"] is False


def test_join_restricted_finalization_commits_everything(tmp_path) -> None:
    db = Database(tmp_path / "join-restricted-commit.db")
    campaign, slot, task, dialog_id = _due_join_slot(db, 202)

    state = db.finalize_join_slot_outcome_with_restriction(
        task["id"],
        slot["id"],
        restriction_kwargs=_restriction_kwargs(db, 202),
        status="failed",
        result="Peer flood",
        joined=False,
        saved_dialog_id=dialog_id,
        account_id=202,
        membership_status="failed",
        membership_error="Peer flood",
        campaign_pause_reason="Restricted",
    )

    assert state["active"] is True
    assert state["finalized"] is True
    assert db.get_join_schedule(campaign["id"], limit=1)[0]["status"] == "failed"
    assert db.get_join_campaign(campaign["id"])["status"] == "stopped"
    assert db.get_task(task["id"])["status"] == "completed"
    membership = next(
        row for row in db.get_saved_dialogs(202) if row["id"] == dialog_id
    )
    assert membership["membership_status"] == "failed"
    assert get_account_restriction_state(db, account_id=202)["active"] is True


@pytest.mark.parametrize(
    ("elapsed", "allowed", "wait_seconds"),
    [
        (14.1, False, 1),
        (14.5, False, 1),
        (14.9, False, 1),
        (15.0, True, 0),
        (15.1, True, 0),
    ],
)
def test_join_guard_uses_full_fractional_interval(
    tmp_path, elapsed: float, allowed: bool, wait_seconds: int
) -> None:
    db = Database(tmp_path / f"fractional-{elapsed}.db")
    account_id = 301
    last = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
    now = last + timedelta(seconds=elapsed)
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO join_events(
                   linked_chat_id, joined_at, result, account_id)
               VALUES(?, ?, 'joined', ?)""",
            (40_001, to_db_time(last), account_id),
        )

    guard = db.get_join_guard(
        max_joins=100,
        min_interval_seconds=15,
        now=now,
        window_seconds=3600,
        account_id=account_id,
    )

    assert guard["allowed"] is allowed
    assert guard["wait_seconds"] == wait_seconds

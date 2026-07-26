from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from core.campaign_schedule import from_db_time
from storage.database import Database, DatabaseError

UTC = timezone.utc


def test_comment_resume_rolls_back_status_when_slot_redistribution_fails(
    tmp_path, monkeypatch
) -> None:
    db = Database(tmp_path / "atomic-resume.db")
    start = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=40,
        slot_count=3,
        duration_hours=24,
        continuous=False,
        start_at=start,
        rng=random.Random(1),
    )
    campaign_id = int(campaign["id"])
    assert db.pause_comment_campaign(campaign_id, reason="Пауза пользователя")

    before_campaign = db.get_comment_campaign(campaign_id)
    before_schedule = db.get_comment_schedule(campaign_id, limit=10)
    original_redistribute = db._redistribute_pending_comment_slots_in_transaction

    def fail_after_status_update(_conn, _campaign_id, **_kwargs):
        raise DatabaseError("injected redistribution failure")

    monkeypatch.setattr(
        db,
        "_redistribute_pending_comment_slots_in_transaction",
        fail_after_status_update,
    )

    with pytest.raises(DatabaseError, match="injected redistribution failure"):
        db.resume_comment_campaign(
            campaign_id,
            now=start + timedelta(days=2),
            rng=random.Random(2),
        )

    after_failure = db.get_comment_campaign(campaign_id)
    after_schedule = db.get_comment_schedule(campaign_id, limit=10)
    assert after_failure["status"] == "paused"
    assert after_failure["pause_reason"] == before_campaign["pause_reason"]
    assert after_failure["ends_at"] == before_campaign["ends_at"]
    assert [row["scheduled_at"] for row in after_schedule] == [
        row["scheduled_at"] for row in before_schedule
    ]

    # A failed attempt must remain retryable without restarting the application.
    monkeypatch.setattr(
        db,
        "_redistribute_pending_comment_slots_in_transaction",
        original_redistribute,
    )
    resumed_at = start + timedelta(days=2, minutes=1)
    assert db.resume_comment_campaign(
        campaign_id,
        now=resumed_at,
        rng=random.Random(3),
    )
    final_campaign = db.get_comment_campaign(campaign_id)
    final_schedule = db.get_comment_schedule(campaign_id, limit=10)
    assert final_campaign["status"] == "running"
    assert all(from_db_time(row["scheduled_at"]) > resumed_at for row in final_schedule)

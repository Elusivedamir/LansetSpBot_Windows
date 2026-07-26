from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from core.campaign_schedule import (
    from_db_time,
    generate_random_slots,
    redistribute_slots,
    to_db_time,
)
from storage.database import Database
from services.api import ServiceAPI

UTC = timezone.utc


def _add_linked_channels(db: Database, count: int) -> None:
    for index in range(1, count + 1):
        channel_id = 10_000 + index
        db.insert_channel(
            {
                "channel_id": channel_id,
                "title": f"Канал {index:03d}",
                "username": f"channel_{index}",
                "linked_chat_id": 20_000 + index,
            }
        )


def test_40_slots_are_randomized_across_full_24_hours():
    start = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    end = start + timedelta(hours=24)
    slots = generate_random_slots(start, end, 40, rng=random.Random(1234))

    assert len(slots) == 40
    assert slots == sorted(slots)
    assert all(start < item < end for item in slots)
    first_segment = timedelta(hours=24) / 40
    assert first_segment * 0.10 <= slots[0] - start <= first_segment * 0.90
    assert slots[-1] - start > timedelta(hours=23)
    assert len(set(slots)) == 40


def test_1000_slots_preserve_exact_count_and_order():
    start = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    end = start + timedelta(hours=24)
    slots = generate_random_slots(start, end, 1000, rng=random.Random(9876))

    assert len(slots) == 1000
    assert slots == sorted(slots)
    assert len(set(slots)) == 1000
    assert all(start < item < end for item in slots)
    assert slots[-1] - start > timedelta(hours=23, minutes=58)
    assert max((b - a).total_seconds() for a, b in zip(slots, slots[1:])) < 180


def test_1000_slots_seed_322_preserves_exact_30_second_floor():
    """Regression: binary float subtraction used to reject this valid cadence."""
    start = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    end = start + timedelta(hours=24)

    slots = generate_random_slots(
        start,
        end,
        1000,
        rng=random.Random(322),
        minimum_gap_seconds=30,
    )

    assert len(slots) == 1000
    assert slots == sorted(slots)
    assert all(b - a >= timedelta(seconds=30) for a, b in zip(slots, slots[1:]))


def test_campaign_is_persistent_and_queues_only_one_due_slot(tmp_path):
    db = Database(tmp_path / "campaign.db")
    _add_linked_channels(db, 3)
    start = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["Первый", "Второй"],
        daily_limit=40,
        duration_hours=24,
        continuous=True,
        start_at=start,
        rng=random.Random(5),
    )

    schedule = db.get_comment_schedule(campaign["id"], limit=100)
    assert len(schedule) == 40
    assert all(row["status"] == "pending" for row in schedule)

    due = from_db_time(schedule[0]["scheduled_at"]) + timedelta(seconds=1)
    queued = db.queue_due_comment_slot(now=due)
    assert queued is not None
    assert queued["campaign_id"] == campaign["id"]
    assert queued["slot_id"] == schedule[0]["id"]
    assert db.queue_due_comment_slot(now=due) is None

    task = db.get_task(queued["task_id"])
    payload = json.loads(task["payload"])
    assert task["type"] == "auto_comment_slot"
    assert payload == {
        "campaign_id": campaign["id"],
        "slot_id": schedule[0]["id"],
        "account_id": campaign["account_id"],
    }


def test_restart_redistribution_never_creates_catch_up_burst(tmp_path):
    db = Database(tmp_path / "restart.db")
    _add_linked_channels(db, 2)
    start = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=40,
        duration_hours=24,
        start_at=start,
        rng=random.Random(10),
    )
    now = start + timedelta(hours=8)
    moved = db.redistribute_pending_comment_slots(
        campaign["id"], now=now, grace_seconds=0, rng=random.Random(11)
    )
    assert moved > 0

    rows = db.get_comment_schedule(campaign["id"], limit=100)
    future = [
        from_db_time(row["scheduled_at"]) for row in rows if row["status"] == "pending"
    ]
    assert future
    assert min(future) >= now + timedelta(minutes=2)
    assert future == sorted(future)
    assert all((b - a) >= timedelta(minutes=2) for a, b in zip(future, future[1:]))


def test_dense_redistribution_supports_short_remaining_windows():
    now = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    slots = redistribute_slots(now, now + timedelta(minutes=4), 5, rng=random.Random(1))
    assert len(slots) == 5
    assert slots == sorted(slots)
    first_segment = timedelta(minutes=4) / 5
    assert now + first_segment * 0.10 <= min(slots) <= now + first_segment * 0.90
    assert max(slots) < now + timedelta(minutes=4)


def test_80_channels_rotate_40_then_remaining_40(tmp_path):
    db = Database(tmp_path / "rotation80.db")
    _add_linked_channels(db, 80)

    first = db.get_channels_for_commenting(40)
    assert len(first) == 40
    for row in first:
        assert db.mark_channel_comment_checked(row["channel_id"])

    second = db.get_channels_for_commenting(40)
    assert len(second) == 40
    assert {row["channel_id"] for row in first}.isdisjoint(
        {row["channel_id"] for row in second}
    )


def test_five_channels_cycle_without_duplicate_before_full_round(tmp_path):
    db = Database(tmp_path / "rotation5.db")
    _add_linked_channels(db, 5)

    picked = []
    for _ in range(5):
        row = db.get_channels_for_commenting(1)[0]
        picked.append(row["channel_id"])
        db.mark_channel_comment_checked(row["channel_id"])

    assert len(set(picked)) == 5
    assert db.get_channels_for_commenting(1)[0]["channel_id"] in set(picked)


def test_join_guard_enforces_daily_limit_and_minimum_interval(tmp_path):
    db = Database(tmp_path / "joins.db")
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    with db.get_connection() as conn:
        for index in range(6):
            conn.execute(
                "INSERT INTO join_events(linked_chat_id, joined_at, result) VALUES(?, ?, 'joined')",
                (30_000 + index, to_db_time(now - timedelta(hours=index + 1))),
            )
    guard = db.get_join_guard(max_joins=6, min_interval_seconds=45 * 60, now=now)
    assert guard["allowed"] is False
    assert guard["remaining"] == 0

    db2 = Database(tmp_path / "joins_interval.db")
    with db2.get_connection() as conn:
        conn.execute(
            "INSERT INTO join_events(linked_chat_id, joined_at, result) VALUES(?, ?, 'joined')",
            (40_001, to_db_time(now - timedelta(minutes=10))),
        )
    guard = db2.get_join_guard(max_joins=6, min_interval_seconds=45 * 60, now=now)
    assert guard["allowed"] is False
    assert 34 * 60 <= guard["wait_seconds"] <= 35 * 60


def test_twenty_hour_downtime_preserves_every_pending_slot(tmp_path):
    db = Database(tmp_path / "downtime20.db")
    _add_linked_channels(db, 40)
    start = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=40,
        duration_hours=24,
        start_at=start,
        rng=random.Random(20),
    )

    now = start + timedelta(hours=20)
    moved = db.redistribute_pending_comment_slots(
        campaign["id"], now=now, grace_seconds=0, rng=random.Random(21)
    )
    rows = db.get_comment_schedule(campaign["id"], limit=100)
    pending = [row for row in rows if row["status"] == "pending"]
    missed = [row for row in rows if row["status"] == "missed"]

    assert moved == 40
    assert len(pending) == 40
    assert missed == []
    future = [from_db_time(row["scheduled_at"]) for row in pending]
    first_segment = timedelta(hours=24) / 40
    assert now + first_segment * 0.10 <= min(future) <= now + first_segment * 0.90
    assert max(future) < now + timedelta(hours=24)
    refreshed = db.get_comment_campaign(campaign["id"])
    assert from_db_time(refreshed["ends_at"]) > max(future)


def test_network_wait_defers_slot_without_consuming_attempt(tmp_path):
    db = Database(tmp_path / "network-wait.db")
    db.set_setting("telegram.account_id", 1)
    _add_linked_channels(db, 1)
    start = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=1,
        duration_hours=24,
        start_at=start,
        rng=random.Random(2),
    )
    slot = db.get_comment_schedule(campaign["id"], limit=5)[0]
    due = from_db_time(slot["scheduled_at"]) + timedelta(seconds=1)
    queued = db.queue_due_comment_slot(now=due)
    assert queued is not None
    assert db.mark_comment_slot_running(slot["id"], queued["task_id"])

    retry_at = due + timedelta(minutes=3)
    assert db.defer_comment_slot_and_set_network_wait(
        queued["task_id"],
        slot["id"],
        campaign["id"],
        scheduled_at=retry_at,
        slot_result="Ожидание сети",
        reason="Нет сети",
    )

    state = db.get_comment_campaign(campaign["id"])
    deferred = db.get_comment_schedule(campaign["id"], limit=5)[0]
    assert state["status"] == "network_wait"
    assert state["network_failure_count"] == 1
    assert state["attempted_count"] == 0
    assert deferred["status"] == "pending"
    assert deferred["task_id"] is None
    assert from_db_time(deferred["scheduled_at"]) == retry_at.replace(microsecond=0)
    assert db.get_active_comment_campaign()["id"] == campaign["id"]
    assert db.resume_network_wait_campaign(
        campaign["id"], now=retry_at + timedelta(seconds=1)
    )
    assert db.get_comment_campaign(campaign["id"])["status"] == "running"


def test_local_sqlite_setting_and_campaign_limit_persist_across_instances(tmp_path):
    path = tmp_path / "local-sync.db"
    db = Database(path)
    db.set_setting("commenting.daily_limit", 137)
    _add_linked_channels(db, 2)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=137,
        duration_hours=24,
        start_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        rng=random.Random(42),
    )

    reopened = Database(path)
    assert reopened.get_setting("commenting.daily_limit") == "137"
    assert reopened.get_comment_campaign(campaign["id"])["daily_limit"] == 137
    assert len(reopened.get_comment_schedule(campaign["id"], limit=1000)) == 137
    health = reopened.health_check()
    assert health["quick_check"] == "ok"
    assert health["journal_mode"] == "wal"
    assert health["foreign_keys"] is True
    assert health["local_file"] is True


def test_commenting_24h_cooldown_excludes_old_channels_but_accepts_new_sync(tmp_path):
    db = Database(tmp_path / "cooldown.db")
    _add_linked_channels(db, 5)
    first = db.get_channels_for_commenting(10, cooldown_hours=24)
    assert len(first) == 5
    for row in first:
        assert db.mark_channel_comment_checked(row["channel_id"])

    assert db.count_channels_for_commenting(cooldown_hours=24) == 0
    assert db.get_channels_for_commenting(10, cooldown_hours=24) == []

    # Simulate «Получить мои каналы»: the sync upserts both the five old
    # channels and ten newly added channels. Existing cooldown timestamps must
    # survive the upsert instead of making the old five eligible again.
    db.upsert_channels_batch(
        [
            {
                "channel_id": 10_000 + index,
                "title": f"Канал после синхронизации {index}",
                "username": f"synced_channel_{index}",
                "linked_chat_id": 20_000 + index,
            }
            for index in range(1, 16)
        ]
    )

    eligible = db.get_channels_for_commenting(20, cooldown_hours=24)
    assert len(eligible) == 10
    assert {row["channel_id"] for row in eligible}.isdisjoint(
        {row["channel_id"] for row in first}
    )


def test_service_campaign_caps_attempts_to_unique_eligible_channels(tmp_path):
    db = Database(tmp_path / "cap.db")
    db.set_setting("telegram.account_id", 101)
    _add_linked_channels(db, 5)
    api = ServiceAPI(db)

    campaign = api.start_comment_campaign(
        ["Комментарий"], continuous=False, daily_limit=33
    )

    assert campaign["requested_daily_limit"] == 33
    assert campaign["eligible_channel_count"] == 5
    assert campaign["daily_limit"] == 33
    assert campaign["planned_count"] == 5
    assert len(db.get_comment_schedule(campaign["id"], limit=100)) == 5


def test_small_available_set_keeps_slider_cadence_instead_of_full_day(tmp_path):
    db = Database(tmp_path / "slider-cadence.db")
    start = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=1000,
        slot_count=11,
        duration_hours=24,
        continuous=False,
        start_at=start,
        rng=random.Random(77),
    )

    schedule = db.get_comment_schedule(campaign["id"], limit=100)
    moments = [from_db_time(row["scheduled_at"]) for row in schedule]
    assert campaign["daily_limit"] == 1000
    assert len(moments) == 11
    assert moments == sorted(moments)
    assert moments[0] < start + timedelta(minutes=2)
    assert moments[-1] < start + timedelta(minutes=17)


def test_dense_restart_redistribution_preserves_slider_cadence(tmp_path):
    db = Database(tmp_path / "slider-restart.db")
    start = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=1000,
        slot_count=11,
        duration_hours=24,
        start_at=start,
        rng=random.Random(9),
    )
    now = start + timedelta(minutes=10)
    moved = db.redistribute_pending_comment_slots(
        campaign["id"], now=now, grace_seconds=0, force=True, rng=random.Random(10)
    )
    assert moved == 11
    rows = db.get_comment_schedule(campaign["id"], limit=100)
    future = [
        from_db_time(row["scheduled_at"]) for row in rows if row["status"] == "pending"
    ]
    assert len(future) == 11
    assert min(future) >= now + timedelta(seconds=30)
    assert max(future) < now + timedelta(minutes=18)


def test_campaign_completes_when_last_planned_unique_channel_finishes(tmp_path):
    db = Database(tmp_path / "early-complete.db")
    _add_linked_channels(db, 1)
    start = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=1,
        duration_hours=24,
        continuous=False,
        start_at=start,
        rng=random.Random(1),
    )
    slot = db.get_comment_schedule(campaign["id"], limit=5)[0]
    due = from_db_time(slot["scheduled_at"]) + timedelta(seconds=1)
    queued = db.queue_due_comment_slot(now=due)
    assert queued is not None
    assert db.mark_comment_slot_running(slot["id"], queued["task_id"])
    assert db.finish_comment_slot(
        slot["id"],
        status="skipped",
        result="Проверено",
        channel_id=10_001,
        post_id=900,
    )
    completed = db.get_comment_campaign(campaign["id"])
    assert completed["status"] == "completed"
    assert completed["attempted_count"] == 1
    assert completed["pause_reason"] == "Все запланированные каналы обработаны"

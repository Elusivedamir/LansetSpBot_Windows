from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.account_restriction import (
    activate_account_restriction,
    clear_account_restriction_after_spambot_confirmation,
    get_account_restriction_state,
)
from core.campaign_schedule import generate_random_slots
from storage.database import Database
from storage.db_common import DatabaseError


def _campaign(db: Database, account_id: int, *, allow_existing: bool = False):
    return db.create_comment_campaign(
        [f"comment-{account_id}"],
        daily_limit=40,
        slot_count=1,
        account_id=account_id,
        allow_existing=allow_existing,
    )


def test_restrictions_are_independent_across_sequential_and_multi_accounts(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "account-restrictions.db")
    db.set_setting("telegram.account_id", 101)
    campaign_101 = _campaign(db, 101)
    task_101 = db.insert_task(
        "auto_comment_slot",
        {"account_id": 101, "campaign_id": campaign_101["id"], "slot_id": 1},
    )

    db.set_setting("telegram.account_id", 202)
    campaign_202 = _campaign(db, 202)
    task_202 = db.insert_task(
        "auto_comment_slot",
        {"account_id": 202, "campaign_id": campaign_202["id"], "slot_id": 2},
    )

    first = activate_account_restriction(
        db,
        account_id=101,
        code="peer_flood",
        message="account 101 restricted",
    )
    assert first["account_id"] == 101
    assert get_account_restriction_state(db, account_id=101)["active"] is True
    assert get_account_restriction_state(db, account_id=202)["active"] is False
    assert db.get_comment_campaign(campaign_101["id"])["status"] == "stopped"
    assert db.get_comment_campaign(campaign_202["id"])["status"] == "running"
    assert db.get_task(task_101)["status"] == "cancelled"
    assert db.get_task(task_202)["status"] == "pending"

    second = activate_account_restriction(
        db,
        account_id=202,
        code="user_restricted",
        message="account 202 restricted",
    )
    assert second["account_id"] == 202
    assert get_account_restriction_state(db, account_id=101)["active"] is True
    assert get_account_restriction_state(db, account_id=202)["active"] is True

    clear_account_restriction_after_spambot_confirmation(db, account_id=202)
    assert get_account_restriction_state(db, account_id=202)["active"] is False
    assert get_account_restriction_state(db, account_id=101)["active"] is True

    db.set_setting("telegram.account_id", 202)
    assert get_account_restriction_state(db)["active"] is False
    db.set_setting("telegram.account_id", 101)
    assert get_account_restriction_state(db)["active"] is True


def test_restriction_activation_rolls_back_as_one_sqlite_transaction(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "restriction-rollback.db")
    db.set_setting("telegram.account_id", 303)
    campaign = _campaign(db, 303)
    task_id = db.insert_task(
        "auto_comment_slot",
        {"account_id": 303, "campaign_id": campaign["id"], "slot_id": 1},
    )
    with db.get_connection() as conn:
        conn.execute(
            """CREATE TRIGGER fail_restriction_task_cancel
               BEFORE UPDATE OF status ON tasks
               WHEN NEW.status='cancelled'
               BEGIN SELECT RAISE(ABORT, 'forced atomic rollback'); END"""
        )

    with pytest.raises(DatabaseError, match="forced atomic rollback"):
        activate_account_restriction(
            db,
            account_id=303,
            code="peer_flood",
            message="must roll back",
        )

    assert get_account_restriction_state(db, account_id=303)["active"] is False
    assert db.get_comment_campaign(campaign["id"])["status"] == "running"
    assert db.get_task(task_id)["status"] == "pending"


def test_v10_global_restriction_migrates_to_account_scoped_v19(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v18.db"
    bootstrap = Database(path)
    bootstrap.set_setting("telegram.account_id", 404)
    bootstrap.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE account_restrictions")
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            ("telegram.restriction.active", "1"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            ("telegram.restriction.account_id", "404"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            ("telegram.restriction.code", "peer_flood"),
        )
        conn.execute("DELETE FROM migrations WHERE version=19")
        conn.execute("PRAGMA user_version=18")
        conn.commit()
    finally:
        conn.close()

    migrated = Database(path)
    state = get_account_restriction_state(migrated, account_id=404)
    assert migrated.get_version() == Database.SCHEMA_VERSION == 30
    assert state["active"] is True
    assert state["code"] == "peer_flood"
    assert migrated.get_settings(prefix="telegram.restriction.") == {}


def test_slider_cadence_is_random_per_comment_and_contains_service_pauses() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    slots = generate_random_slots(
        start,
        start + timedelta(hours=24),
        1000,
        rng=random.Random(47),
        minimum_gap_seconds=30,
    )
    gaps = [(right - left).total_seconds() for left, right in zip(slots, slots[1:])]

    # 1000/day is the densest slider setting: 86.4 seconds on average. The
    # independent 15-30 second JOIN/SEND_COMMENT service pauses therefore fit
    # inside the displayed cadence instead of being added as a fixed extra gap.
    assert 85.0 < sum(gaps) / len(gaps) < 88.0
    assert min(gaps) >= 30.0
    assert len({round(gap, 3) for gap in gaps}) > 900

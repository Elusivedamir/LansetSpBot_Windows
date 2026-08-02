from __future__ import annotations

import json
from datetime import timedelta

from core.campaign_schedule import to_db_time, utc_now
from services.multiaccount_scheduler import AccountCampaignDatabaseView
from storage.database import Database
from tools.check_critical_coverage import (
    BRANCH_GROUP_THRESHOLDS,
    LINE_GROUP_THRESHOLDS,
)


def _register_account(database: Database, account_id: int) -> None:
    database.register_telegram_account(
        telegram_account_id=account_id,
        session_name=f"account_{account_id}",
        display_name=f"Account {account_id}",
        username=f"account_{account_id}",
        authorized=True,
    )


def _assert_queued_task(
    database: Database,
    queued: dict[str, int],
    *,
    schedule_table: str,
    task_type: str,
    account_id: int,
) -> None:
    with database.get_connection() as connection:
        task = connection.execute(
            "SELECT account_id, type, payload, status FROM tasks WHERE id=?",
            (queued["task_id"],),
        ).fetchone()
        schedule = connection.execute(
            f"SELECT status, task_id FROM {schedule_table} WHERE id=?",
            (queued["slot_id"],),
        ).fetchone()

    assert task is not None
    assert int(task["account_id"]) == account_id
    assert str(task["type"]) == task_type
    assert str(task["status"]) == "pending"
    assert json.loads(str(task["payload"])) == {
        "campaign_id": queued["campaign_id"],
        "slot_id": queued["slot_id"],
        "account_id": account_id,
    }
    assert schedule is not None
    assert str(schedule["status"]) == "queued"
    assert int(schedule["task_id"]) == queued["task_id"]


def test_account_scoped_comment_slot_is_queued_once(tmp_path) -> None:
    database = Database(tmp_path / "comment-slot.db")
    account_id = 101
    now = utc_now()
    try:
        _register_account(database, account_id)
        campaign = database.create_comment_campaign(
            ["hello"],
            daily_limit=1,
            slot_count=1,
            continuous=False,
            start_at=now,
            account_id=account_id,
        )
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE comment_schedule SET scheduled_at=? WHERE campaign_id=?",
                (to_db_time(now - timedelta(seconds=1)), int(campaign["id"])),
            )

        view = AccountCampaignDatabaseView(database, account_id)
        queued = view.queue_due_comment_slot(now=now)

        assert queued is not None
        assert queued["account_id"] == account_id
        _assert_queued_task(
            database,
            queued,
            schedule_table="comment_schedule",
            task_type="auto_comment_slot",
            account_id=account_id,
        )
        assert view.queue_due_comment_slot(now=now) is None
    finally:
        database.close_thread_connection()


def test_account_scoped_join_slot_is_queued_once(tmp_path) -> None:
    database = Database(tmp_path / "join-slot.db")
    account_id = 202
    now = utc_now()
    try:
        _register_account(database, account_id)
        dialog_id = database.upsert_saved_dialog(
            {
                "peer_id": 9001,
                "username": "slot_queue_target",
                "title": "Slot queue target",
                "kind": "channel",
            },
            account_id=account_id,
        )
        database.set_saved_dialog_membership(dialog_id, account_id, "left")
        campaign = database.create_join_campaign(
            account_id,
            max_per_hour=40,
            start_at=now,
        )
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE join_schedule SET scheduled_at=? WHERE campaign_id=?",
                (to_db_time(now - timedelta(seconds=1)), int(campaign["id"])),
            )

        view = AccountCampaignDatabaseView(database, account_id)
        queued = view.queue_due_join_slot(now=now)

        assert queued is not None
        assert queued["account_id"] == account_id
        _assert_queued_task(
            database,
            queued,
            schedule_table="join_schedule",
            task_type="join_saved_slot",
            account_id=account_id,
        )
        assert view.queue_due_join_slot(now=now) is None
    finally:
        database.close_thread_connection()


def test_critical_coverage_floors_preserve_proven_baseline() -> None:
    assert LINE_GROUP_THRESHOLDS["multiaccount_runtime"][1] == 55.0
    assert LINE_GROUP_THRESHOLDS["account_gui_lifecycle"][1] == 65.0
    assert BRANCH_GROUP_THRESHOLDS["multiaccount_runtime"][1] == 40.0
    assert BRANCH_GROUP_THRESHOLDS["account_gui_lifecycle"][1] == 35.0

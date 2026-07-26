from __future__ import annotations

import random
import stat
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import services.api_parts.comments as comments_api_module
from core.campaign_schedule import from_db_time
from services.api_parts.comments import CommentCampaignAPIMixin
from storage.database import Database
from tests.test_composition_resilience import _Telegram, _comment_database, _handlers


def test_shutdown_tick_never_starts_secret_migration_retry(monkeypatch) -> None:
    migration_required = threading.Event()
    migration_required.set()
    api = SimpleNamespace(
        _shutdown_requested=True,
        _secret_migration_required=migration_required,
    )
    thread_factory = MagicMock()
    monkeypatch.setattr(comments_api_module.threading, "Thread", thread_factory)

    CommentCampaignAPIMixin._campaign_tick(api)

    thread_factory.assert_not_called()


def test_cancelled_comment_task_is_resumable_without_spending_slot(tmp_path) -> None:
    database = Database(tmp_path / "cancelled-comment-resume.db")
    start = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    campaign = database.create_comment_campaign(
        ["hello"],
        daily_limit=1,
        slot_count=1,
        continuous=False,
        start_at=start,
        rng=random.Random(41),
    )
    campaign_id = int(campaign["id"])
    slot = database.get_comment_schedule(campaign_id, limit=10)[0]
    queued = database.queue_due_comment_slot(
        now=from_db_time(slot["scheduled_at"]) + timedelta(seconds=1)
    )
    assert queued is not None
    assert database.cancel_task(queued["task_id"])

    assert database.reconcile_comment_schedule() == 1
    recovered = database.get_comment_schedule(campaign_id, limit=10)[0]
    assert recovered["status"] == "pending"
    assert recovered["task_id"] is None
    assert recovered["executed_at"] is None
    assert database.get_comment_campaign(campaign_id)["attempted_count"] == 0

    resumed_at = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
    assert database.resume_comment_campaign(
        campaign_id, now=resumed_at, rng=random.Random(42)
    )
    resumed = database.get_comment_schedule(campaign_id, limit=10)[0]
    assert resumed["status"] == "pending"
    assert from_db_time(resumed["scheduled_at"]) >= resumed_at
    assert (
        database.queue_due_comment_slot(
            now=from_db_time(resumed["scheduled_at"]) + timedelta(seconds=1)
        )
        is not None
    )


def test_cancelled_join_task_is_resumable_without_spending_slot(tmp_path) -> None:
    database = Database(tmp_path / "cancelled-join-resume.db")
    account_id = 51
    dialog_id = database.upsert_saved_dialog(
        {
            "peer_id": 51001,
            "username": "cancelled_join_resume",
            "title": "Cancelled join resume",
            "kind": "channel",
        },
        account_id=account_id,
    )
    database.set_saved_dialog_membership(dialog_id, account_id, "left")
    campaign = database.create_join_campaign(
        account_id,
        max_per_hour=40,
        start_at=datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc),
        rng=random.Random(51),
    )
    campaign_id = int(campaign["id"])
    slot = database.get_join_schedule(campaign_id, limit=10)[0]
    queued = database.queue_due_join_slot(
        now=from_db_time(slot["scheduled_at"]) + timedelta(seconds=1)
    )
    assert queued is not None
    assert database.cancel_task(queued["task_id"])

    assert database.reconcile_join_schedule() == 1
    recovered = database.get_join_schedule(campaign_id, limit=10)[0]
    assert recovered["status"] == "pending"
    assert recovered["task_id"] is None
    assert recovered["executed_at"] is None
    assert database.get_join_campaign(campaign_id)["attempted_count"] == 0

    assert database.resume_join_campaign(campaign_id)
    resumed = database.get_join_schedule(campaign_id, limit=10)[0]
    assert resumed["status"] == "pending"
    assert (
        database.queue_due_join_slot(
            now=from_db_time(resumed["scheduled_at"]) + timedelta(seconds=1)
        )
        is not None
    )


def test_uncertain_comment_join_counts_against_guard_until_dialog_sync(
    tmp_path,
) -> None:
    database = Database(tmp_path / "uncertain-comment-join.db")
    account_id = 77
    peer_id = -1000000077001

    dialog_id = database.set_peer_membership_uncertain(
        peer_id,
        account_id,
        "join result unknown",
        title="Discussion group",
    )
    dialog = {row["id"]: row for row in database.get_saved_dialogs(account_id)}[
        dialog_id
    ]
    assert dialog["peer_id"] == peer_id
    assert dialog["membership_status"] == "uncertain"
    guard = database.get_join_guard(
        max_joins=40,
        min_interval_seconds=0,
        account_id=account_id,
    )
    assert guard["uncertain_count"] == 1
    assert guard["effective_count"] == 1

    resolved_id = database.upsert_saved_dialog(
        {
            "peer_id": peer_id,
            "title": "Discussion group",
            "kind": "group",
        },
        account_id=account_id,
    )
    assert resolved_id == dialog_id
    resolved = {row["id"]: row for row in database.get_saved_dialogs(account_id)}[
        dialog_id
    ]
    assert resolved["membership_status"] == "member"
    guard = database.get_join_guard(
        max_joins=40,
        min_interval_seconds=0,
        account_id=account_id,
    )
    assert guard["uncertain_count"] == 0
    assert guard["joined_count"] == 1


@pytest.mark.asyncio
async def test_comment_uses_links_preflight_without_membership_probe(
    monkeypatch,
) -> None:
    database = _comment_database()
    database.get_setting.return_value = 77
    database.get_channels_for_commenting.return_value[0]["linked_chat_title"] = (
        "Discussion group"
    )
    telegram = _Telegram()
    telegram.member = False
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, database, telegram)

    await handlers["auto_comment_slot"](
        {"id": 71, "payload": {"campaign_id": 1, "slot_id": 71}}
    )

    assert telegram.member_calls == []
    assert telegram.join_calls == []
    database.set_peer_membership_uncertain.assert_not_called()
    assert len(comments.sent) == 1
    assert database.finish_comment_slot.call_args.kwargs["status"] == "sent"
    database.pause_campaign_for_safety.assert_not_called()



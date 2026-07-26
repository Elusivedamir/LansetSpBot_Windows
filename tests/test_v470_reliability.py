from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.campaign_schedule import from_db_time, to_db_time
from core.exceptions import NonRetryableTelegramError
from core.secret_store import SecretStore
from services.api import ServiceAPI
from services.comment_service import CommentService
from storage.database import Database

UTC = timezone.utc


def test_pending_task_type_is_not_limited_to_first_twenty(tmp_path):
    db = Database(tmp_path / "pending.db")
    for _ in range(25):
        db.insert_task("noop", {})
    db.insert_task("auto_comment_slot", {"campaign_id": 1, "slot_id": 1})
    assert db.has_pending_task_type("auto_comment_slot") is True


@pytest.mark.asyncio
async def test_sent_comment_is_reserved_if_receipt_finalization_fails(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "delivery.db")

    class Telegram:
        async def send_comment(self, channel_id, post_id, text, reply_to=None):
            return SimpleNamespace(id=99, sender_id=7, date=None)

    service = CommentService(Telegram(), linked_chat_service=None, db=db)

    def fail_finalize(_data):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "finalize_comment_delivery", fail_finalize)
    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.ensure_and_send_comment(
            channel_id=10,
            linked_chat_id=20,
            post_message_id=30,
            text="hello",
            membership_ready=True,
        )
    assert raised.value.code == "delivery_persist_failed"
    assert db.has_commented(10, 30) is True
    with db.get_connection() as conn:
        status = conn.execute(
            "SELECT status FROM comment_deliveries WHERE channel_id=10 AND post_id=30"
        ).fetchone()[0]
    assert status in {"sending", "uncertain"}


@pytest.mark.asyncio
async def test_known_send_failure_releases_delivery_reservation(tmp_path):
    db = Database(tmp_path / "known-failure.db")

    class Telegram:
        async def send_comment(self, channel_id, post_id, text, reply_to=None):
            raise NonRetryableTelegramError("forbidden", code="chat_write_forbidden")

    service = CommentService(Telegram(), linked_chat_service=None, db=db)
    with pytest.raises(NonRetryableTelegramError):
        await service.ensure_and_send_comment(
            channel_id=1,
            linked_chat_id=2,
            post_message_id=3,
            text="hello",
            membership_ready=True,
        )
    assert db.has_commented(1, 3) is False


def test_saved_dialogs_survive_account_switch_and_campaign_excludes_members(tmp_path):
    db = Database(tmp_path / "saved.db")
    for index in range(3):
        db.upsert_saved_dialog(
            {
                "peer_id": 100 + index,
                "username": f"public_{index}",
                "title": f"Dialog {index}",
                "kind": "channel",
            },
            account_id=1,
            phone="+1000",
        )
    db.set_saved_dialog_membership(1, 2, "member")
    campaign = db.create_join_campaign(
        2,
        max_per_hour=40,
        start_at=datetime(2026, 7, 11, 10, 0, tzinfo=UTC),
    )
    schedule = db.get_join_schedule(campaign["id"], limit=100)
    assert len(schedule) == 2
    assert {row["saved_dialog_id"] for row in schedule} == {2, 3}


def test_join_schedule_never_exceeds_40_in_rolling_hour(tmp_path):
    db = Database(tmp_path / "joins.db")
    for index in range(90):
        db.upsert_saved_dialog(
            {
                "peer_id": 1_000 + index,
                "username": f"channel_{index}",
                "title": f"Channel {index}",
                "kind": "channel",
            },
            account_id=1,
        )
    campaign = db.create_join_campaign(
        2,
        max_per_hour=40,
        start_at=datetime(2026, 7, 11, 10, 0, tzinfo=UTC),
    )
    moments = [
        from_db_time(row["scheduled_at"])
        for row in db.get_join_schedule(campaign["id"], 200)
    ]
    assert len(moments) == 90
    for moment in moments:
        assert (
            sum(
                1
                for candidate in moments
                if moment <= candidate < moment + timedelta(hours=1)
            )
            <= 40
        )


def test_sliding_join_guard_defers_41st_success(tmp_path):
    db = Database(tmp_path / "guard.db")
    now = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    with db.get_connection() as conn:
        for index in range(40):
            conn.execute(
                "INSERT INTO join_events(linked_chat_id, joined_at, result) VALUES(?, ?, 'joined')",
                (index + 1, to_db_time(now - timedelta(seconds=90 * index))),
            )
    guard = db.get_join_guard(
        max_joins=40,
        min_interval_seconds=45,
        window_seconds=3600,
        now=now,
    )
    assert guard["allowed"] is False
    assert guard["remaining"] == 0
    assert guard["wait_seconds"] > 0


def test_secret_settings_are_removed_from_sqlite(tmp_path):
    db = Database(tmp_path / "secrets.db")
    store = SecretStore(tmp_path / "private-secrets.json")
    api = ServiceAPI(db, queue_worker=None, secret_store=store)
    api.save_settings(
        {
            "telegram.api_id": "123",
            "telegram.api_hash": "hash-secret",
            "telegram.proxy_password": "proxy-secret",
        }
    )
    assert db.get_setting("telegram.api_hash") is None
    assert db.get_setting("telegram.proxy_password") is None
    values = api.get_settings("telegram.")
    assert values["telegram.api_hash"] == "hash-secret"
    assert values["telegram.proxy_password"] == "proxy-secret"
    api.prepare_shutdown()


def test_prune_channels_removes_previous_account_working_set(tmp_path):
    db = Database(tmp_path / "prune.db")
    for channel_id in (1, 2, 3):
        db.insert_channel({"channel_id": channel_id, "title": str(channel_id)})
    db.prune_channels_except([2, 3])
    assert {row["channel_id"] for row in db.get_channels()} == {2, 3}


def test_stale_sending_delivery_becomes_uncertain_on_startup(tmp_path):
    path = tmp_path / "stale-delivery.db"
    db = Database(path)
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO comment_deliveries(
                   channel_id, post_id, status, reserved_at, updated_at
               ) VALUES(1, 2, 'sending', datetime('now', '-10 minutes'), datetime('now', '-10 minutes'))"""
        )
    Database(path)
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT status, error FROM comment_deliveries WHERE channel_id=1 AND post_id=2"
        ).fetchone()
    assert row["status"] == "uncertain"
    assert "unclean shutdown" in row["error"]
    assert db.has_commented(1, 2) is True


def test_completed_join_campaign_has_explicit_completed_status(tmp_path):
    db = Database(tmp_path / "completed-join.db")
    db.upsert_saved_dialog(
        {
            "peer_id": 100,
            "username": "public_join",
            "title": "Public",
            "kind": "channel",
        },
        account_id=1,
    )
    campaign = db.create_join_campaign(
        2,
        max_per_hour=40,
        start_at=datetime(2026, 7, 11, 10, 0, tzinfo=UTC),
    )
    assert db.complete_join_campaign(campaign["id"]) is True
    state = db.get_join_campaign(campaign["id"])
    assert state["status"] == "completed"
    assert db.get_active_join_campaign() is None


def test_due_join_slot_queues_only_once(tmp_path):
    db = Database(tmp_path / "join-queue.db")
    db.upsert_saved_dialog(
        {
            "peer_id": 100,
            "username": "public_queue",
            "title": "Queue",
            "kind": "channel",
        },
        account_id=1,
    )
    now = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)
    campaign = db.create_join_campaign(2, max_per_hour=40, start_at=now)
    schedule = db.get_join_schedule(campaign["id"], limit=10)
    due = from_db_time(schedule[0]["scheduled_at"]) + timedelta(seconds=1)
    first = db.queue_due_join_slot(now=due)
    second = db.queue_due_join_slot(now=due)
    assert first is not None
    assert second is None
    assert db.has_pending_task_type("join_saved_slot") is True


def test_fallback_secret_file_is_owner_only(tmp_path):
    import os
    import stat

    path = tmp_path / "nested" / "secrets.json"
    store = SecretStore(path)
    store.set("telegram.api_hash", "secret")
    assert store.get("telegram.api_hash") == "secret"
    if os.name == "nt":
        # Windows uses NTFS ACLs; stat() exposes synthetic POSIX mode bits.
        assert SecretStore._restrict_windows_acl(path) is True
    else:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

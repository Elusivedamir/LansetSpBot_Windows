from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from telethon import types

from core.campaign_schedule import utc_now
from services.telegram_service import TelegramService
from storage.database import Database
from tests.test_composition_resilience import _Telegram, _handlers
from workers.queue_worker import QueueWorker


class _NoLinkRpc:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def get_linked_chat_id(self, channel_id):
        self.calls.append(int(channel_id))
        raise AssertionError("ordinary groups must be classified locally")


@pytest.mark.asyncio
async def test_ordinary_group_classification_uses_zero_telegram_rpc(monkeypatch):
    db = MagicMock()
    db.get_setting.return_value = 77
    group_id = -1000000000200
    db.get_channels.return_value = [
        {
            "channel_id": group_id,
            "title": "Ordinary group",
            "target_kind": "group",
            "comment_mode": "pending",
            "linked_chat_id": group_id,
        }
    ]
    db.refresh_group_comment_modes.return_value = {
        "linked_discussion": 0,
        "direct_group": 1,
    }
    telegram = _Telegram()
    linked = _NoLinkRpc()
    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch, db, telegram, linked=linked
    )

    await handlers["link_channels"]({"id": 1001, "payload": {"account_id": 77}})

    assert linked.calls == []
    assert telegram.join_calls == []
    db.update_group_link_classification.assert_called_once_with(
        group_id,
        is_linked=False,
        status="Группа · локально определена как обычная",
        account_id=77,
    )


def test_peer_reference_reconstructs_input_peer_without_entity_lookup():
    service = object.__new__(TelegramService)
    service._peer_references = {}

    peer = service.register_peer_reference(
        123,
        access_hash=987654321,
        peer_type="channel",
    )

    assert isinstance(peer, types.InputPeerChannel)
    assert peer.channel_id == 123
    assert peer.access_hash == 987654321
    assert service._resolve_peer_reference(123) is peer


def test_negative_cache_excludes_target_until_expiry(tmp_path):
    db = Database(tmp_path / "negative-cache.db")
    db.set_setting("telegram.account_id", 77)
    db.upsert_channels_batch(
        [
            {
                "channel_id": 10,
                "title": "Channel",
                "target_kind": "channel",
                "comment_mode": "channel_post",
                "linked_chat_id": -1000000000020,
                "link_status": "Связано",
            }
        ],
        account_id=77,
    )

    assert len(db.get_channels_for_commenting(1, account_id=77)) == 1
    assert db.set_channel_negative_cache(
        10,
        "comments_disabled",
        ttl_seconds=3600,
        account_id=77,
    )
    assert db.get_channels_for_commenting(1, account_id=77) == []
    assert db.clear_channel_negative_cache(10, account_id=77)
    assert len(db.get_channels_for_commenting(1, account_id=77)) == 1


def test_comment_route_survives_retry_and_restart(tmp_path):
    path = tmp_path / "route-cache.db"
    db = Database(path)
    db.set_setting("telegram.account_id", 77)
    campaign = db.create_comment_campaign(
        ["hello"],
        daily_limit=1,
        slot_count=1,
        start_at=utc_now() - timedelta(seconds=1),
        account_id=77,
    )
    schedule = db.get_comment_schedule(campaign["id"])
    assert len(schedule) == 1
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE comment_schedule SET scheduled_at=datetime('now', '-1 minute') WHERE id=?",
            (int(schedule[0]["id"]),),
        )
    queued = db.queue_due_comment_slot(now=utc_now())
    assert queued is not None
    assert db.mark_comment_slot_running(queued["slot_id"], queued["task_id"])
    assert db.bind_comment_slot_target(
        queued["slot_id"],
        queued["task_id"],
        channel_id=10,
        post_id=55,
        linked_chat_id=-1000000000020,
        discussion_message_id=777,
    )
    db.close_thread_connection()

    reopened = Database(path)
    route = reopened.get_comment_slot_route(queued["slot_id"])
    assert route is not None
    assert route["channel_id"] == 10
    assert route["post_id"] == 55
    assert route["linked_chat_id"] == -1000000000020
    assert route["discussion_message_id"] == 777
    assert route["route_cached_at"] is not None
    assert reopened.get_comment_campaign(campaign["id"])["id"] == campaign["id"]


def test_production_worker_can_stay_connected_until_shutdown():
    worker = QueueWorker(lambda: {}, persistent_idle=True)
    assert worker.persistent_idle is True


def test_schema_v23_contains_rpc_optimization_columns(tmp_path):
    db = Database(tmp_path / "schema-v23.db")
    assert db.get_version() == Database.SCHEMA_VERSION
    with db.get_connection() as conn:
        channel_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(channels)")
        }
        schedule_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(comment_schedule)")
        }
    assert {
        "access_hash",
        "peer_type",
        "negative_status",
        "negative_until",
    } <= channel_columns
    assert {
        "linked_chat_id",
        "discussion_message_id",
        "route_cached_at",
    } <= schedule_columns

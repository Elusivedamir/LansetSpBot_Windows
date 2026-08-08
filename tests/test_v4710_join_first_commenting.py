from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.telegram_service import TelegramService
from storage.channel_repository_parts.queries import ChannelQueryRepositoryMixin
from tests.test_composition_resilience import (
    _Linked,
    _Telegram,
    _comment_database,
    _handlers,
)


class _ChannelQueryHarness(ChannelQueryRepositoryMixin):
    def __init__(self, connection) -> None:
        self.connection = connection

    def get_connection(self):
        return nullcontext(self.connection)

    @staticmethod
    def get_setting(_key, default=None):
        return default


@pytest.mark.asyncio
async def test_prepared_member_sends_without_join(monkeypatch):
    db = _comment_database()
    telegram = _Telegram()
    telegram.member_results = [True]
    telegram.latest_post = SimpleNamespace(
        status="ok",
        message=SimpleNamespace(id=901),
        discussion_chat_id=20,
        discussion_message_id=777,
    )
    handlers, _cleanup, comments, worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 301, "payload": {"campaign_id": 1, "slot_id": 301}}
    )

    assert telegram.member_calls == []
    assert telegram.join_calls == []
    assert worker.sleep_calls == []
    assert len(comments.sent) == 1
    assert db.finish_comment_slot.call_args.kwargs["status"] == "sent"


@pytest.mark.asyncio
async def test_comment_send_relies_on_links_preflight_not_membership_rpc(monkeypatch):
    db = _comment_database()
    telegram = _Telegram()
    telegram.member_results = [False]
    telegram.latest_post = SimpleNamespace(
        status="ok",
        message=SimpleNamespace(id=901),
        discussion_chat_id=20,
        discussion_message_id=777,
    )
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 302, "payload": {"campaign_id": 1, "slot_id": 302}}
    )

    assert telegram.member_calls == []
    assert telegram.join_calls == []
    assert len(comments.sent) == 1
    assert db.finish_comment_slot.call_args.kwargs["status"] == "sent"


def test_join_requested_is_excluded_by_durable_comment_membership_gate():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE channels(
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            username TEXT,
            title TEXT,
            target_kind TEXT,
            comment_mode TEXT,
            linked_chat_id INTEGER,
            linked_chat_title TEXT,
            link_status TEXT,
            last_sync_at DATETIME,
            last_comment_check_at DATETIME,
            access_hash INTEGER,
            peer_type TEXT,
            negative_status TEXT,
            negative_until DATETIME,
            local_ban_reason TEXT,
            local_ban_peer_id INTEGER,
            local_banned_at DATETIME
        );
        CREATE TABLE local_ban_targets(
            account_id INTEGER NOT NULL,
            peer_id INTEGER NOT NULL
        );
        """
    )
    rows = [
        (1, 77, 10, "10", "channel", "channel_post", 20,
         "Связано · заявка на вступление отправлена"),
        (2, 77, 11, "11", "channel", "channel_post", 21,
         "Связано · вступление выполнено"),
        (3, 77, 12, "12", "channel", "channel_post", 22,
         "Связано · участие уже было"),
        (4, 77, 13, "13", "channel", "channel_post", 23,
         "Связано · обсуждение уже в диалогах"),
        (5, 77, 14, "14", "channel", "channel_post", 24,
         "Связано · участие подтверждено"),
        (6, 77, 15, "15", "group", "direct_group", None,
         "Группа · локально определена как обычная"),
        (7, 77, 16, "16", "channel", "channel_post", 25, None),
    ]
    connection.executemany(
        """INSERT INTO channels(
               id, account_id, channel_id, title, target_kind, comment_mode,
               linked_chat_id, link_status
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    repository = _ChannelQueryHarness(connection)

    eligible = repository.get_channels_for_commenting(
        20, cooldown_hours=0, account_id=77
    )
    eligible_with_cooldown = repository.get_channels_for_commenting(
        20, cooldown_hours=24, account_id=77
    )

    assert {row["channel_id"] for row in eligible} == {11, 12, 13, 14, 15, 16}
    assert {row["channel_id"] for row in eligible_with_cooldown} == {
        11, 12, 13, 14, 15, 16
    }
    assert repository.count_channels_for_commenting(
        cooldown_hours=0, account_id=77
    ) == 6
    assert repository.count_channels_for_commenting(
        cooldown_hours=24, account_id=77
    ) == 6
    assert (
        repository.is_comment_link_membership_confirmed(
            10, 20, account_id=77
        )
        is False
    )
    assert repository.is_comment_link_membership_confirmed(
        11, 21, account_id=77
    )
    assert repository.is_comment_link_membership_confirmed(
        16, 25, account_id=77
    )
    assert (
        repository.is_comment_link_membership_confirmed(
            11, 999, account_id=77
        )
        is False
    )
    connection.close()


@pytest.mark.asyncio
async def test_links_preparation_joins_missing_discussions_with_15_25_second_gap(
    monkeypatch,
):
    db = MagicMock()
    db.get_setting.return_value = 77
    db.get_channels.return_value = [
        {"channel_id": 1, "title": "A", "target_kind": "channel"},
        {"channel_id": 2, "title": "B", "target_kind": "channel"},
    ]
    db.refresh_group_comment_modes.return_value = {
        "linked_discussion": 0,
        "direct_group": 0,
    }
    linked = _Linked()
    linked.links = {1: 20, 2: 30}
    linked.access = {20: False, 30: False}
    telegram = _Telegram()
    handlers, _cleanup, _comments, worker = _handlers(
        monkeypatch, db, telegram, linked=linked
    )

    await handlers["link_channels"]({"id": 303, "payload": {}})

    assert telegram.join_calls == [20, 30]
    assert len(worker.sleep_calls) == 1
    assert 15 <= worker.sleep_calls[0] <= 25
    assert db.record_join_event.call_args_list[0].args == (20, "joined")
    assert db.record_join_event.call_args_list[0].kwargs == {"account_id": 77}
    assert db.record_join_event.call_args_list[1].args == (30, "joined")
    assert db.record_join_event.call_args_list[1].kwargs == {"account_id": 77}


@pytest.mark.asyncio
async def test_links_preparation_uses_single_join_request_as_membership_probe(
    monkeypatch,
):
    db = MagicMock()
    db.get_channels.return_value = [
        {"channel_id": 1, "title": "A", "target_kind": "channel"}
    ]
    db.refresh_group_comment_modes.return_value = {
        "linked_discussion": 0,
        "direct_group": 0,
    }
    linked = _Linked()
    linked.links = {1: 20}
    linked.access = {20: True}
    telegram = _Telegram()
    handlers, _cleanup, _comments, worker = _handlers(
        monkeypatch, db, telegram, linked=linked
    )

    await handlers["link_channels"]({"id": 304, "payload": {}})

    telegram.join_result = False
    # Re-run with Telegram reporting USER_ALREADY_PARTICIPANT semantics.
    db.reset_mock()
    telegram.join_calls.clear()
    await handlers["link_channels"]({"id": 305, "payload": {}})
    assert telegram.join_calls == [20]
    assert worker.sleep_calls == []
    db.update_channel_link.assert_called_once_with(
        1, 20, None, "Связано · участие уже было"
    )


@pytest.mark.asyncio
async def test_incomplete_discussion_response_fails_closed_instead_of_using_source_post():
    from telethon.tl import types

    from tests.test_telegram_logic import Limiter, _MessageIterator

    source = types.PeerChannel(100)

    class Client:
        def is_connected(self):
            return True

        async def get_entity(self, channel_id):
            return SimpleNamespace(id=100)

        def iter_messages(self, entity, limit):
            return _MessageIterator(
                [SimpleNamespace(id=50, action=None, grouped_id=None)]
            )

        async def __call__(self, request):
            return SimpleNamespace(
                messages=[SimpleNamespace(id=50, peer_id=source)],
                chats=[SimpleNamespace(id=100)],
            )

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = None

    result = await service.get_latest_post_for_commenting(100)

    assert result.status == "discussion_missing"
    assert result.message.id == 50

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.telegram_service import TelegramService
from tests.test_composition_resilience import (
    _Linked,
    _Telegram,
    _comment_database,
    _handlers,
)


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

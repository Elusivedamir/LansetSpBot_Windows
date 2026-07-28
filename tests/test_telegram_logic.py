from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telethon import types, utils

from core.exceptions import DeferredTelegramError
from services.comment_service import CommentService
from services.telegram.posts import (
    EXPANDED_POST_SCAN_LIMIT,
    LATEST_POST_SCAN_LIMIT,
)
from services.telegram_service import TelegramService


class Limiter:
    async def acquire(self):
        return None


def test_membership_probe_api_is_removed():
    assert not hasattr(TelegramService, "is_member")


@pytest.mark.asyncio
async def test_transient_network_error_is_retried(monkeypatch):
    class Client:
        def is_connected(self):
            return True

        async def disconnect(self):
            return None

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    monkeypatch.setattr(
        service, "safe_sleep", lambda seconds: asyncio.sleep(0, result=True)
    )
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("temporary")
        return "ok"

    assert await service.execute(operation) == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_comment_uses_channel_comment_route():
    calls = []

    class Telegram:
        async def send_comment(
            self, channel_id, post_id, text, reply_to=None, linked_chat_id=None
        ):
            calls.append((channel_id, post_id, text, reply_to, linked_chat_id))
            return SimpleNamespace(id=44, sender_id=9, date=None)

    class Linked:
        async def get_linked_chat_id(self, channel_id):
            return 777

        async def check_access(self, linked_chat_id):
            return True

    service = CommentService(Telegram(), Linked(), db=None)
    result = await service.ensure_and_send_comment(
        linked_chat_id=None,
        post_message_id=55,
        text="hello",
        channel_id=123,
    )
    assert result.id == 44
    assert calls == [(123, 55, "hello", None, None)]


@pytest.mark.asyncio
async def test_mutating_send_is_not_retried_after_ambiguous_network_failure(
    monkeypatch,
):
    from core.exceptions import NonRetryableTelegramError

    class Client:
        def is_connected(self):
            return True

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise ConnectionError("response lost")

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.execute(operation, retry_network=False)
    assert raised.value.code == "delivery_result_unknown"
    assert calls == 1


class _MessageIterator:
    def __init__(self, messages):
        self._messages = list(messages)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        item = self._messages[self._index]
        self._index += 1
        return item


@pytest.mark.asyncio
async def test_strict_latest_post_does_not_fall_back_when_comments_disabled():
    from telethon.errors import ChatDiscussionUnallowedError

    calls = []

    class Client:
        def is_connected(self):
            return True

        async def get_entity(self, channel_id):
            return channel_id

        def iter_messages(self, entity, limit):
            return _MessageIterator(
                [
                    SimpleNamespace(id=20, action=None, grouped_id=None),
                    SimpleNamespace(id=19, action=None, grouped_id=None),
                ]
            )

        async def __call__(self, request):
            calls.append(request.msg_id)
            raise ChatDiscussionUnallowedError(request)

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = None

    result = await service.get_latest_post_for_commenting(123)
    assert result.status == "comments_disabled"
    assert result.message.id == 20
    assert calls == [20]


@pytest.mark.asyncio
async def test_deleted_latest_discussion_is_reported_without_older_fallback():
    from telethon.errors import MessageIdInvalidError

    calls = []

    class Client:
        def is_connected(self):
            return True

        async def get_entity(self, channel_id):
            return channel_id

        def iter_messages(self, entity, limit):
            return _MessageIterator(
                [
                    SimpleNamespace(id=50, action=None, grouped_id=None),
                    SimpleNamespace(id=49, action=None, grouped_id=None),
                ]
            )

        async def __call__(self, request):
            calls.append(request.msg_id)
            raise MessageIdInvalidError(request)

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = None

    result = await service.get_latest_post_for_commenting(123)
    assert result.status == "discussion_missing"
    assert result.message.id == 50
    assert calls == [50]


@pytest.mark.asyncio
async def test_latest_message_uses_one_discussion_request_only():
    calls = []

    class Client:
        def is_connected(self):
            return True

        async def get_entity(self, channel_id):
            return SimpleNamespace(id=123)

        def iter_messages(self, entity, limit):
            return _MessageIterator(
                [
                    SimpleNamespace(id=32, action=None, grouped_id=900),
                    SimpleNamespace(id=31, action=None, grouped_id=900),
                    SimpleNamespace(id=30, action=None, grouped_id=None),
                ]
            )

        async def __call__(self, request):
            from telethon.tl.types import PeerChannel

            calls.append(request.msg_id)
            return SimpleNamespace(
                messages=[
                    SimpleNamespace(id=request.msg_id, peer_id=PeerChannel(123)),
                    SimpleNamespace(id=700, peer_id=PeerChannel(456)),
                ],
                chats=[SimpleNamespace(id=123), SimpleNamespace(id=456)],
            )

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = None

    result = await service.get_latest_post_for_commenting(123)
    assert result.status == "ok"
    assert result.message.id == 31
    assert calls == [31]


@pytest.mark.asyncio
async def test_latest_post_expands_past_four_service_events():
    limits = []
    offsets = []

    class Client:
        def is_connected(self):
            return True

        async def get_entity(self, channel_id):
            return channel_id

        def iter_messages(self, entity, limit, offset_id=None):
            del entity
            limits.append(limit)
            offsets.append(offset_id)
            # The run of service events must exceed the first-pass window so
            # the saturated-batch branch is still the code path under test.
            messages = [
                SimpleNamespace(id=value, action=object(), grouped_id=None)
                for value in range(60, 25, -1)
            ]
            messages.append(SimpleNamespace(id=25, action=None, grouped_id=None))
            if offset_id is not None:
                messages = [item for item in messages if item.id < offset_id]
            return _MessageIterator(messages[:limit])

        async def __call__(self, request):
            from telethon.tl.types import PeerChannel

            return SimpleNamespace(
                messages=[
                    SimpleNamespace(id=request.msg_id, peer_id=PeerChannel(123)),
                    SimpleNamespace(id=700, peer_id=PeerChannel(456)),
                ],
                chats=[SimpleNamespace(id=123), SimpleNamespace(id=456)],
            )

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = None

    result = await service.get_latest_post_for_commenting(123)

    assert result.status == "ok"
    assert result.message.id == 25
    assert limits == [
        LATEST_POST_SCAN_LIMIT,
        EXPANDED_POST_SCAN_LIMIT - LATEST_POST_SCAN_LIMIT,
    ]
    assert offsets == [None, 31]


@pytest.mark.asyncio
async def test_latest_album_expands_before_choosing_durable_post_id():
    limits = []
    offsets = []

    class Client:
        def is_connected(self):
            return True

        async def get_entity(self, channel_id):
            return channel_id

        def iter_messages(self, entity, limit, offset_id=None):
            del entity
            limits.append(limit)
            offsets.append(offset_id)
            # Synthetically wider than any real Telegram album so the
            # truncated-prefix branch stays covered after the window change.
            messages = [
                SimpleNamespace(id=value, action=None, grouped_id=900)
                for value in range(60, 30, -1)
            ]
            messages.append(SimpleNamespace(id=30, action=None, grouped_id=None))
            if offset_id is not None:
                messages = [item for item in messages if item.id < offset_id]
            return _MessageIterator(messages[:limit])

        async def __call__(self, request):
            from telethon.tl.types import PeerChannel

            return SimpleNamespace(
                messages=[
                    SimpleNamespace(id=request.msg_id, peer_id=PeerChannel(123)),
                    SimpleNamespace(id=700, peer_id=PeerChannel(456)),
                ],
                chats=[SimpleNamespace(id=123), SimpleNamespace(id=456)],
            )

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = None

    result = await service.get_latest_post_for_commenting(123)

    assert result.status == "ok"
    assert result.message.id == 31
    assert limits == [
        LATEST_POST_SCAN_LIMIT,
        EXPANDED_POST_SCAN_LIMIT - LATEST_POST_SCAN_LIMIT,
    ]
    assert offsets == [None, 31]


@pytest.mark.asyncio
async def test_full_size_album_resolves_in_one_history_request():
    """A ten-item album is the largest Telegram allows and must not expand.

    The previous four-message window saturated on any album of four or more
    media and paid a second full history request for the same answer.
    """

    limits = []

    class Client:
        def is_connected(self):
            return True

        async def get_entity(self, channel_id):
            return channel_id

        def iter_messages(self, entity, limit):
            del entity
            limits.append(limit)
            messages = [
                SimpleNamespace(id=value, action=None, grouped_id=900)
                for value in range(40, 30, -1)
            ]
            messages.append(SimpleNamespace(id=30, action=None, grouped_id=None))
            return _MessageIterator(messages[:limit])

        async def __call__(self, request):
            from telethon.tl.types import PeerChannel

            return SimpleNamespace(
                messages=[
                    SimpleNamespace(id=request.msg_id, peer_id=PeerChannel(123)),
                    SimpleNamespace(id=700, peer_id=PeerChannel(456)),
                ],
                chats=[SimpleNamespace(id=123), SimpleNamespace(id=456)],
            )

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = None

    result = await service.get_latest_post_for_commenting(123)

    assert result.status == "ok"
    # The durable post id is the album's lowest message id, unchanged.
    assert result.message.id == 31
    assert limits == [LATEST_POST_SCAN_LIMIT]


@pytest.mark.asyncio
async def test_flood_wait_is_reported_to_runtime_status(monkeypatch):
    from telethon.errors import FloodWaitError

    class Client:
        def is_connected(self):
            return True

    statuses = []
    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = statuses.append
    monkeypatch.setattr("services.telegram_service.random.randint", lambda _a, _b: 20)
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise FloodWaitError(None, capture=2)

    with pytest.raises(DeferredTelegramError) as raised:
        await service.execute(operation)
    assert raised.value.code == "flood_wait_deferred"
    assert raised.value.retry_after == 180
    assert calls == 1
    assert any("Ограничение Telegram" in item for item in statuses)


@pytest.mark.asyncio
async def test_comment_engine_requires_channel_and_uses_true_comment_route():
    from services.comment_engine import CommentEngine

    calls = []

    class CommentSender:
        async def send_comment(self, **kwargs):
            calls.append(kwargs)
            return "sent"

    engine = CommentEngine()
    result = await engine.process(
        {
            "channel_id": 123,
            "linked_chat_id": 777,
            "post_id": 55,
            "reply_to": None,
        },
        CommentSender(),
        ["hello"],
    )

    assert result == "sent"
    assert calls == [
        {
            "channel_id": 123,
            "linked_chat_id": 777,
            "post_message_id": 55,
            "text": "hello",
            "reply_to": None,
        }
    ]


@pytest.mark.asyncio
async def test_comment_service_rejects_missing_channel_id_before_sending():
    from core.exceptions import NonRetryableTelegramError

    class Telegram:
        async def send_comment(self, *args, **kwargs):
            raise AssertionError("Telegram send must not be called")

    service = CommentService(Telegram(), linked_chat_service=None, db=None)
    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.ensure_and_send_comment(
            channel_id=None,
            linked_chat_id=777,
            post_message_id=55,
            text="hello",
        )
    assert raised.value.code == "channel_id_missing"


@pytest.mark.asyncio
async def test_repeated_slow_mode_wait_keeps_waiting_until_operation_succeeds(
    monkeypatch,
):
    from telethon.errors import SlowModeWaitError

    class Client:
        def is_connected(self):
            return True

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = None
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise SlowModeWaitError(None, capture=1)

    monkeypatch.setattr("services.telegram_service.random.randint", lambda _a, _b: 20)

    with pytest.raises(DeferredTelegramError) as raised:
        await service.execute(operation)
    assert raised.value.code == "slow_mode_wait_deferred"
    assert raised.value.retry_after == 21
    assert calls == 1


def test_missing_comment_text_mentions_saved_template_not_enabled_template():
    from core.exceptions import NonRetryableTelegramError

    class Database:
        def get_templates(self):
            return []

    service = CommentService(telegram=object(), linked_chat_service=None, db=Database())
    with pytest.raises(NonRetryableTelegramError) as raised:
        service._select_text("")
    assert "saved template" in str(raised.value)
    assert "enabled template" not in str(raised.value)


def test_saved_template_is_trimmed_before_sending():
    class Database:
        def get_templates(self):
            return [{"text_1": "  hello from template\n", "text_2": "   "}]

    service = CommentService(telegram=object(), linked_chat_service=None, db=Database())
    assert service._select_text("") == "hello from template"


@pytest.mark.asyncio
async def test_exhausted_read_network_retries_use_network_unavailable_code(monkeypatch):
    from core.exceptions import NonRetryableTelegramError

    class Client:
        def is_connected(self):
            return True

        async def disconnect(self):
            return None

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = None
    monkeypatch.setattr(
        service, "safe_sleep", lambda seconds: asyncio.sleep(0, result=True)
    )

    async def operation():
        raise ConnectionError("offline")

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.execute(operation, retry_network=True)
    assert raised.value.code == "network_unavailable"


@pytest.mark.asyncio
async def test_direct_comment_route_uses_resolved_discussion_chat_and_root():
    calls = []

    class Service(TelegramService):
        async def send_message(self, chat_id, text, reply_to_message_id=None):
            calls.append((chat_id, text, reply_to_message_id))
            return "sent"

    service = object.__new__(Service)
    result = await service.send_comment(
        123,
        55,
        "hello",
        reply_to=777,
        linked_chat_id=888,
    )

    assert result == "sent"
    assert calls == [(utils.get_peer_id(types.PeerChannel(888)), "hello", 777)]


@pytest.mark.asyncio
async def test_discussion_resolver_selects_message_from_linked_chat():
    from telethon import utils
    from telethon.tl.types import PeerChannel

    entity_requests = []

    class Client:
        def is_connected(self):
            return True

        async def get_entity(self, channel_id):
            entity_requests.append(channel_id)
            return SimpleNamespace(id=100)

        def iter_messages(self, entity, limit):
            return _MessageIterator(
                [SimpleNamespace(id=50, action=None, grouped_id=None)]
            )

        async def __call__(self, request):
            return SimpleNamespace(
                messages=[
                    SimpleNamespace(id=50, peer_id=PeerChannel(100)),
                    SimpleNamespace(id=777, peer_id=PeerChannel(200)),
                ],
                chats=[SimpleNamespace(id=100), SimpleNamespace(id=200)],
            )

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = None

    result = await service.get_latest_post_for_commenting(100)

    assert result.status == "ok"
    assert entity_requests == []
    assert result.discussion_chat_id == utils.get_peer_id(PeerChannel(200))
    assert result.discussion_message_id == 777


@pytest.mark.asyncio
async def test_invalid_direct_discussion_root_falls_back_to_comment_to():
    from core.exceptions import NonRetryableTelegramError

    calls = []

    class Telegram:
        async def send_comment(
            self, channel_id, post_id, text, reply_to=None, linked_chat_id=None
        ):
            calls.append((channel_id, post_id, text, reply_to, linked_chat_id))
            if reply_to is not None:
                raise NonRetryableTelegramError(
                    "root disappeared", code="message_id_invalid"
                )
            return SimpleNamespace(id=44, sender_id=9, date=None)

    service = CommentService(Telegram(), linked_chat_service=None, db=None)
    result = await service.ensure_and_send_comment(
        channel_id=123,
        linked_chat_id=777,
        post_message_id=55,
        text="hello",
        reply_to=888,
        membership_ready=True,
    )

    assert result.id == 44
    assert calls == [
        (123, 55, "hello", 888, 777),
        (123, 55, "hello", None, None),
    ]

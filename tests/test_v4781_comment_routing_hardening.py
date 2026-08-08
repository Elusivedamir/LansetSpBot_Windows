from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import utils
from telethon.tl import types

from core.exceptions import NonRetryableTelegramError
from services.comment_service import CommentService
from services.telegram_service import TelegramService
from storage.database import Database
from tests.test_composition_resilience import (
    _Telegram,
    _comment_database,
    _handlers,
)
from tests.test_telegram_logic import Limiter, _MessageIterator


@pytest.mark.asyncio
async def test_discussion_resolver_uses_oldest_matching_discussion_message_as_root():
    source = types.PeerChannel(100)
    discussion = types.PeerChannel(200)

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
                # Telegram returns newest first. 900 and 800 are user comments;
                # 700 is the auto-forwarded root of the channel post.
                messages=[
                    SimpleNamespace(id=900, peer_id=discussion),
                    SimpleNamespace(id=800, peer_id=discussion),
                    SimpleNamespace(id=700, peer_id=discussion),
                    SimpleNamespace(id=50, peer_id=source),
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
    assert result.discussion_chat_id == utils.get_peer_id(discussion)
    assert result.discussion_message_id == 700


@pytest.mark.asyncio
async def test_iter_channels_marks_has_link_group_without_source_channel():
    linked_group = types.Channel(
        id=222,
        title="Discussion only",
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=False,
        megagroup=True,
        has_link=True,
        access_hash=2,
        username="discussion_only",
    )
    dialogs = [SimpleNamespace(entity=linked_group, is_channel=True, is_group=True)]

    class Rows:
        def __init__(self):
            self._rows = iter(dialogs)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._rows)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    service = object.__new__(TelegramService)
    service.ensure_connected = AsyncMock()
    service.client = SimpleNamespace(iter_dialogs=Rows)
    service._iter_with_timeout = lambda iterator: iterator

    rows = [row async for row in service.iter_channels()]

    assert len(rows) == 1
    assert rows[0]["comment_mode"] == "linked_discussion"
    assert "только комментарии" in rows[0]["link_status"]


def test_linked_group_hint_survives_classification_without_source_channel(tmp_path):
    db = Database(tmp_path / "linked-hint.db")
    group_id = -1000000000222
    db.upsert_channels_batch(
        [
            {
                "channel_id": group_id,
                "title": "Discussion only",
                "target_kind": "group",
                "comment_mode": "linked_discussion",
                "linked_chat_id": group_id,
                "linked_chat_title": "Discussion only",
                "link_status": "Связанное обсуждение · только комментарии к постам",
            }
        ]
    )

    result = db.refresh_group_comment_modes()
    row = db.get_channel_by_id(group_id)

    assert result == {"linked_discussion": 1, "direct_group": 0}
    assert row["comment_mode"] == "linked_discussion"
    assert db.get_channels_for_commenting(10) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        "user_banned",
        "entity_bounds_invalid",
        "chat_restricted",
        "privacy_restricted",
    ],
)
async def test_send_rejections_happen_only_after_prepared_membership(
    monkeypatch, error_code
):
    db = _comment_database()
    telegram = _Telegram()
    telegram.member_results = [True]
    telegram.latest_post = SimpleNamespace(
        status="ok",
        message=SimpleNamespace(id=901),
        discussion_chat_id=20,
        discussion_message_id=777,
    )
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, telegram)
    comments.errors = [
        NonRetryableTelegramError("rejected", code=error_code),
    ]

    await handlers["auto_comment_slot"](
        {"id": 201, "payload": {"campaign_id": 1, "slot_id": 201}}
    )

    assert telegram.join_calls == []
    db.record_join_event.assert_not_called()
    assert comments.sent == []
    expected_status = "failed" if error_code == "user_banned" else "skipped"
    assert db.finish_comment_slot.call_args.kwargs["status"] == expected_status
    assert error_code in db.finish_comment_slot.call_args.kwargs["result"]


@pytest.mark.asyncio
async def test_oversized_comment_is_rejected_locally_before_join(monkeypatch):
    db = _comment_database(comments=["x" * 4097], last_comment_text="")
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
        {"id": 202, "payload": {"campaign_id": 1, "slot_id": 202}}
    )

    assert telegram.join_calls == []
    assert comments.sent == []
    assert db.finish_comment_slot.call_args.kwargs["status"] == "skipped"
    assert "message_too_long" in db.finish_comment_slot.call_args.kwargs["result"]


@pytest.mark.asyncio
async def test_comment_service_rejects_oversized_text_before_reservation_or_send():
    telegram = SimpleNamespace(send_comment=AsyncMock())
    db = SimpleNamespace(reserve_comment_delivery=AsyncMock())
    service = CommentService(telegram=telegram, linked_chat_service=None, db=db)

    with pytest.raises(NonRetryableTelegramError) as error:
        await service.ensure_and_send_comment(
            channel_id=10,
            post_message_id=20,
            linked_chat_id=30,
            text="x" * 4097,
            membership_ready=True,
        )

    assert error.value.code == "message_too_long"
    telegram.send_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_post_does_not_change_cached_discussion_link(monkeypatch):
    db = _comment_database()
    telegram = _Telegram()
    telegram.latest_post = SimpleNamespace(
        status="comments_disabled",
        message=SimpleNamespace(id=901),
        discussion_chat_id=999,
        discussion_message_id=None,
    )
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 202, "payload": {"campaign_id": 1, "slot_id": 202}}
    )

    db.update_channel_link.assert_not_called()
    assert comments.sent == []
    assert "комментарии отключены" in db.finish_comment_slot.call_args.kwargs["result"]


@pytest.mark.asyncio
async def test_iter_channels_excludes_read_only_group_but_keeps_admin_override():
    read_only_rights = types.ChatBannedRights(
        until_date=None, send_messages=True, send_plain=True
    )
    read_only = types.Channel(
        id=301,
        title="Read only",
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=False,
        megagroup=True,
        access_hash=3,
        default_banned_rights=read_only_rights,
    )
    admin_rights = types.ChatAdminRights(change_info=True)
    admin_group = types.Channel(
        id=302,
        title="Admin can write",
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=False,
        megagroup=True,
        access_hash=4,
        default_banned_rights=read_only_rights,
        admin_rights=admin_rights,
    )
    dialogs = [
        SimpleNamespace(entity=read_only, is_channel=True, is_group=True),
        SimpleNamespace(entity=admin_group, is_channel=True, is_group=True),
    ]

    class Rows:
        def __init__(self):
            self._rows = iter(dialogs)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._rows)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    service = object.__new__(TelegramService)
    service.ensure_connected = AsyncMock()
    service.client = SimpleNamespace(iter_dialogs=Rows)
    service._iter_with_timeout = lambda iterator: iterator

    rows = [row async for row in service.iter_channels()]

    assert [row["id"] for row in rows] == [utils.get_peer_id(admin_group)]


def test_group_write_preflight_excludes_deactivated_and_migrated_basic_groups():
    deactivated = types.Chat(
        id=401,
        title="Old",
        photo=types.ChatPhotoEmpty(),
        participants_count=0,
        date=None,
        version=1,
        deactivated=True,
    )
    migrated = types.Chat(
        id=402,
        title="Migrated",
        photo=types.ChatPhotoEmpty(),
        participants_count=0,
        date=None,
        version=1,
        migrated_to=types.InputChannel(999, 1),
    )

    assert TelegramService._group_allows_plain_text(deactivated) is False
    assert TelegramService._group_allows_plain_text(migrated) is False


def test_unverified_group_stays_pending_and_is_not_a_direct_target(tmp_path):
    db = Database(tmp_path / "pending-safe.db")
    group_id = -1000000000555
    db.upsert_channels_batch(
        [
            {
                "channel_id": group_id,
                "title": "Unverified",
                "target_kind": "group",
                "comment_mode": "pending",
                "linked_chat_id": group_id,
                "link_status": "Группа · ожидает проверки связей",
            }
        ]
    )

    result = db.refresh_group_comment_modes()

    assert result == {"linked_discussion": 0, "direct_group": 0}
    assert db.get_channel_by_id(group_id)["comment_mode"] == "pending"
    assert db.get_channels_for_commenting(10) == []


def test_explicit_no_link_promotes_ordinary_group_to_direct_target(tmp_path):
    db = Database(tmp_path / "direct-confirmed.db")
    group_id = -1000000000666
    db.upsert_channels_batch(
        [
            {
                "channel_id": group_id,
                "title": "Standalone",
                "target_kind": "group",
                "comment_mode": "pending",
                "linked_chat_id": group_id,
            }
        ]
    )
    db.update_group_link_classification(
        group_id, is_linked=False, status="Группа · связь не обнаружена"
    )

    result = db.refresh_group_comment_modes()

    assert result == {"linked_discussion": 0, "direct_group": 1}
    assert db.get_channel_by_id(group_id)["comment_mode"] == "direct_group"
    assert {row["channel_id"] for row in db.get_channels_for_commenting(10)} == {
        group_id
    }


@pytest.mark.asyncio
async def test_invalid_root_fails_closed_without_dynamic_comment_reroute():
    class Telegram:
        def __init__(self):
            self.calls = []

        async def send_comment(
            self, channel_id, post_id, text, reply_to=None, linked_chat_id=None,
            dispatch_barrier=None,
        ):
            self.calls.append((reply_to, linked_chat_id))
            raise NonRetryableTelegramError("stale root", code="message_id_invalid")

    class DB:
        def __init__(self):
            self.finalized = None
            self.released = False

        def reserve_comment_delivery(self, *args, **kwargs):
            return True

        def release_comment_delivery(self, *args, **kwargs):
            self.released = True

        def finalize_comment_delivery(self, data):
            self.finalized = data

    telegram = Telegram()
    db = DB()
    service = CommentService(telegram=telegram, linked_chat_service=None, db=db)

    with pytest.raises(NonRetryableTelegramError) as error:
        await service.ensure_and_send_comment(
            channel_id=10,
            post_message_id=20,
            linked_chat_id=30,
            text="hello",
            reply_to=777,
            membership_ready=True,
        )

    assert error.value.code == "message_id_invalid"
    assert telegram.calls == [(777, 30)]
    assert db.released is True
    assert db.finalized is None


def test_failed_group_rescan_preserves_confirmed_direct_target(tmp_path):
    db = Database(tmp_path / "rescan-failure.db")
    group_id = -1000000000777
    db.upsert_channels_batch(
        [
            {
                "channel_id": group_id,
                "title": "Former direct",
                "target_kind": "group",
                "comment_mode": "pending",
                "linked_chat_id": group_id,
            }
        ]
    )
    db.update_group_link_classification(
        group_id, is_linked=False, status="Группа · связь не обнаружена"
    )
    db.refresh_group_comment_modes()
    assert db.get_channel_by_id(group_id)["comment_mode"] == "direct_group"

    db.update_group_link_classification(
        group_id, is_linked=None, status="Группа · связь не проверена: timeout"
    )

    assert db.get_channel_by_id(group_id)["comment_mode"] == "direct_group"
    assert {row["channel_id"] for row in db.get_channels_for_commenting(10)} == {
        group_id
    }


@pytest.mark.parametrize(
    ("error_type", "expected_code"),
    [
        ("UserNotParticipantError", "join_required"),
        ("ChatWriteForbiddenError", "chat_write_forbidden"),
        ("UserBannedInChannelError", "user_banned"),
        ("MessageTooLongError", "message_too_long"),
    ],
)
def test_telegram_send_errors_have_precise_codes(error_type, expected_code):
    from telethon import errors

    from services.telegram_error_translation import translate_permanent_send_error

    translated = translate_permanent_send_error(getattr(errors, error_type)(None))

    assert translated.code == expected_code
    assert translated.details["rpc_error"] == error_type


@pytest.mark.asyncio
async def test_link_scan_confirms_basic_group_as_direct_without_channel_request(
    monkeypatch,
):
    db = MagicMock()
    group_id = -12345
    db.get_channels.return_value = [
        {
            "channel_id": group_id,
            "title": "Basic group",
            "target_kind": "group",
            "comment_mode": "pending",
        }
    ]
    db.refresh_group_comment_modes.return_value = {
        "linked_discussion": 0,
        "direct_group": 1,
    }
    telegram = _Telegram()
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["link_channels"]({"id": 301, "payload": {}})

    db.update_group_link_classification.assert_called_once_with(
        group_id,
        is_linked=False,
        status="Группа · локально определена как обычная",
    )


@pytest.mark.asyncio
async def test_link_scan_keeps_unverified_supergroup_out_of_direct_targets(
    monkeypatch,
):
    db = MagicMock()
    group_id = -1000000000888
    db.get_channels.return_value = [
        {
            "channel_id": group_id,
            "title": "Unavailable group",
            "target_kind": "group",
            "comment_mode": "pending",
        }
    ]
    db.refresh_group_comment_modes.return_value = {
        "linked_discussion": 0,
        "direct_group": 0,
    }
    telegram = _Telegram()
    telegram.get_linked_chat = AsyncMock(
        side_effect=NonRetryableTelegramError("private", code="channel_private")
    )
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["link_channels"]({"id": 302, "payload": {}})

    db.update_group_link_classification.assert_called_once_with(
        group_id,
        is_linked=False,
        status="Группа · локально определена как обычная",
    )
    telegram.get_linked_chat.assert_not_awaited()
    db.refresh_group_comment_modes.assert_called_once_with()


@pytest.mark.asyncio
async def test_link_scan_propagates_group_telegram_operation_error(monkeypatch):
    from core.exceptions import TelegramOperationError

    db = MagicMock()
    group_id = -1000000000999
    db.get_channels.return_value = [
        {
            "channel_id": group_id,
            "title": "Broken group",
            "target_kind": "group",
            "comment_mode": "pending",
        }
    ]
    telegram = _Telegram()
    telegram.get_linked_chat = AsyncMock(
        side_effect=TelegramOperationError("temporary RPC failure")
    )
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["link_channels"]({"id": 303, "payload": {}})

    telegram.get_linked_chat.assert_not_awaited()
    db.update_group_link_classification.assert_called_once_with(
        group_id,
        is_linked=False,
        status="Группа · локально определена как обычная",
    )

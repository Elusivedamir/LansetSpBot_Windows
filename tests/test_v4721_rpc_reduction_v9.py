from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from telethon.tl.types import Channel, ChatPhotoEmpty, PeerChannel

import services.telegram.transport as transport_module
from core.exceptions import DeferredTelegramError
from services.telegram.posts import LATEST_POST_SCAN_LIMIT
from services.telegram_service import TelegramService
from tests.test_composition_resilience import (
    _Comments,
    _Linked,
    _Telegram,
    _Worker,
    _comment_database,
    _handlers,
)
from tests.test_telegram_logic import Limiter, _MessageIterator
from workers.comment_slot.handler import create_comment_slot_handler
from workers.handlers.join_slot import create_join_slot_handler
from workers.handlers.link_channels import create_link_channels_handler


def _direct_comment_handler(db, telegram, comments):
    def as_int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return int(default)

    return create_comment_slot_handler(
        as_int=as_int,
        queue_worker=_Worker(db),
        config=SimpleNamespace(),
        worker_db=db,
        telegram=telegram,
        comments=comments,
        set_runtime=lambda *_args, **_kwargs: None,
    )


@pytest.mark.asyncio
async def test_authorization_identity_probe_runs_every_fifteen_minutes(monkeypatch):
    class Client:
        def __init__(self) -> None:
            self.get_me_calls = 0

        def is_connected(self) -> bool:
            return True

        async def get_me(self):
            self.get_me_calls += 1
            return SimpleNamespace(id=77)

    service = object.__new__(TelegramService)
    service.client = Client()
    service.settings = SimpleNamespace(expected_account_id=77)
    service._connected = True
    service._last_authorization_check = 1.0
    service._authorized_user = SimpleNamespace(id=77)

    monkeypatch.setattr(transport_module.time, "monotonic", lambda: 900.0)
    await service.ensure_connected()
    assert service.client.get_me_calls == 0

    monkeypatch.setattr(transport_module.time, "monotonic", lambda: 902.0)
    await service.ensure_connected()
    assert service.client.get_me_calls == 1


@pytest.mark.asyncio
async def test_health_identity_reuses_recent_authorization_probe():
    class Client:
        def __init__(self) -> None:
            self.get_me_calls = 0

        def is_connected(self) -> bool:
            return True

        async def get_me(self):
            self.get_me_calls += 1
            return SimpleNamespace(id=77)

    cached_identity = SimpleNamespace(id=77)
    service = object.__new__(TelegramService)
    service.client = Client()
    service.settings = SimpleNamespace(expected_account_id=77)
    service._connected = True
    service._last_authorization_check = transport_module.time.monotonic()
    service._authorized_user = cached_identity

    identity = await service.get_connected_identity()

    assert identity is cached_identity
    assert service.client.get_me_calls == 0


@pytest.mark.asyncio
async def test_cached_duplicate_skips_post_and_discussion_reads():
    db = _comment_database(account_id=77)
    db.get_campaign_comment_settings.return_value = {}
    db.get_comment_slot_route.return_value = {
        "channel_id": 10,
        "post_id": 901,
        "linked_chat_id": 20,
        "discussion_message_id": 777,
    }
    db.get_channel_by_id.return_value = {
        "channel_id": 10,
        "linked_chat_id": 20,
        "title": "Channel",
    }
    db.has_commented.return_value = True
    telegram = _Telegram()
    comments = _Comments()
    handler = _direct_comment_handler(db, telegram, comments)

    await handler(
        {
            "id": 101,
            "payload": {
                "account_id": 77,
                "campaign_id": 1,
                "slot_id": 101,
            },
        }
    )

    assert telegram.latest_calls == 0
    assert comments.sent == []
    assert "уже комментировали" in db.finish_comment_slot.call_args.kwargs["result"]


@pytest.mark.asyncio
async def test_quiet_hours_defer_before_telegram_or_openai_work():
    class QuietSchedule:
        calls = 0

        def require_active(self):
            self.calls += 1
            raise DeferredTelegramError(
                "Тихие часы",
                code="local_quiet_hours",
                retry_after=90,
            )

    db = _comment_database(account_id=77)
    db.get_campaign_comment_settings.return_value = {}
    db.get_comment_slot_route.return_value = None
    telegram = _Telegram()
    comments = _Comments()
    comments.activity_schedule = QuietSchedule()
    handler = _direct_comment_handler(db, telegram, comments)

    await handler(
        {
            "id": 102,
            "payload": {
                "account_id": 77,
                "campaign_id": 1,
                "slot_id": 102,
            },
        }
    )

    assert comments.activity_schedule.calls == 1
    assert telegram.latest_calls == 0
    assert comments.sent == []
    db.defer_comment_slot.assert_called_once()


@pytest.mark.asyncio
async def test_links_use_durable_join_guard_before_join_rpc():
    db = MagicMock()
    db.get_setting.return_value = 77
    db.get_channels.return_value = [
        {"channel_id": 10, "title": "Channel", "target_kind": "channel"}
    ]
    db.update_task_checkpoint.return_value = True
    db.get_join_guard.return_value = {
        "allowed": False,
        "wait_seconds": 60,
        "effective_count": 1,
    }
    db.postpone_running_task_for_account_cooldown.return_value = True
    telegram = _Telegram()
    linked = _Linked()
    linked.links = {10: 20}
    worker = _Worker(db)

    class Container:
        config = SimpleNamespace(
            max_joins_per_hour=40,
            min_join_interval_seconds=45,
            link_join_delay_min_seconds=15,
            link_join_delay_max_seconds=25,
            link_check_delay_min_seconds=0,
            link_check_delay_max_seconds=0,
        )
        queue_worker = worker

        @staticmethod
        def _as_int(value, default):
            try:
                return int(value)
            except (TypeError, ValueError, OverflowError):
                return int(default)

    handler = create_link_channels_handler(
        self=Container(),
        telegram=telegram,
        worker_db=db,
        linked=linked,
        set_runtime=lambda *_args, **_kwargs: None,
        publish_activity=lambda *_args, **_kwargs: None,
    )

    await handler({"id": 103, "payload": {"account_id": 77}})

    db.get_join_guard.assert_called_once_with(
        max_joins=40,
        min_interval_seconds=120.0,
        window_seconds=3600,
        account_id=77,
    )
    db.postpone_running_task_for_account_cooldown.assert_called_once()
    assert telegram.join_calls == []
    checkpoint_payload = db.update_task_checkpoint.call_args_list[-1].args[1]
    assert checkpoint_payload["_link_checkpoint"]["channel_index"] == 0


@pytest.mark.asyncio
async def test_comment_flood_wait_persists_account_wide_auto_resume():
    db = _comment_database(account_id=77)
    db.get_campaign_comment_settings.return_value = {}
    db.get_comment_slot_route.return_value = None
    telegram = _Telegram()
    telegram.latest_error = DeferredTelegramError(
        "Telegram FloodWait",
        code="flood_wait_deferred",
        retry_after=240,
    )
    handler = _direct_comment_handler(db, telegram, _Comments())

    await handler(
        {
            "id": 104,
            "payload": {
                "account_id": 77,
                "campaign_id": 1,
                "slot_id": 104,
            },
        }
    )

    cooldown = db.set_account_rpc_cooldown.call_args.kwargs
    assert cooldown["account_id"] == 77
    assert cooldown["code"] == "flood_wait_deferred"
    assert cooldown["source_task_id"] == 104
    assert cooldown["wait_seconds"] == 240
    db.defer_comment_slot_and_set_network_wait.assert_called_once()


@pytest.mark.asyncio
async def test_join_flood_wait_persists_account_wide_auto_resume():
    db = MagicMock()
    db.get_join_slot_context.return_value = {
        "campaign_status": "running",
        "status": "queued",
        "title": "Saved",
        "account_id": 77,
        "saved_dialog_id": 6,
        "peer_id": 20,
        "username": "saved",
        "invite_link": None,
        "max_per_hour": 40,
    }
    db.mark_join_slot_running.return_value = True
    db.get_join_guard.return_value = {"allowed": True, "wait_seconds": 0}
    db.defer_join_slot_and_set_network_wait.return_value = True
    telegram = _Telegram()
    telegram.join_error = DeferredTelegramError(
        "Telegram FloodWait",
        code="flood_wait_deferred",
        retry_after=240,
    )
    handler = create_join_slot_handler(
        as_int=lambda value, default: int(value or default),
        queue_worker=_Worker(db),
        config=SimpleNamespace(min_join_interval_seconds=45),
        worker_db=db,
        telegram=telegram,
        set_runtime=lambda *_args, **_kwargs: None,
    )

    await handler(
        {
            "id": 105,
            "payload": {
                "account_id": 77,
                "campaign_id": 1,
                "slot_id": 105,
            },
        }
    )

    cooldown = db.set_account_rpc_cooldown.call_args.kwargs
    assert cooldown["account_id"] == 77
    assert cooldown["code"] == "flood_wait_deferred"
    assert cooldown["source_task_id"] == 105
    assert cooldown["wait_seconds"] == 240
    db.defer_join_slot_and_set_network_wait.assert_called_once()


@pytest.mark.asyncio
async def test_dialog_snapshot_projects_both_tables_from_one_iter_dialogs_pass():
    entity = Channel(
        id=123,
        title="Channel",
        photo=ChatPhotoEmpty(),
        date=None,
        broadcast=True,
        megagroup=False,
        username="channel",
    )
    dialog = SimpleNamespace(entity=entity, is_channel=True, is_group=False)

    class Client:
        def __init__(self) -> None:
            self.iter_dialogs_calls = 0

        def iter_dialogs(self):
            self.iter_dialogs_calls += 1
            return _MessageIterator([dialog])

    service = object.__new__(TelegramService)
    service.client = Client()
    service._connected = True

    async def ensure_connected() -> None:
        return None

    service.ensure_connected = ensure_connected
    rows = [row async for row in service.iter_dialog_snapshot()]

    assert service.client.iter_dialogs_calls == 1
    assert rows[0]["work_target"]["id"] == 123
    assert rows[0]["saved_dialog"]["peer_id"] == -1000000000123


@pytest.mark.asyncio
async def test_unified_sync_handler_never_calls_legacy_second_dialog_pass(monkeypatch):
    class Telegram(_Telegram):
        def __init__(self) -> None:
            super().__init__()
            self.snapshot_calls = 0

        async def iter_dialog_snapshot(self):
            self.snapshot_calls += 1
            yield {
                "work_target": {
                    "id": 10,
                    "title": "Channel",
                    "username": "channel",
                    "target_kind": "channel",
                    "comment_mode": "channel_post",
                },
                "saved_dialog": {
                    "peer_id": -1000000000010,
                    "title": "Channel",
                    "username": "channel",
                    "kind": "channel",
                    "invite_link": None,
                },
            }

        async def iter_channels(self):  # pragma: no cover - must never run
            raise AssertionError("legacy channel pass must not run")
            yield

        async def iter_saved_dialogs(self):  # pragma: no cover - must never run
            raise AssertionError("legacy saved-dialog pass must not run")
            yield

    db = MagicMock()
    db.get_setting.side_effect = lambda key, default=None: {
        "telegram.account_id": 77,
        "telegram.phone": "+100",
    }.get(key, default)
    db.upsert_saved_dialogs_batch.return_value = [-1000000000010]
    telegram = Telegram()
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["sync_channels"]({"id": 1, "payload": {"account_id": 77}})

    assert telegram.snapshot_calls == 1
    db.upsert_channels_batch.assert_called_once()
    db.upsert_saved_dialogs_batch.assert_called_once()


@pytest.mark.asyncio
async def test_latest_post_fetches_one_history_page_and_one_discussion_request():
    limits: list[int] = []
    discussion_calls: list[int] = []

    class Client:
        def is_connected(self) -> bool:
            return True

        async def get_entity(self, _channel_id):
            return SimpleNamespace(id=123)

        def iter_messages(self, _entity, limit):
            limits.append(int(limit))
            return _MessageIterator(
                [
                    SimpleNamespace(id=50, action=None, grouped_id=900),
                    SimpleNamespace(id=49, action=None, grouped_id=900),
                ]
            )

        async def __call__(self, request):
            discussion_calls.append(int(request.msg_id))
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
    assert limits == [LATEST_POST_SCAN_LIMIT]
    assert discussion_calls == [49]

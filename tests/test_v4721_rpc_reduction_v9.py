from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from telethon.tl.types import Channel, ChatPhotoEmpty, PeerChannel

import services.telegram.transport as transport_module
from services.telegram.posts import LATEST_POST_SCAN_LIMIT
from services.telegram_service import TelegramService
from tests.test_composition_resilience import _Telegram, _handlers
from tests.test_telegram_logic import Limiter, _MessageIterator


@pytest.mark.asyncio
async def test_authorization_identity_probe_runs_every_five_minutes(monkeypatch):
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

    monkeypatch.setattr(transport_module.time, "monotonic", lambda: 300.0)
    await service.ensure_connected()
    assert service.client.get_me_calls == 0

    monkeypatch.setattr(transport_module.time, "monotonic", lambda: 302.0)
    await service.ensure_connected()
    assert service.client.get_me_calls == 1


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

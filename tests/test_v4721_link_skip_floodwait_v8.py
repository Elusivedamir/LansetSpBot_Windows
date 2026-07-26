from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from core.exceptions import NonRetryableTelegramError
from services.telegram_service import TelegramService
from tests.test_composition_resilience import _Linked, _Telegram, _handlers


class _TrackingLinked(_Linked):
    def __init__(self) -> None:
        super().__init__()
        self.access_calls: list[int] = []

    async def check_access(self, linked_chat_id):
        self.access_calls.append(int(linked_chat_id))
        return False


class _JoinOnceTelegram(_Telegram):
    def __init__(self) -> None:
        super().__init__()
        self.join_once_calls: list[int] = []

    async def join_without_confirmation(self, chat_id) -> bool:
        self.join_once_calls.append(int(chat_id))
        return False

    async def join(self, chat_id) -> bool:  # pragma: no cover - must never run
        raise AssertionError("link_channels must use join_without_confirmation")


@pytest.mark.asyncio
async def test_link_join_has_no_second_membership_check(monkeypatch):
    database = MagicMock()
    database.get_setting.return_value = 77
    database.get_channels.return_value = [
        {"channel_id": 1, "title": "One", "target_kind": "channel"}
    ]
    linked = _TrackingLinked()
    linked.links = {1: 101}
    telegram = _JoinOnceTelegram()
    telegram.chat_titles = {101: "Discussion"}
    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch, database, telegram, linked=linked
    )

    await handlers["link_channels"]({"id": 81, "payload": {"account_id": 77}})

    assert linked.access_calls == []
    assert telegram.join_once_calls == [101]
    database.update_channel_link.assert_called_once_with(
        1,
        101,
        None,
        "Связано · участие уже было",
    )
    database.mark_link_checked.assert_called_with(1, account_id=77)


@pytest.mark.asyncio
async def test_join_without_confirmation_does_not_issue_membership_rpc(monkeypatch):
    class Client:
        def __init__(self) -> None:
            self.join_calls = 0
            self.permission_calls = 0

        def is_connected(self) -> bool:
            return True

        async def get_entity(self, chat_id):
            return chat_id

        async def __call__(self, _request):
            self.join_calls += 1
            raise ConnectionError("join response lost")

        async def get_permissions(self, _chat_id, _who):
            self.permission_calls += 1
            return object()

    class Limiter:
        async def acquire(self):
            return None

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = None

    async def no_op():
        return None

    monkeypatch.setattr(service, "disconnect", no_op)
    monkeypatch.setattr(service, "ensure_connected", no_op)
    monkeypatch.setattr(
        service, "safe_sleep", lambda _seconds: asyncio.sleep(0, result=True)
    )

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.join_without_confirmation(123)

    assert raised.value.code == "join_result_unknown"
    assert service.client.join_calls == 1
    assert service.client.permission_calls == 0

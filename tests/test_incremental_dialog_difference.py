from __future__ import annotations

import pytest
from telethon import types

from core.exceptions import NonRetryableTelegramError
from services.telegram_service import TelegramService


def test_incremental_marker_validation_is_fail_closed():
    service = object.__new__(TelegramService)
    with pytest.raises(NonRetryableTelegramError) as exc_info:
        service._validated_dialog_sync_state({"version": 99})
    assert exc_info.value.code == "full_sync_required"


@pytest.mark.asyncio
async def test_difference_too_long_requires_full_sync():
    service = object.__new__(TelegramService)
    service.client = object()

    async def ensure_connected():
        return None

    async def execute(_method, _request, **_kwargs):
        return types.updates.DifferenceTooLong(pts=999_999)

    service.ensure_connected = ensure_connected
    service.execute = execute
    marker = {
        "version": 1, "pts": 100, "qts": 0,
        "date": 1_700_000_000, "seq": 10,
    }
    with pytest.raises(NonRetryableTelegramError) as exc_info:
        await service.fetch_incremental_dialog_snapshots(marker)
    assert exc_info.value.code == "full_sync_required"
    assert marker["pts"] == 100

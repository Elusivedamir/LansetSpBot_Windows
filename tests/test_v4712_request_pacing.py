from __future__ import annotations

import asyncio
import time

import pytest
from telethon import TelegramClient
from telethon.sessions import StringSession

from core.rate_limiter import RateLimiter
from services.paced_telegram_client import PacedTelegramClient


def test_request_interval_has_hard_one_second_floor() -> None:
    assert RateLimiter(0.01).interval == 1.0
    assert RateLimiter(1.0).interval == 1.0
    assert RateLimiter(3.5).interval == 3.5


@pytest.mark.asyncio
async def test_client_serializes_and_spaces_concurrent_requests(monkeypatch) -> None:
    RateLimiter._reset_for_tests()
    limiter = RateLimiter(1.0)
    limiter.interval = (
        0.05  # keep the regression test fast; production is clamped to 1s
    )
    starts: list[float] = []
    active = 0
    max_active = 0

    async def fake_call(self, request, ordered=False, flood_sleep_threshold=None):  # noqa: ANN001
        nonlocal active, max_active
        del self, ordered, flood_sleep_threshold
        starts.append(time.monotonic())
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return request

    monkeypatch.setattr(TelegramClient, "__call__", fake_call)
    client = PacedTelegramClient(
        StringSession(),
        1,
        "hash",
        request_limiter=limiter,
        request_timeout=1.0,
    )

    try:
        assert await asyncio.gather(client("one"), client("two")) == ["one", "two"]
        assert max_active == 1
        assert len(starts) == 2
        assert starts[1] - starts[0] >= 0.045
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_client_splits_request_batches_instead_of_bursting(monkeypatch) -> None:
    RateLimiter._reset_for_tests()
    limiter = RateLimiter(1.0)
    limiter.interval = 0.03
    starts: list[float] = []

    async def fake_call(self, request, ordered=False, flood_sleep_threshold=None):  # noqa: ANN001
        del self, ordered, flood_sleep_threshold
        starts.append(time.monotonic())
        return request

    monkeypatch.setattr(TelegramClient, "__call__", fake_call)
    client = PacedTelegramClient(
        StringSession(),
        1,
        "hash",
        request_limiter=limiter,
        request_timeout=1.0,
    )

    try:
        assert await client([1, 2, 3]) == [1, 2, 3]
        assert len(starts) == 3
        assert starts[1] - starts[0] >= 0.025
        assert starts[2] - starts[1] >= 0.025
    finally:
        await client.disconnect()

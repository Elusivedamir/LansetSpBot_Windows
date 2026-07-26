from __future__ import annotations

from core.config import Config
from core.rate_limiter import RateLimiter
from services.telegram_service import TelegramService


def test_global_request_floor_is_one_second(monkeypatch) -> None:
    monkeypatch.setenv("RATE_LIMIT", "0.1")
    assert Config().rate_limit == 1.0
    assert RateLimiter(0.01).interval == 1.0
    assert RateLimiter(1.0).interval == 1.0


def test_flood_and_slowmode_buffer_is_thirty_to_forty_five_seconds() -> None:
    assert TelegramService.FLOOD_WAIT_BUFFER_MIN_SECONDS == 30
    assert TelegramService.FLOOD_WAIT_BUFFER_MAX_SECONDS == 45

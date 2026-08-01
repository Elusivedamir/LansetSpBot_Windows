from __future__ import annotations

from datetime import datetime, time
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core.activity_schedule import (
    ActivityScheduleConfig,
    ActivityScheduleManager,
    QUIET_END_KEY,
    QUIET_START_KEY,
    SCHEDULE_ENABLED_KEY,
    TIMEZONE_KEY,
)
from core.exceptions import DeferredTelegramError

# CommentService only needs these names for annotations/construction. Load a
# private copy with temporary lightweight facades so the dependency-light audit
# environment can run this focused test without polluting ``sys.modules`` for
# the rest of the historical suite.
def _load_comment_service_class():
    names = ("services.linked_chat_service", "services.telegram_service")
    previous = {name: sys.modules.get(name) for name in names}
    linked_module = ModuleType("services.linked_chat_service")
    linked_module.LinkedChatService = object
    telegram_module = ModuleType("services.telegram_service")
    telegram_module.TelegramService = object
    sys.modules[names[0]] = linked_module
    sys.modules[names[1]] = telegram_module
    try:
        path = Path(__file__).resolve().parents[1] / "services" / "comment_service.py"
        spec = importlib.util.spec_from_file_location(
            "_v45_comment_service_for_test", path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.CommentService
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


CommentService = _load_comment_service_class()


def _manager(*, start: time = time(22, 0), end: time = time(7, 0)):
    return ActivityScheduleManager(
        ActivityScheduleConfig(
            enabled=True,
            timezone_name="Europe/Berlin",
            quiet_start=start,
            quiet_end=end,
        ),
        account_id=7001,
    )


def test_overnight_quiet_window_uses_account_local_time() -> None:
    manager = _manager()
    now = datetime(2026, 7, 25, 23, 30, tzinfo=ZoneInfo("Europe/Berlin"))

    decision = manager.decision(now)

    assert decision.allowed is False
    assert decision.resume_at_local == datetime(
        2026, 7, 26, 7, 0, tzinfo=ZoneInfo("Europe/Berlin")
    )
    assert decision.retry_after_seconds == 7 * 3600 + 30 * 60


def test_schedule_allows_dispatch_outside_quiet_window() -> None:
    manager = _manager()
    now = datetime(2026, 7, 25, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    assert manager.decision(now).allowed is True


def test_equal_boundaries_fail_closed_for_full_day() -> None:
    manager = _manager(start=time(8, 0), end=time(8, 0))
    now = datetime(2026, 7, 25, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    decision = manager.decision(now)

    assert decision.allowed is False
    assert decision.retry_after_seconds == 20 * 3600



def test_spring_dst_gap_resumes_at_first_valid_local_minute() -> None:
    manager = _manager(end=time(2, 30))
    now = datetime(2026, 3, 29, 1, 30, tzinfo=ZoneInfo("Europe/Berlin"))

    decision = manager.decision(now)

    assert decision.allowed is False
    assert decision.resume_at_local == datetime(
        2026, 3, 29, 3, 0, tzinfo=ZoneInfo("Europe/Berlin")
    )
    assert decision.retry_after_seconds == 30 * 60


def test_fall_dst_overlap_selects_future_fold() -> None:
    manager = _manager(end=time(2, 30))
    now = datetime(
        2026,
        10,
        25,
        2,
        15,
        tzinfo=ZoneInfo("Europe/Berlin"),
        fold=1,
    )

    decision = manager.decision(now)

    assert decision.allowed is False
    assert decision.resume_at_local is not None
    assert decision.resume_at_local.fold == 1
    assert decision.resume_at_local.hour == 2
    assert decision.resume_at_local.minute == 30
    assert decision.retry_after_seconds == 15 * 60

def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="Неизвестный часовой пояс"):
        ActivityScheduleConfig.from_mapping(
            {
                SCHEDULE_ENABLED_KEY: "1",
                TIMEZONE_KEY: "Mars/Olympus",
                QUIET_START_KEY: "22:00",
                QUIET_END_KEY: "07:00",
            }
        )


def test_require_active_raises_durable_deferral() -> None:
    manager = _manager()
    now = datetime(2026, 7, 25, 23, 30, tzinfo=ZoneInfo("Europe/Berlin"))

    with pytest.raises(DeferredTelegramError) as raised:
        manager.require_active(now)

    assert raised.value.code == "local_quiet_hours"
    assert raised.value.retry_after == 7 * 3600 + 30 * 60


class _Schedule:
    def __init__(self, *, blocked: bool) -> None:
        self.blocked = blocked
        self.calls = 0

    def require_active(self):
        self.calls += 1
        if self.blocked:
            raise DeferredTelegramError(
                "quiet",
                code="local_quiet_hours",
                retry_after=60,
            )


class _Database:
    def __init__(self) -> None:
        self.reserve_calls = 0

    def is_channel_locally_banned(self, *_args, **_kwargs):
        return False

    def reserve_comment_delivery(self, *_args, **_kwargs):
        self.reserve_calls += 1
        return True


class _Telegram:
    def __init__(self) -> None:
        self.calls = 0

    async def send_comment(self, *_args, **_kwargs):
        self.calls += 1
        return SimpleNamespace(id=9001, sender_id=7001, date="2026-07-25")


@pytest.mark.asyncio
async def test_automated_comment_is_deferred_before_delivery_reservation() -> None:
    schedule = _Schedule(blocked=True)
    database = _Database()
    telegram = _Telegram()
    service = CommentService(
        telegram,
        db=database,
        activity_schedule=schedule,
    )

    with pytest.raises(DeferredTelegramError):
        await service.ensure_and_send_comment(
            channel_id=-1001,
            linked_chat_id=-1002,
            post_message_id=10,
            text="Комментарий",
            account_id=7001,
            campaign_id=99,
            action_type="campaign_comment",
        )

    assert schedule.calls == 1
    assert database.reserve_calls == 0
    assert telegram.calls == 0


@pytest.mark.asyncio
async def test_manual_comment_bypasses_automation_quiet_hours() -> None:
    schedule = _Schedule(blocked=True)
    telegram = _Telegram()
    service = CommentService(telegram, activity_schedule=schedule)

    result = await service.ensure_and_send_comment(
        channel_id=-1001,
        linked_chat_id=-1002,
        post_message_id=10,
        text="Ручной комментарий",
        account_id=7001,
        campaign_id=0,
        action_type="manual_comment",
    )

    assert result.id == 9001
    assert schedule.calls == 0
    assert telegram.calls == 1


def test_campaign_schedule_deferral_does_not_enter_network_wait() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "workers"
        / "comment_slot"
        / "runner.py"
    ).read_text(encoding="utf-8")
    start = source.index("    def _defer_quiet_hours(")
    end = source.index("    def _defer_telegram_wait(", start)
    branch = source[start:end]

    assert "defer_comment_slot(" in branch
    assert "defer_comment_slot_and_set_network_wait" not in branch
    assert "reserve_comment_delivery" not in branch


def test_account_view_exposes_explicit_schedule_controls() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "gui" / "views" / "account_view.py"
    ).read_text(encoding="utf-8")

    assert "self.schedule_enabled = QCheckBox" in source
    assert "self.timezone_name = QLineEdit" in source
    assert "self.quiet_start = QTimeEdit" in source
    assert "self.quiet_end = QTimeEdit" in source
    assert "self.save_schedule_button" in source

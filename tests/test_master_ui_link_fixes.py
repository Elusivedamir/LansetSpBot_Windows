from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from core.exceptions import DeferredTelegramError
from core.rate_limiter import RateLimiter, RpcCategory
from gui.account_manager_panel import AccountManagerPanel, format_account_identity
from gui.theme import (
    AURORA_BACKGROUND,
    AURORA_GREEN,
    AURORA_GREEN_GLOW,
    AURORA_GREEN_HOVER,
    CARD_BACKGROUND,
    TEXT_MUTED,
    TEXT_PRIMARY,
)
from workers.handlers.link_channels_flow import ChannelWork
from workers.handlers.link_channels_hardened import HardenedLinkChannelsRunner


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _accounts() -> list[dict]:
    return [
        {
            "telegram_account_id": 101,
            "first_name": "Анна",
            "last_name": "Иванова",
            "display_name": "Анна Иванова",
            "username": "AnnaWork",
            "phone_masked": "+49 *** 1234",
            "runtime_state": "connected",
        },
        {
            "telegram_account_id": 202,
            "display_name": "Telegram Account",
            "username": "source_user",
            "phone_masked": "+7 *** 7788",
            "runtime_state": "stopped",
            "stopped": True,
        },
    ]


def test_account_identity_never_uses_technical_session_name() -> None:
    assert (
        format_account_identity(101, _accounts()[0])
        == "Анна Иванова · @AnnaWork"
    )
    assert (
        format_account_identity(202, _accounts()[1])
        == "@source_user · Telegram ID 202"
    )
    assert format_account_identity(
        303,
        {"display_name": "pending_deadbeefdeadbeef", "username": ""},
    ) == "Telegram ID 303"


def test_live_account_search_and_import_sources_do_not_switch_account() -> None:
    app = _app()
    panel = AccountManagerPanel()
    selected: list[int] = []
    panel.account_selected.connect(selected.append)
    panel.reload(_accounts(), selected_account_id=101, previous_account_id=202)
    panel.set_data_counts(
        comment_counts={202: 8},
        channel_counts={202: 12},
    )

    for query in ("анна", "иванова", "@annawork", "annaw", "1234", "101"):
        panel.search.setText(query)
        app.processEvents()
        assert panel.selector.count() == 1
        assert int(panel.selector.itemData(0)) == 101
        assert selected == []

    panel.search.setText("7788")
    app.processEvents()
    assert panel.selector.count() == 1
    assert int(panel.selector.itemData(0)) == 202
    assert selected == []

    panel.search.clear()
    app.processEvents()
    assert panel.selector.count() == 2
    assert int(panel.selector.currentData()) == 101
    assert panel.import_comments_button.isEnabled()
    assert panel.import_channels_button.isEnabled()
    assert not hasattr(panel, "delete_button")
    panel.deleteLater()
    app.processEvents()


def test_import_buttons_disabled_without_data_source() -> None:
    app = _app()
    panel = AccountManagerPanel()
    panel.reload(_accounts(), selected_account_id=101, previous_account_id=202)
    panel.set_data_counts(comment_counts={202: 0}, channel_counts={202: 0})
    assert not panel.import_comments_button.isEnabled()
    assert not panel.import_channels_button.isEnabled()
    assert (
        panel.import_comments_button.toolTip()
        == "Нет другого аккаунта с данными для импорта"
    )
    panel.deleteLater()
    app.processEvents()


class _FixedRandom:
    def __init__(self, value: float):
        self.value = float(value)

    def uniform(self, low: float, high: float) -> float:
        assert low <= self.value <= high
        return self.value


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += float(seconds)


@pytest.mark.asyncio
async def test_every_rpc_waits_two_to_five_seconds_with_fake_clock() -> None:
    RateLimiter._reset_for_tests()
    clock = _FakeTime()
    limiter = RateLimiter(
        rng=_FixedRandom(3.0),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    async with limiter.request_slot(RpcCategory.READ):
        pass
    async with limiter.request_slot(RpcCategory.RESOLVE_ENTITY):
        pass
    assert 2.0 <= sum(clock.sleeps) <= 5.0


@pytest.mark.asyncio
async def test_link_runner_waits_two_to_five_minutes_before_second_join(monkeypatch) -> None:
    runner = object.__new__(HardenedLinkChannelsRunner)
    runner.task_id = 11
    runner.account_id = 101
    runner.channel_ids = [999]
    runner.group_by_id = {}
    runner.resolved_discussion_ids = set()
    runner.join_attempt_count = 1
    runner.joined_count = 0
    runner.prepared_count = 0
    runner.join_delay_min = 120.0
    runner.join_delay_max = 300.0
    runner.checkpoint = {}
    events: list[tuple[str, float | int]] = []

    async def checkpointed_sleep(delay: float, **kwargs) -> None:
        events.append((str(kwargs["wait_type"]), float(delay)))

    class _Telegram:
        async def join_without_confirmation(self, linked_id: int, **_kwargs) -> bool:
            events.append(("join", linked_id))
            return False

    class _Database:
        def record_join_event(self, *_args, **_kwargs) -> None:
            pytest.fail("already-member result must not create a join event")

    runner.telegram = _Telegram()
    runner.worker_db = _Database()
    runner._checkpointed_sleep = checkpointed_sleep
    runner._join_guard = lambda: {"allowed": True}
    runner.set_runtime = lambda *_args, **_kwargs: None
    runner.pause_requested = lambda: False
    runner._current_channel_allows_rpc = lambda *_args, **_kwargs: True
    runner._create_join_dispatch_barrier = lambda *_args, **_kwargs: None
    runner._update_channel_link = lambda *_args, **_kwargs: None
    monkeypatch.setattr(
        "workers.handlers.link_channels_hardened.random.uniform",
        lambda low, high: 180.0,
    )

    result = await runner._prepare_linked_channel(
        ChannelWork(channel_id=999, row={}, title="Test", number=1),
        linked_id=777,
    )

    assert str(result) == "complete"
    assert events == [("local_join_cooldown", 180.0), ("join", 777)]


def test_true_floodwait_keeps_current_channel_checkpoint() -> None:
    runner = object.__new__(HardenedLinkChannelsRunner)
    runner.task_id = 7
    runner.account_id = 101
    runner.channel_index = 2
    runner.group_index = 0
    runner.checkpoint = {}
    persisted: list[str] = []
    runtime: list[str] = []
    runner.persist_checkpoint = lambda *, phase: persisted.append(phase)
    runner.set_runtime = lambda _task_id, message, **_kwargs: runtime.append(message)
    runner.pause_at_checkpoint = lambda *, phase: pytest.fail(
        f"unexpected pause disposition: {phase}"
    )

    work = ChannelWork(channel_id=999, row={}, title="Test", number=3)
    error = DeferredTelegramError(
        "Telegram FloodWait",
        code="telegram_flood_wait",
        retry_after=169,
    )
    cause = RuntimeError("server wait")
    cause.seconds = 132  # type: ignore[attr-defined]
    error.__cause__ = cause
    with pytest.raises(DeferredTelegramError):
        runner._handle_deferred_channel(work, error)

    assert runner.channel_index == 2
    assert runner.checkpoint["wait_type"] == "telegram_flood_wait"
    assert runner.checkpoint["current_channel_index"] == 2
    assert persisted == ["channels"]
    assert runner.checkpoint["telegram_wait_seconds"] == 132
    assert runner.checkpoint["safety_buffer_seconds"] == 37
    assert any("132 сек + защитный запас 37 сек" in message for message in runtime)


def test_aurora_theme_has_one_central_neon_token_set() -> None:
    assert AURORA_GREEN == "#39FF14"
    assert AURORA_GREEN_HOVER == "#4CFF2B"
    assert AURORA_GREEN_GLOW == "#66FF47"
    assert AURORA_BACKGROUND
    assert CARD_BACKGROUND
    assert TEXT_PRIMARY
    assert TEXT_MUTED


def test_main_window_uses_single_aurora_canvas_and_premium_views() -> None:
    source = Path("gui/main_window.py").read_text(encoding="utf-8")
    assert source.count("AuroraBackgroundWidget()") == 1
    assert "PremiumAccountView" in source
    assert "PremiumLinksView" in source
    assert "PremiumInstructionsView" in source
    assert "ActivityPanel" in source


def test_obsolete_spambot_action_is_not_part_of_active_main_window() -> None:
    source = Path("gui/main_window.py").read_text(encoding="utf-8")
    journal = Path("gui/activity_panel.py").read_text(encoding="utf-8")
    assert "spambot_button" not in source
    assert "QDesktopServices" not in journal
    assert "tg://resolve?domain=SpamBot" not in journal


def test_instruction_is_utf8_text_native_with_cyrillic_fallback() -> None:
    source = Path("gui/views/premium_instructions_view.py").read_text(
        encoding="utf-8"
    )
    assert "Инструкция LansetSpBot для Windows" in source
    assert 'QFont("Segoe UI")' in source
    assert "Вписать в окно" in source
    assert "100%" in source


def test_selected_account_card_and_schedule_state_are_explicit() -> None:
    source = Path("gui/views/premium_account_view.py").read_text(encoding="utf-8")
    assert 'setObjectName("selectedAccountCard")' in source
    assert 'setObjectName("accountDeleteButton")' in source
    assert "self.status_card.hide()" in source
    assert 'QUIET_BLOCK_SETTINGS_KEY = "ui/account/quiet_schedule_expanded"' in source
    assert "QSettings().setValue" in source
    assert "🌙 Расписание тишины" in source


def test_factory_reset_dialog_has_readable_russian_actions() -> None:
    source = Path("gui/dialogs.py").read_text(encoding="utf-8")
    assert 'self.reset_button = QPushButton("Сбросить")' in source
    assert 'self.cancel_button = QPushButton("Отмена")' in source
    assert 'self.reset_button.setObjectName("dangerButton")' in source
    assert 'self.cancel_button.setObjectName("secondaryButton")' in source


def test_restriction_api_has_no_manual_spambot_clear_action() -> None:
    source = Path("services/api_parts/restrictions.py").read_text(encoding="utf-8")
    assert "confirm_spambot" not in source.casefold()
    assert "get_account_restriction_state" in source


def test_active_restriction_code_has_no_spambot_specific_action_or_text() -> None:
    source = Path("core/account_restriction.py").read_text(encoding="utf-8")
    assert "spambot" not in source.casefold()
    assert "clear_account_restriction_after_authoritative_check" in source

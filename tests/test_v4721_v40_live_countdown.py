from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.countdown import countdown_label, format_countdown, seconds_until

UTC = timezone.utc


def test_countdown_is_recomputed_from_absolute_deadline() -> None:
    start = datetime(2026, 7, 22, 0, 40, 0, tzinfo=UTC)
    deadline = start + timedelta(seconds=65)

    assert seconds_until(deadline, now=start) == 65
    assert format_countdown(65) == "01:05"
    assert seconds_until(deadline, now=start + timedelta(seconds=17)) == 48
    assert format_countdown(48) == "00:48"


def test_delayed_ui_tick_catches_up_instead_of_drifting() -> None:
    start = datetime(2026, 7, 22, 0, 40, 0, tzinfo=UTC)
    deadline = start + timedelta(seconds=65)

    # Simulate a Qt timer callback arriving 12 seconds late.
    delayed_tick = start + timedelta(seconds=22)
    assert seconds_until(deadline, now=delayed_tick) == 43
    assert "00:43" in countdown_label(
        "Следующая проверка",
        deadline,
        now=delayed_tick,
        include_deadline=False,
    )


def test_countdown_never_shows_zero_before_deadline() -> None:
    deadline = datetime(2026, 7, 22, 0, 41, 0, tzinfo=UTC)
    just_before = deadline - timedelta(milliseconds=100)

    assert seconds_until(deadline, now=just_before) == 1
    assert seconds_until(deadline, now=deadline) == 0
    assert "выполняется" in countdown_label(
        "Следующая проверка",
        deadline,
        now=deadline,
        include_deadline=False,
    )

from __future__ import annotations

import math
from datetime import datetime

from core.campaign_schedule import ensure_utc, from_db_time, utc_now


def seconds_until(value: str | datetime | None, *, now: datetime | None = None) -> int | None:
    """Return whole seconds until *value*, rounded upward and never negative.

    Rounding upward prevents the UI from showing ``00:00`` before the actual
    deadline.  The caller may invoke this after a delayed Qt timer event; the
    value is always recalculated from the absolute timestamp, so the display
    catches up immediately instead of drifting.
    """

    target = from_db_time(value)
    if target is None:
        return None
    current = ensure_utc(now) if now is not None else utc_now()
    delta = (target - current).total_seconds()
    if delta <= 0:
        return 0
    return int(math.ceil(delta))


def format_countdown(seconds: int) -> str:
    """Format a non-negative duration as MM:SS or HH:MM:SS."""

    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def countdown_label(
    prefix: str,
    value: str | datetime | None,
    *,
    now: datetime | None = None,
    include_deadline: bool = True,
    include_date: bool = False,
    due_text: str = "выполняется…",
) -> str:
    """Build a live countdown label from an absolute UTC timestamp."""

    target = from_db_time(value)
    if target is None:
        return f"{prefix}: —"
    remaining = seconds_until(target, now=now)
    if remaining is None:
        return f"{prefix}: —"
    if remaining == 0:
        text = f"{prefix}: {due_text}"
    else:
        text = f"{prefix} через {format_countdown(remaining)}"
    if include_deadline:
        pattern = "%d.%m %H:%M:%S" if include_date else "%H:%M:%S"
        text += f" · {target.astimezone().strftime(pattern)}"
    return text

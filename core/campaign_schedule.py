from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
DB_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DB_TIME_PRECISE_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
MICROSECONDS_PER_SECOND = 1_000_000


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_db_time(value: datetime) -> str:
    return ensure_utc(value).strftime(DB_TIME_FORMAT)


def to_db_time_precise(value: datetime) -> str:
    """Serialize a UTC instant without losing sub-second join spacing."""
    return ensure_utc(value).strftime(DB_TIME_PRECISE_FORMAT)


def from_db_time(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("T", " ").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = datetime.strptime(text[:19], DB_TIME_FORMAT)
    return ensure_utc(parsed)


def generate_random_slots(
    start: datetime,
    end: datetime,
    count: int,
    *,
    rng: random.Random | None = None,
    minimum_gap_seconds: float = 1.0,
) -> list[datetime]:
    """Return ordered, randomized slots spread across the whole window.

    The window is split into ``count`` equal segments and one timestamp is
    selected from the middle 80% of every segment. This preserves the exact
    requested count from 1 to 1000, avoids clustering, and keeps slots ordered
    even when the average interval is only a few seconds.

    No fixed startup delay is applied. The first and all subsequent slots are
    derived solely from the selected 24-hour cadence, so dense schedules (for
    example 223 or 1000 per day) are not distorted by a legacy 15-30 minute
    rule.
    """

    start = ensure_utc(start)
    end = ensure_utc(end)
    count = max(0, int(count))
    if count == 0 or end <= start:
        return []

    source = rng or random.SystemRandom()
    duration = end - start
    total_microseconds = (
        duration.days * 86_400 + duration.seconds
    ) * MICROSECONDS_PER_SECOND + duration.microseconds
    minimum_gap_microseconds = max(
        MICROSECONDS_PER_SECOND,
        math.ceil(float(minimum_gap_seconds) * MICROSECONDS_PER_SECOND),
    )
    required_microseconds = (
        count - 1
    ) * minimum_gap_microseconds + MICROSECONDS_PER_SECOND
    if total_microseconds < required_microseconds:
        raise ValueError(
            "Campaign window is too short to preserve the requested minimum slot gap"
        )
    slots: list[datetime] = []
    previous_offset_microseconds: int | None = None

    for index in range(count):
        segment_start = total_microseconds * index / count
        segment_end = total_microseconds * (index + 1) / count
        margin = (segment_end - segment_start) * 0.10
        low = segment_start + margin
        high = segment_end - margin

        if high <= low:
            low = segment_start
            high = segment_end
        candidate_microseconds = round(float(source.uniform(low, high)))

        # SQLite stores campaign timestamps with one-second precision. Preserve
        # randomness while enforcing a real one-second floor and enough room for
        # every remaining slot. This prevents two distinct Python datetimes from
        # collapsing to the same persistent slot after formatting.
        minimum_offset_microseconds = (
            0
            if previous_offset_microseconds is None
            else previous_offset_microseconds + minimum_gap_microseconds
        )
        remaining_after = count - index - 1
        maximum_offset_microseconds = (
            total_microseconds
            - remaining_after * minimum_gap_microseconds
            - MICROSECONDS_PER_SECOND
        )
        offset_microseconds = min(
            max(candidate_microseconds, minimum_offset_microseconds),
            maximum_offset_microseconds,
        )
        if (
            previous_offset_microseconds is not None
            and offset_microseconds - previous_offset_microseconds
            < minimum_gap_microseconds
        ):
            raise ValueError("Unable to preserve the minimum campaign slot interval")
        slots.append(start + timedelta(microseconds=offset_microseconds))
        previous_offset_microseconds = offset_microseconds

    return slots


def redistribute_slots(
    now: datetime,
    end: datetime,
    count: int,
    *,
    minimum_lead_seconds: int = 0,
    rng: random.Random | None = None,
    minimum_gap_seconds: float = 1.0,
) -> list[datetime]:
    """Spread remaining slots over the remaining campaign time.

    The caller already caps the remaining count to the original campaign
    density. This function therefore supports the full 0-1000 range instead of
    imposing a fixed restart delay. The first slot follows the same proportional
    cadence as a newly created campaign. At most one slot per second is generated.
    """

    now = ensure_utc(now)
    end = ensure_utc(end)
    earliest = now + timedelta(seconds=max(0, int(minimum_lead_seconds)))
    if end <= earliest or count <= 0:
        return []

    available = (end - earliest).total_seconds()
    max_slots = max(0, int(available))
    if max_slots <= 0:
        return []
    safe_count = min(int(count), max_slots)
    return generate_random_slots(
        earliest,
        end,
        safe_count,
        rng=rng,
        minimum_gap_seconds=minimum_gap_seconds,
    )


def generate_join_slots(
    start: datetime,
    count: int,
    *,
    rng: random.Random | None = None,
    max_per_hour: int = 40,
    minimum_gap_seconds: int | None = None,
    maximum_gap_seconds: int | None = None,
) -> list[datetime]:
    """Create a persistent join schedule for the configured hourly limit.

    No hidden 40/hour ceiling is applied. The rolling database guard and the
    separately configured minimum join interval remain the final authorities,
    while this schedule distributes attempts without bursts.
    """
    start = ensure_utc(start)
    count = max(0, int(count))
    if count <= 0:
        return []
    source = rng or random.SystemRandom()
    hourly_limit = max(1, int(max_per_hour))
    rate_gap = int(math.ceil(3600 / hourly_limit))
    requested_minimum = (
        rate_gap if minimum_gap_seconds is None else int(minimum_gap_seconds)
    )
    minimum = max(1, rate_gap, requested_minimum)
    requested_maximum = (
        minimum + 60 if maximum_gap_seconds is None else int(maximum_gap_seconds)
    )
    maximum = max(minimum, requested_maximum)
    current = start + timedelta(seconds=source.uniform(60, 150))
    slots = []
    for _ in range(count):
        slots.append(current)
        current += timedelta(seconds=source.uniform(minimum, maximum))
    return slots


def local_display(value: str | datetime | None) -> str:
    parsed = from_db_time(value)
    if parsed is None:
        return "—"
    return parsed.astimezone().strftime("%d.%m %H:%M")

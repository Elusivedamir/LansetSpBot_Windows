from __future__ import annotations


def _timedelta_microseconds(value) -> int:
    """Return an exact integer duration without float rounding drift."""
    return (int(value.days) * 86_400 + int(value.seconds)) * 1_000_000 + int(
        value.microseconds
    )

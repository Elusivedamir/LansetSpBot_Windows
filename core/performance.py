"""Small, dependency-free helpers for local performance diagnostics."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any


def log_if_slow(
    logger: logging.Logger,
    operation: str,
    started_at: float,
    *,
    threshold_seconds: float,
    level: int = logging.WARNING,
    **details: Any,
) -> float:
    """Log a slow operation without including payloads or secrets.

    Callers should pass only safe scalar diagnostics such as task ids, operation
    names and counters. Message text, API credentials and session data must never
    be included in ``details``.
    """

    elapsed = max(0.0, time.monotonic() - float(started_at))
    if elapsed < max(0.0, float(threshold_seconds)):
        return elapsed
    safe_details = " ".join(
        f"{key}={value}" for key, value in details.items() if value is not None
    )
    suffix = f" {safe_details}" if safe_details else ""
    logger.log(level, "Slow operation: %s took %.3fs%s", operation, elapsed, suffix)
    return elapsed


def wal_size_bytes(database_path: str | Path) -> int:
    """Return the current SQLite WAL size, or zero when no WAL file exists."""

    wal_path = Path(f"{Path(database_path)}-wal")
    try:
        return int(wal_path.stat().st_size)
    except OSError:
        return 0

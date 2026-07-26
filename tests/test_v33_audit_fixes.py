from __future__ import annotations

import math
import random
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.campaign_schedule import redistribute_slots
from core.paths import AppPaths
from services.api_parts.task_queue import TaskQueueAPIMixin
from storage.database import Database


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        database=root / "marlen.db",
        logs=root / "logs",
        sessions=root / "sessions",
        backups=root / "backups",
    )


def _sqlite_session(path: Path, value: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE sample(value INTEGER NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES(?)", (value,))
        connection.commit()


class _TaskAPI(TaskQueueAPIMixin):
    ALLOWED_TASK_TYPES = frozenset({"auto_comment"})
    ACCOUNT_BOUND_TASK_TYPES = frozenset({"auto_comment"})
    NON_IDEMPOTENT_TASK_TYPES = frozenset({"auto_comment"})

    def __init__(self, database: Database) -> None:
        self.database = database
        self._auth_in_progress = False


def _task_api(database: Database) -> _TaskAPI:
    return _TaskAPI(database)


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf, "NaN", "Infinity"])
def test_auto_comment_rejects_non_finite_delays_without_partial_task(
    tmp_path: Path, invalid: object
) -> None:
    database = Database(tmp_path / "marlen.db")
    database.set_setting("telegram.account_id", 42)
    api = _task_api(database)

    with pytest.raises(ValueError, match="finite"):
        api.create_task(
            "auto_comment",
            {"comments": ["test"], "delay_min": invalid, "delay_max": 10},
        )

    assert database.get_tasks(limit=10) == []


def test_redistribute_slots_honors_minimum_lead_after_wake() -> None:
    now = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    lead = 180
    slots = redistribute_slots(
        now,
        now + timedelta(hours=2),
        20,
        minimum_lead_seconds=lead,
        minimum_gap_seconds=15,
        rng=random.Random(7331),
    )

    assert len(slots) == 20
    assert slots == sorted(slots)
    assert slots[0] > now + timedelta(seconds=lead)
    assert all(b - a >= timedelta(seconds=15) for a, b in zip(slots, slots[1:]))



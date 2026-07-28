from __future__ import annotations

from datetime import timedelta

import pytest

from core.campaign_schedule import utc_now
from workers.flood_wait_guard import install_account_flood_wait
from workers.queue_worker import QueueWorker


def test_flood_wait_is_remembered_before_persistence_failure() -> None:
    events: list[tuple[str, str]] = []

    class Worker:
        def remember_account_rpc_cooldown(self, account_id, wait, key=""):
            events.append(("remember", str(key)))
            return int(wait)

    class BrokenDatabase:
        def set_account_rpc_cooldown(self, **_kwargs):
            events.append(("write", ""))
            raise RuntimeError("database is locked")

    with pytest.raises(RuntimeError, match="database is locked"):
        install_account_flood_wait(
            queue_worker=Worker(),
            worker_db=BrokenDatabase(),
            account_id=7001,
            retry_at=utc_now() + timedelta(minutes=3),
            code="flood_wait_deferred",
            source_task_id=11,
            wait_seconds=180,
        )

    assert events == [("remember", ""), ("write", "")]


def test_successful_flood_wait_updates_local_key_after_write() -> None:
    events: list[tuple[str, str]] = []

    class Worker:
        def remember_account_rpc_cooldown(self, account_id, wait, key=""):
            events.append(("remember", str(key)))
            return int(wait)

    class Database:
        def set_account_rpc_cooldown(self, **_kwargs):
            events.append(("write", ""))
            return {"next_allowed_at": "2030-01-01 00:00:00"}

    install_account_flood_wait(
        queue_worker=Worker(),
        worker_db=Database(),
        account_id=7002,
        retry_at=utc_now() + timedelta(minutes=3),
        code="flood_wait_deferred",
        source_task_id=12,
        wait_seconds=180,
    )

    assert events == [
        ("remember", ""),
        ("write", ""),
        ("remember", "2030-01-01 00:00:00"),
    ]


def test_local_embargo_survives_missing_database_row() -> None:
    worker = QueueWorker(lambda: ({}, None))
    worker.remember_account_rpc_cooldown(7003, 120, "")

    remaining = worker._account_rpc_cooldown_remaining(7003, {})

    assert 1 <= remaining <= 120


def test_database_read_failure_uses_local_embargo() -> None:
    class Database:
        def __init__(self) -> None:
            self.postponed = False

        def get_account_rpc_cooldown(self, *, account_id):
            raise RuntimeError("database is locked")

        def postpone_running_task_for_account_cooldown(
            self, task_id, *, retry_at, code
        ):
            self.postponed = True
            return True

    database = Database()
    worker = QueueWorker(lambda: ({}, None))
    worker._db = database
    worker.remember_account_rpc_cooldown(7004, 120, "")

    assert worker._postpone_for_account_rpc_cooldown(
        task_id=99,
        task_type="sync_channels",
        account_id=7004,
    ) is True
    assert database.postponed is True

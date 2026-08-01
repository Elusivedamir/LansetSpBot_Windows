from __future__ import annotations

import random
import threading
import time
from datetime import timedelta

import pytest
from PySide6.QtWidgets import QApplication

from core.campaign_schedule import generate_join_slots, utc_now
from core.composition import ApplicationContainer
from core.config import Config
from storage.database import Database


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def test_persistent_idle_worker_does_not_block_account_change(
    qapp, monkeypatch, tmp_path
):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "idle-account-change.db"))
    container = ApplicationContainer(Config())
    try:
        task = container.api.create_task("noop", {})
        completed = threading.Event()
        container.queue_worker.task_completed.connect(
            lambda task_id: completed.set()
            if int(task_id) == int(task["id"])
            else None
        )
        assert container.api.start_queue() is True
        deadline = time.monotonic() + 30
        while not completed.is_set() and time.monotonic() < deadline:
            qapp.processEvents()
            completed.wait(0.02)
        assert completed.is_set()
        assert container.api.get_task(task["id"])["status"] == "completed"
        assert container.queue_worker.isRunning() is True
        assert _wait_until(
            lambda: not container.queue_worker.has_active_task,
            timeout=30,
        )
        assert container.api.is_queue_running() is False

        assert container.api.prepare_account_change(5_000) is True
        assert container.queue_worker.isRunning() is False
    finally:
        container.shutdown()


def test_unfinished_task_still_blocks_account_change(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "unfinished-account-change.db"))
    container = ApplicationContainer(Config())
    try:
        task = container.api.create_task("noop", {})
        assert container.api.is_queue_running() is True
        assert container.api.prepare_account_change(100) is False
        assert container.api.cancel_task(task["id"]) is True
        assert container.api.is_queue_running() is False
    finally:
        container.shutdown()


def test_join_limit_above_40_is_preserved_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "join-limit.db"))
    monkeypatch.setenv("MAX_JOINS_PER_HOUR", "125")
    config = Config()
    assert config.max_joins_per_hour == 125

    db = Database(config.database_path)
    db.upsert_saved_dialog(
        {
            "peer_id": 1001,
            "username": "public_group",
            "title": "Public group",
            "kind": "group",
        },
        account_id=1,
    )
    campaign = db.create_join_campaign(77, max_per_hour=125, rng=random.Random(1))
    assert int(campaign["max_per_hour"]) == 125


def test_join_schedule_has_no_hidden_90_second_or_40_per_hour_floor():
    start = utc_now()

    class MinimumRandom:
        def uniform(self, low, high):
            return float(low)

    slots = generate_join_slots(
        start,
        3,
        rng=MinimumRandom(),
        max_per_hour=120,
    )
    assert len(slots) == 3
    assert slots[1] - slots[0] == timedelta(seconds=30)
    assert slots[2] - slots[1] == timedelta(seconds=30)

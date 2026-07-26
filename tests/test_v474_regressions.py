from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

import core.campaign_schedule as campaign_schedule
from core.campaign_schedule import from_db_time, to_db_time
from core.secret_store import SecretStore
from services.api import ServiceAPI
from storage.database import Database

UTC = timezone.utc
_QT_APP: QApplication | None = None


def _qt_app() -> QApplication:
    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    return _QT_APP


def _add_linked_channels(db: Database, count: int) -> None:
    for index in range(1, count + 1):
        db.insert_channel(
            {
                "channel_id": 50_000 + index,
                "title": f"Канал {index}",
                "username": f"v474_channel_{index}",
                "linked_chat_id": 60_000 + index,
            }
        )


@pytest.mark.parametrize("daily_limit", [7, 137, 1000])
def test_redistribution_keeps_exact_capacity_at_window_start(tmp_path, daily_limit):
    db = Database(tmp_path / f"density-{daily_limit}.db")
    start = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=daily_limit,
        slot_count=daily_limit,
        duration_hours=24,
        continuous=False,
        start_at=start,
    )

    moved = db.redistribute_pending_comment_slots(
        campaign["id"], now=start, grace_seconds=0, force=True
    )
    summary = db.get_comment_schedule_summary(campaign["id"])

    assert moved == daily_limit
    assert summary["counts"].get("pending") == daily_limit
    assert summary["counts"].get("missed", 0) == 0


def test_continuous_campaign_stays_active_after_last_slot(tmp_path):
    db = Database(tmp_path / "continuous-active.db")
    _add_linked_channels(db, 1)
    start = datetime.now(UTC)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=1000,
        slot_count=1,
        duration_hours=24,
        continuous=True,
        start_at=start,
    )
    slot = db.get_comment_schedule(campaign["id"], limit=5)[0]
    due = from_db_time(slot["scheduled_at"]) + timedelta(seconds=1)
    queued = db.queue_due_comment_slot(now=due)

    assert queued is not None
    assert db.mark_comment_slot_running(slot["id"], queued["task_id"])
    assert db.finish_comment_slot(
        slot["id"],
        status="skipped",
        result="Проверено",
        channel_id=50_001,
        post_id=100,
    )

    state = db.get_comment_campaign(campaign["id"])
    assert state["status"] == "cycle_wait"
    assert db.get_active_comment_campaign()["id"] == campaign["id"]


def test_continuous_rollover_waits_for_full_batch_then_restarts(tmp_path):
    db = Database(tmp_path / "continuous-rollover.db")
    db.set_setting("telegram.account_id", 101)
    _add_linked_channels(db, 3)
    now = datetime.now(UTC).replace(microsecond=0)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=1000,
        slot_count=3,
        duration_hours=24,
        continuous=True,
        start_at=now - timedelta(hours=25),
    )
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE comment_schedule SET status='skipped', executed_at=CURRENT_TIMESTAMP "
            "WHERE campaign_id=?",
            (campaign["id"],),
        )
        conn.execute(
            "UPDATE comment_campaigns SET status='cycle_wait' WHERE id=?",
            (campaign["id"],),
        )
        conn.execute(
            "UPDATE channels SET last_comment_check_at=? WHERE channel_id IN (?, ?)",
            (to_db_time(now - timedelta(hours=25)), 50_001, 50_002),
        )
        conn.execute(
            "UPDATE channels SET last_comment_check_at=? WHERE channel_id=?",
            (to_db_time(now - timedelta(hours=23)), 50_003),
        )

    _qt_app()
    api = ServiceAPI(db)
    api._campaign_timer.stop()
    api._campaign_tick()

    waiting = db.get_comment_campaign(campaign["id"])
    assert waiting["status"] == "cycle_wait"
    assert db.get_latest_comment_campaign()["id"] == campaign["id"]

    with db.get_connection() as conn:
        conn.execute(
            "UPDATE channels SET last_comment_check_at=? WHERE channel_id=?",
            (to_db_time(now - timedelta(hours=25)), 50_003),
        )

    api._campaign_tick()
    latest = db.get_latest_comment_campaign()
    assert latest["id"] != campaign["id"]
    assert latest["status"] == "running"
    assert latest["continuous"] is True
    assert len(db.get_comment_schedule(latest["id"], limit=10)) == 3
    assert db.get_comment_campaign(campaign["id"])["status"] == "completed"
    api.prepare_shutdown()


def test_completed_continuous_campaign_from_previous_version_is_restarted(tmp_path):
    db = Database(tmp_path / "legacy-continuous-rollover.db")
    db.set_setting("telegram.account_id", 101)
    _add_linked_channels(db, 2)
    now = datetime.now(UTC).replace(microsecond=0)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=1000,
        slot_count=2,
        duration_hours=24,
        continuous=True,
        start_at=now - timedelta(hours=25),
    )
    assert db.complete_comment_campaign(campaign["id"], "Старое раннее завершение")

    _qt_app()
    api = ServiceAPI(db)
    api._campaign_timer.stop()
    api._campaign_tick()

    latest = db.get_latest_comment_campaign()
    assert latest["id"] != campaign["id"]
    assert latest["status"] == "running"
    assert len(db.get_comment_schedule(latest["id"], limit=10)) == 2
    api.prepare_shutdown()


def test_continuous_rollover_seed_322_creates_successor_without_scheduler_error(
    monkeypatch, tmp_path
):
    """Regression for the 1000/day float precision failure in production rollover."""
    db = Database(tmp_path / "continuous-rollover-seed-322.db")
    db.set_setting("telegram.account_id", 101)
    _add_linked_channels(db, 1)
    now = datetime.now(UTC).replace(microsecond=0)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=1000,
        slot_count=1,
        duration_hours=24,
        continuous=True,
        start_at=now - timedelta(hours=25),
        rng=random.Random(9876),
    )
    assert db.complete_comment_campaign(campaign["id"], "Цикл завершён")

    monkeypatch.setattr(
        campaign_schedule.random,
        "SystemRandom",
        lambda: random.Random(322),
    )

    _qt_app()
    api = ServiceAPI(db)
    api._campaign_timer.stop()
    try:
        api._campaign_tick()

        latest = db.get_latest_comment_campaign()
        active = db.get_active_comment_campaign()
        assert latest["id"] != campaign["id"]
        assert latest["status"] == "running"
        assert active is not None and active["id"] == latest["id"]
        assert len(db.get_comment_schedule(latest["id"], limit=10)) == 1
        assert db.get_comment_campaign(campaign["id"])["status"] == "completed"
        assert db.get_setting("scheduler.comment_error", "") == ""
    finally:
        api.prepare_shutdown()


class _BlockingMigrationDatabase(Database):
    def __init__(
        self, path, read_started: threading.Event, release_read: threading.Event
    ):
        self._read_started = read_started
        self._release_read = release_read
        self._blocked_once = False
        super().__init__(path)

    def get_setting(self, key, default=None):
        value = super().get_setting(key, default)
        if key == "telegram.api_hash" and not self._blocked_once:
            self._blocked_once = True
            self._read_started.set()
            if not self._release_read.wait(5):
                raise TimeoutError("migration test was not released")
        return value


class _ApiHashOnlyService(ServiceAPI):
    SECRET_SETTING_KEYS = frozenset({"telegram.api_hash"})


def test_secret_migration_cannot_overwrite_new_gui_value(tmp_path):
    read_started = threading.Event()
    release_read = threading.Event()
    db = _BlockingMigrationDatabase(
        tmp_path / "secret-race.db", read_started, release_read
    )
    db.set_setting("telegram.api_hash", "OLD")
    store = SecretStore(tmp_path / "secrets.json")
    _qt_app()
    api = _ApiHashOnlyService(db, secret_store=store)
    api._campaign_timer.stop()

    assert read_started.wait(5)

    def save_in_thread():
        try:
            api.save_settings({"telegram.api_hash": "NEW"})
        finally:
            api.close_thread_connection()

    writer = threading.Thread(target=save_in_thread, daemon=True)
    writer.start()
    time.sleep(0.05)
    assert writer.is_alive(), "writer should wait for the migration transaction"

    release_read.set()
    api._secret_migration_thread.join(5)
    writer.join(5)

    assert not writer.is_alive()
    assert store.get("telegram.api_hash") == "NEW"
    assert db.get_setting("telegram.api_hash") is None
    api.prepare_shutdown()


def test_start_queue_cannot_cancel_shutdown(tmp_path):
    worker = MagicMock()
    worker.isRunning.return_value = False
    _qt_app()
    api = ServiceAPI(Database(tmp_path / "shutdown.db"), queue_worker=worker)
    api._campaign_timer.stop()

    api.prepare_shutdown()
    assert api.start_queue() is False
    assert api._shutdown_requested is True
    worker.start.assert_not_called()

    api.cancel_shutdown()
    api._campaign_timer.stop()
    assert api.start_queue() is True
    worker.start.assert_called_once_with()
    api.prepare_shutdown()


def test_corrupted_fallback_secret_file_is_never_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "broken-secrets.json"
    original = "{this is not valid json"
    path.write_text(original, encoding="utf-8")
    store = SecretStore(path)

    assert store.get("telegram.api_hash", "missing") == "missing"
    with pytest.raises(RuntimeError, match="corrupted"):
        store.set("telegram.api_hash", "NEW")
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("continuous", "expected_status"),
    [(False, "completed"), (True, "cycle_wait")],
)
def test_reconcile_matches_normal_slot_completion_policy(
    tmp_path, continuous, expected_status
):
    db = Database(tmp_path / f"reconcile-{continuous}.db")
    start = datetime.now(UTC)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=1,
        slot_count=1,
        duration_hours=24,
        continuous=continuous,
        start_at=start,
    )
    slot = db.get_comment_schedule(campaign["id"], limit=5)[0]
    due = from_db_time(slot["scheduled_at"]) + timedelta(seconds=1)
    queued = db.queue_due_comment_slot(now=due)
    assert queued is not None
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', error='worker stopped' WHERE id=?",
            (queued["task_id"],),
        )

    assert db.reconcile_comment_schedule() == 1
    state = db.get_comment_campaign(campaign["id"])
    assert state["status"] == expected_status
    assert state["attempted_count"] == 1


def test_resuming_expired_finite_campaign_preserves_slots_without_new_cycle(tmp_path):
    db = Database(tmp_path / "finite-resume.db")
    _add_linked_channels(db, 2)
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=2,
        slot_count=2,
        duration_hours=24,
        continuous=False,
        start_at=datetime.now(UTC) - timedelta(hours=25),
    )
    assert db.pause_comment_campaign(campaign["id"])
    _qt_app()
    api = ServiceAPI(db)
    api._campaign_timer.stop()

    assert api.resume_comment_campaign() is True
    latest = db.get_latest_comment_campaign()
    assert latest["id"] == campaign["id"]
    assert latest["status"] == "running"
    assert from_db_time(latest["ends_at"]) > datetime.now(UTC)
    rows = db.get_comment_schedule(campaign["id"], limit=5)
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"pending"}
    api.prepare_shutdown()

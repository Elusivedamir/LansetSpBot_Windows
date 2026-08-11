from __future__ import annotations

import random
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.campaign_schedule import to_db_time, utc_now
from services.api import ServiceAPI
from services.import_service import ImportService, ImportValidationError
from storage.database import Database
from workers.handlers.join_slot import create_join_slot_handler
from workers.queue_worker import QueueWorker


class _Secrets:
    def get(self, _key, default=""):
        return default

    def set(self, _key, _value):
        return None


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.mark.asyncio
async def test_join_slot_accepts_real_negative_telethon_marked_peer_id() -> None:
    context = {
        "campaign_status": "running",
        "status": "queued",
        "saved_dialog_id": 7,
        "account_id": 9,
        "peer_id": -1001234567890,
        "username": "real_channel",
        "invite_link": None,
        "title": "Real channel",
        "max_per_hour": 40,
    }
    db = MagicMock()
    db.get_join_slot_context.side_effect = [
        context,
        {**context, "status": "running"},
    ]
    db.get_join_campaign.return_value = {"status": "running"}
    db.mark_join_slot_running.return_value = True
    db.get_join_guard.return_value = {"allowed": True, "wait_seconds": 0}
    queue_worker = SimpleNamespace(is_scope_cancelled=lambda *_args: False)
    telegram = SimpleNamespace(
        is_member=AsyncMock(return_value=False),
        join_saved_dialog=AsyncMock(return_value=True),
    )
    handler = create_join_slot_handler(
        as_int=lambda value, default=0: int(value) if value is not None else default,
        queue_worker=queue_worker,
        config=SimpleNamespace(min_join_interval_seconds=45),
        worker_db=db,
        telegram=telegram,
        set_runtime=lambda *_args, **_kwargs: None,
    )

    await handler({"id": 11, "payload": {"campaign_id": 3, "slot_id": 5}})

    db.mark_join_slot_running.assert_called_once_with(5, 11)
    telegram.join_saved_dialog.assert_awaited_once_with(
        username="real_channel",
        invite_link=None,
        expected_peer_id=-1001234567890,
    )
    db.record_join_event.assert_called_once_with(
        -1001234567890,
        "joined",
        campaign_id=3,
        saved_dialog_id=7,
        account_id=9,
    )


def test_sent_direct_group_receipt_reconciles_slot_and_cooldown(tmp_path) -> None:
    db = Database(tmp_path / "direct-reconcile.db")
    channel_id = -1001234567890
    db.insert_channel(
        {"channel_id": channel_id, "linked_chat_id": None, "title": "Group"}
    )
    now = utc_now()
    campaign = db.create_comment_campaign(
        ["Комментарий"],
        daily_limit=1,
        slot_count=1,
        duration_hours=24,
        continuous=False,
        start_at=now - timedelta(hours=1),
        rng=random.Random(15),
    )
    slot = db.get_comment_schedule(campaign["id"], limit=2)[0]
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE comment_schedule SET scheduled_at=? WHERE id=?",
            (to_db_time(now - timedelta(minutes=1)), int(slot["id"])),
        )
    queued = db.queue_due_comment_slot(now=now)
    assert queued is not None
    task = db.claim_next_pending_task()
    assert task is not None
    assert db.mark_comment_slot_running(slot["id"], task["id"])
    assert db.bind_comment_slot_target(slot["id"], task["id"], channel_id=channel_id)
    assert db.reserve_direct_message_delivery(task["id"], channel_id, "Комментарий")
    assert db.finalize_direct_message_delivery(task["id"], message_id=999)
    assert db.set_failed(task["id"], "simulated finalization crash", retry=False)

    assert db.reconcile_comment_schedule() == 1

    refreshed_slot = db.get_comment_schedule(campaign["id"], limit=2)[0]
    refreshed_campaign = db.get_comment_campaign(campaign["id"])
    refreshed_channel = db.get_channel_by_id(channel_id)
    history = db.get_comment_history(campaign_id=campaign["id"])
    assert refreshed_slot["status"] == "sent"
    assert refreshed_campaign["attempted_count"] == 1
    assert refreshed_campaign["sent_count"] == 1
    assert refreshed_channel["last_comment_check_at"] is not None
    assert len(history) == 1
    assert history[0]["slot_id"] == slot["id"]
    assert db.get_task(task["id"])["status"] == "completed"
    assert db.count_channels_for_commenting(cooldown_hours=24) == 0


@pytest.mark.asyncio
async def test_queue_worker_stops_after_five_consecutive_processing_failures(
    monkeypatch,
) -> None:
    worker = QueueWorker(lambda: {})
    calls = 0

    class _DB:
        def claim_next_pending_task(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {"id": calls, "type": "boom"}

        def seconds_until_next_pending_task(self):
            return None

    async def fail(_task):
        raise RuntimeError("boom")

    async def no_wait(*_args, **_kwargs):
        return True

    worker._db = _DB()  # type: ignore[assignment]
    monkeypatch.setattr(worker, "_process_task", fail)
    monkeypatch.setattr(worker, "safe_sleep", no_wait)

    await worker._run_async()

    assert calls == 5


def test_successful_scheduler_tick_clears_previous_failure_counter(
    tmp_path, monkeypatch
) -> None:
    db = Database(tmp_path / "scheduler-reset.db")
    db.set_setting("telegram.account_id", 101)
    api = ServiceAPI(db, secret_store=_Secrets())
    api._campaign_timer.stop()
    api._delivery_recovery_timer.stop()
    api._scheduler_failures = 2
    api._scheduler_error_present = True
    db.set_setting("scheduler.comment_error", "old error")
    monkeypatch.setattr(
        "services.multiaccount_scheduler.run_multiaccount_campaign_tick",
        lambda _root: {},
    )

    api._campaign_tick()

    assert api._scheduler_failures == 0
    assert db.get_setting("scheduler.comment_error", "") == ""
    api.prepare_shutdown()


def test_limit_timer_is_cancelled_before_campaign_becomes_active() -> None:
    from gui.views.commenting_view import CommentingView

    _app()
    calls: list[tuple[int, bool]] = []
    state = {"active": False}

    class _Adapter:
        def get_current_account_id(self):
            return 1

        def get_comment_daily_limit(self, **_kwargs):
            return 40

        def get_main_comments(self):
            return ["Комментарий", "", "", "", ""]

        def get_comment_campaign_state(self):
            return None

        def get_channels(self):
            return [{"channel_id": 1, "title": "Group"}]

        def get_commenting_channels(self):
            return [{"channel_id": 1, "title": "Group"}]

        def set_comment_daily_limit(self, value, **_kwargs):
            calls.append((int(value), bool(state["active"])))
            if state["active"]:
                raise ValueError("active campaign")
            return int(value)

        def save_comment_template(self, _comments):
            return None

        def start_comment_campaign(self, _comments, **_kwargs):
            state["active"] = True
            return {"id": 1}

        def get_comment_history(self, **_kwargs):
            return []

    view = CommentingView(_Adapter())
    view.daily_limit_slider.setValue(41)
    assert view.limit_save_timer.isActive()
    view.refresh_campaign = lambda: None  # type: ignore[method-assign]

    view.start_campaign()
    QTest.qWait(350)

    assert calls == [(41, False)]
    assert not view.limit_save_timer.isActive()
    view.deleteLater()


def test_large_csv_import_is_streamed_but_still_atomic(tmp_path) -> None:
    db = Database(tmp_path / "streaming-import.db")
    service = ImportService(db)
    service.IMPORT_BATCH_SIZE = 10
    path = tmp_path / "channels.csv"
    rows = ["channel_id,title"]
    rows.extend(f"{index},Channel {index}" for index in range(1, 26))
    rows.append("not-an-id,Broken")
    path.write_text("\n".join(rows), encoding="utf-8")

    with pytest.raises(ImportValidationError, match="row 27"):
        service.import_file("channels", path)

    assert db.get_channels() == []


def test_cancelled_scope_registry_prunes_old_campaign_ids(monkeypatch) -> None:
    import workers.queue_worker as queue_module

    worker = QueueWorker(lambda: {})
    worker._cancelled_scope_retention_seconds = 10
    moments = iter([100.0, 120.0])
    last_moment = [120.0]

    def monotonic():
        try:
            last_moment[0] = next(moments)
        except StopIteration:
            pass
        return last_moment[0]

    monkeypatch.setattr(queue_module.time, "monotonic", monotonic)

    worker.request_scope_cancellation("comment_campaign", 1)
    assert not worker.is_scope_cancelled("comment_campaign", 2)
    assert ("comment_campaign", 1) not in worker._cancelled_scopes


def test_successful_container_shutdown_closes_gui_thread_connection() -> None:
    from core.composition import ApplicationContainer

    container = object.__new__(ApplicationContainer)
    container.api = MagicMock()
    container.queue_worker = MagicMock()
    container.database = MagicMock()
    container.queue_worker.isRunning.return_value = True
    container.queue_worker.stop.return_value = True

    assert container.shutdown(timeout_ms=25) is True

    container.api.prepare_shutdown.assert_called_once_with()
    container.queue_worker.stop.assert_called_once_with(25)
    container.database.finalize_shutdown.assert_called_once_with()


def test_new_campaign_start_reconciles_sent_receipt_before_active_guard(
    tmp_path,
) -> None:
    db = Database(tmp_path / "start-reconcile.db")
    db.set_setting("telegram.account_id", 101)
    channel_id = -1009876543210
    db.insert_channel({"channel_id": channel_id, "title": "Recovered group"})
    now = utc_now()
    campaign = db.create_comment_campaign(
        ["Первый"],
        daily_limit=1,
        slot_count=1,
        duration_hours=24,
        continuous=False,
        start_at=now - timedelta(hours=1),
        rng=random.Random(16),
    )
    slot = db.get_comment_schedule(campaign["id"], limit=2)[0]
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE comment_schedule SET scheduled_at=? WHERE id=?",
            (to_db_time(now - timedelta(minutes=1)), int(slot["id"])),
        )
    queued = db.queue_due_comment_slot(now=now)
    assert queued is not None
    task = db.claim_next_pending_task()
    assert task is not None
    assert db.mark_comment_slot_running(slot["id"], task["id"])
    assert db.bind_comment_slot_target(slot["id"], task["id"], channel_id=channel_id)
    assert db.reserve_direct_message_delivery(task["id"], channel_id, "Первый")
    assert db.finalize_direct_message_delivery(task["id"], message_id=1001)
    assert db.set_failed(task["id"], "simulated local finalizer failure", retry=False)

    api = ServiceAPI(db, secret_store=_Secrets())
    api._campaign_timer.stop()
    api._delivery_recovery_timer.stop()
    with pytest.raises(ValueError) as exc_info:
        api.start_comment_campaign(["Второй"], continuous=False)

    assert "Кампания уже запущена" not in str(exc_info.value)
    assert db.get_comment_campaign(campaign["id"])["status"] == "completed"
    assert db.get_comment_schedule(campaign["id"], limit=2)[0]["status"] == "sent"
    api.prepare_shutdown()

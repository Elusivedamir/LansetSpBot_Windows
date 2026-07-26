from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from telethon.errors import UserAlreadyParticipantError
from PySide6.QtWidgets import QApplication

from core.version import __version__
from gui.auth_worker import TelegramAuthWorker
from services.api import ServiceAPI
from services.telegram_service import TelegramService
from storage.database import Database


class _Limiter:
    async def acquire(self):
        return None


@pytest.mark.asyncio
async def test_auth_network_wait_has_a_finite_timeout(tmp_path):
    worker = TelegramAuthWorker(
        mode="request_code",
        settings={},
        session_dir=tmp_path,
    )
    worker.NETWORK_TIMEOUT_SECONDS = 0.01
    with pytest.raises(TimeoutError):
        await worker._await_interruptible(asyncio.sleep(1))


@pytest.mark.asyncio
async def test_auth_wait_cancels_when_thread_interruption_is_requested(tmp_path):
    class InterruptedWorker(TelegramAuthWorker):
        def isInterruptionRequested(self):  # noqa: N802 - Qt API
            return True

    worker = InterruptedWorker(mode="request_code", settings={}, session_dir=tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await worker._await_interruptible(asyncio.sleep(1))


@pytest.mark.asyncio
async def test_auth_retries_one_shot_telethon_request_failure(tmp_path):
    worker = TelegramAuthWorker(
        mode="request_code",
        settings={},
        session_dir=tmp_path,
    )
    worker.TRANSIENT_RETRY_DELAY_SECONDS = 0
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ValueError("Request was unsuccessful 1 time(s)")
        return "ok"

    assert await worker._with_transient_retries(operation) == "ok"
    assert attempts == 3


@pytest.mark.asyncio
async def test_invite_only_already_member_is_not_reported_as_new_join():
    class Client:
        def is_connected(self):
            return True

        async def __call__(self, request):
            raise UserAlreadyParticipantError(request)

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = _Limiter()
    service._connected = True
    service._status_callback = None

    newly_joined = await service.join_saved_dialog(invite_link="https://t.me/+abc123")
    assert newly_joined is False


def test_campaign_states_use_aggregate_queries(tmp_path, monkeypatch):
    db = Database(tmp_path / "aggregate.db")
    # Join campaigns are account-scoped; the summary is read for the currently
    # selected account, so the fixture must name one.
    db.set_setting("telegram.account_id", 2)
    for index in range(90):
        db.upsert_saved_dialog(
            {
                "peer_id": 10_000 + index,
                "username": f"aggregate_{index}",
                "title": f"Aggregate {index}",
                "kind": "channel",
            },
            account_id=1,
        )
    db.create_join_campaign(2, max_per_hour=40)
    api = ServiceAPI(db)
    monkeypatch.setattr(
        db,
        "get_join_schedule",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("full schedule must not be loaded for a status summary")
        ),
    )

    state = api.get_join_campaign_state()

    assert state is not None
    assert sum(state["schedule_counts"].values()) == 90
    assert state["next_scheduled_at"] is not None
    api.prepare_shutdown()


def test_release_version_is_consistent_in_build_metadata():
    root = Path(__file__).resolve().parents[1]
    assert __version__ == "4.8.0"
    spec = (root / "build" / "LansetSpBot.windows.spec").read_text(encoding="utf-8")
    build_script = (root / "build" / "build_windows_x64.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "4.7.0" not in spec
    assert "4.7.0" not in build_script


def test_non_idempotent_tasks_are_created_without_automatic_retries(tmp_path):
    db = Database(tmp_path / "retry-policy.db")
    db.set_setting("telegram.account_id", 101)
    api = ServiceAPI(db)

    created = api.create_task(
        "comment",
        {"channel_id": 123, "post_id": 456, "text": "hello"},
        max_retries=9,
    )

    persisted = db.get_task(created["id"])
    assert persisted is not None
    assert persisted["max_retries"] == 0
    api.prepare_shutdown()


@pytest.mark.asyncio
async def test_worker_never_retries_non_idempotent_generic_failure(tmp_path):
    from workers.queue_worker import QueueWorker

    db = Database(tmp_path / "worker-retry.db")
    task_id = db.insert_task(
        "direct_message", {"chat_id": 123, "text": "hello"}, max_retries=3
    )
    task = db.claim_next_pending_task()
    assert task is not None

    async def fail(_task):
        raise RuntimeError("unknown external result")

    worker = QueueWorker(lambda: {})
    worker._db = db
    worker._handlers = {"direct_message": fail}
    await worker._process_task(task)

    persisted = db.get_task(task_id)
    assert persisted is not None
    assert persisted["status"] == "failed"
    assert persisted["retry_count"] == 0


def test_cancel_shutdown_reactivates_campaign_scheduler(tmp_path):
    app = QApplication.instance() or QApplication([])
    api = ServiceAPI(Database(tmp_path / "cancel-shutdown.db"))
    api.prepare_shutdown()
    assert api._shutdown_requested is True
    assert api._campaign_timer.isActive() is False

    api.cancel_shutdown()

    assert api._shutdown_requested is False
    assert api._campaign_timer.isActive() is True
    api.prepare_shutdown()
    assert app is not None

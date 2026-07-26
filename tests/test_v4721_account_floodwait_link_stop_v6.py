from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from core.campaign_schedule import from_db_time, utc_now
from core.exceptions import DeferredTelegramError, TaskPausedError
from storage.database import Database
from tests.test_composition_resilience import _Linked, _Telegram, _handlers
from workers.queue_worker import QueueWorker


@pytest.mark.asyncio
async def test_floodwait_blocks_every_due_rpc_for_same_account(tmp_path):
    database = Database(tmp_path / "account-floodwait.db")
    database.set_setting("telegram.account_id", 77)
    link_id = database.insert_task("link_channels", {"account_id": 77})
    sync_id = database.insert_task("sync_channels", {"account_id": 77})

    first = database.claim_next_pending_task()
    assert first and first["id"] == link_id

    calls: list[str] = []

    async def flood(_task):
        raise DeferredTelegramError(
            "Telegram FloodWait",
            code="flood_wait_deferred",
            retry_after=180,
        )

    async def must_not_run(_task):
        calls.append("rpc")

    worker = QueueWorker(lambda: {})
    worker._db = database
    worker._handlers = {
        "link_channels": flood,
        "sync_channels": must_not_run,
    }

    await worker._process_task(first)
    cooldown = database.get_account_rpc_cooldown(account_id=77)
    assert cooldown["active"] == 1
    cooldown_until = from_db_time(cooldown["next_allowed_at"])
    assert cooldown_until is not None and cooldown_until > utc_now()

    second = database.claim_next_pending_task()
    assert second and second["id"] == sync_id
    await worker._process_task(second)

    assert calls == []
    stored = database.get_task(sync_id)
    assert stored and stored["status"] == "pending"
    assert from_db_time(stored["not_before"]) == cooldown_until


def test_only_one_active_link_task_exists_per_account(tmp_path):
    database = Database(tmp_path / "one-link-task.db")
    database.set_setting("telegram.account_id", 77)

    first, created_first = database.create_or_get_link_task(
        account_id=77,
        payload={"account_id": 77},
        max_retries=3,
    )
    second, created_second = database.create_or_get_link_task(
        account_id=77,
        payload={"account_id": 77},
        max_retries=3,
    )

    assert created_first is True
    assert created_second is False
    assert int(first["id"]) == int(second["id"])

    assert database.pause_pending_link_task(int(first["id"]))
    third, created_third = database.create_or_get_link_task(
        account_id=77,
        payload={"account_id": 77},
        max_retries=3,
    )
    assert created_third is False
    assert int(third["id"]) == int(first["id"])
    assert third["status"] == "paused"


@pytest.mark.asyncio
async def test_user_stop_pauses_link_task_after_checkpoint(tmp_path):
    database = Database(tmp_path / "pause-link-task.db")
    database.set_setting("telegram.account_id", 77)
    task_id = database.insert_task("link_channels", {"account_id": 77})
    task = database.claim_next_pending_task()
    assert task and task["id"] == task_id

    async def checkpoint_then_pause(_task):
        assert database.update_task_checkpoint(
            task_id,
            {
                "account_id": 77,
                "_link_checkpoint": {
                    "version": 1,
                    "account_id": 77,
                    "channel_ids": [1, 2, 3],
                    "group_ids": [],
                    "channel_index": 1,
                    "group_index": 0,
                },
            },
            33,
        )
        raise TaskPausedError("Остановлено пользователем; прогресс связок сохранён")

    worker = QueueWorker(lambda: {})
    worker._db = database
    worker._handlers = {"link_channels": checkpoint_then_pause}
    await worker._process_task(task)

    stored = database.get_task(task_id)
    assert stored and stored["status"] == "paused"
    assert stored["progress"] == 33
    assert "прогресс связок сохранён" in str(stored["error"])


@pytest.mark.asyncio
async def test_new_link_pass_skips_targets_checked_once(monkeypatch):
    database = MagicMock()
    database.get_setting.return_value = 77
    database.get_channels.return_value = [
        {
            "channel_id": 1,
            "title": "Already checked",
            "target_kind": "channel",
            "link_checked_at": "2026-07-18 04:00:00",
            "link_status": "Нет чата обсуждения",
        },
        {
            "channel_id": 2,
            "title": "New",
            "target_kind": "channel",
            "link_checked_at": None,
        },
    ]

    class TrackingLinked(_Linked):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def get_linked_chat_id(self, channel_id):
            self.calls.append(int(channel_id))
            return await super().get_linked_chat_id(channel_id)

    linked = TrackingLinked()
    linked.links = {1: None, 2: None}
    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch, database, _Telegram(), linked=linked
    )

    await handlers["link_channels"]({"id": 91, "payload": {"account_id": 77}})

    assert linked.calls == [2]
    database.update_channel_link.assert_called_once()
    assert database.update_channel_link.call_args.args[0] == 2
    database.mark_link_checked.assert_called_with(2, account_id=77)


def test_account_cooldown_only_extends_never_shortens(tmp_path):
    database = Database(tmp_path / "cooldown-max.db")
    database.set_setting("telegram.account_id", 77)
    long_wait = utc_now() + timedelta(seconds=600)
    short_wait = utc_now() + timedelta(seconds=60)

    first = database.set_account_rpc_cooldown(
        account_id=77,
        retry_at=long_wait,
        source_task_id=1,
    )
    second = database.set_account_rpc_cooldown(
        account_id=77,
        retry_at=short_wait,
        source_task_id=2,
    )

    assert second["next_allowed_at"] == first["next_allowed_at"]
    assert int(second["source_task_id"]) == 1


def test_stop_during_deferred_floodwait_keeps_deadline_and_persists_request(tmp_path):
    database = Database(tmp_path / "deferred-stop.db")
    database.set_setting("telegram.account_id", 77)
    task_id = database.insert_task("link_channels", {"account_id": 77})
    claimed = database.claim_next_pending_task()
    assert claimed and claimed["id"] == task_id

    retry_at = utc_now() + timedelta(seconds=300)
    assert (
        database.defer_task(
            task_id,
            retry_at=retry_at,
            error="flood_wait_deferred: Telegram FloodWait",
        )
        == "deferred"
    )
    before = database.get_task(task_id)
    assert before and before["status"] == "pending"

    assert database.request_link_task_pause(task_id) == "waiting"
    after = database.get_task(task_id)
    assert after and after["status"] == "pending"
    assert after["not_before"] == before["not_before"]
    payload = database._decode_task_payload(after["payload"])
    assert payload["_link_pause_requested"] is True
    assert database.claim_next_pending_task() is None


@pytest.mark.asyncio
async def test_due_stopped_link_task_pauses_before_any_new_rpc(tmp_path):
    database = Database(tmp_path / "due-stop.db")
    database.set_setting("telegram.account_id", 77)
    task_id = database.insert_task("link_channels", {"account_id": 77})
    claimed = database.claim_next_pending_task()
    assert claimed and claimed["id"] == task_id
    assert (
        database.defer_task(
            task_id,
            retry_at=utc_now() + timedelta(seconds=300),
            error="flood_wait_deferred: Telegram FloodWait",
        )
        == "deferred"
    )
    assert database.request_link_task_pause(task_id) == "waiting"

    with database.get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET not_before=datetime('now','-1 second') WHERE id=?",
            (task_id,),
        )

    due = database.claim_next_pending_task()
    assert due and due["id"] == task_id
    calls: list[str] = []

    async def must_not_run(_task):
        calls.append("rpc")

    worker = QueueWorker(lambda: {})
    worker._db = database
    worker._handlers = {"link_channels": must_not_run}
    await worker._process_task(due)

    assert calls == []
    stored = database.get_task(task_id)
    assert stored and stored["status"] == "paused"
    payload = database._decode_task_payload(stored["payload"])
    assert "_link_pause_requested" not in payload


def test_checkpoint_cannot_erase_concurrent_stop_request(tmp_path):
    database = Database(tmp_path / "checkpoint-stop.db")
    database.set_setting("telegram.account_id", 77)
    task_id = database.insert_task("link_channels", {"account_id": 77})
    claimed = database.claim_next_pending_task()
    assert claimed and claimed["id"] == task_id

    assert database.request_link_task_pause(task_id) == "requested"
    assert database.update_task_checkpoint(
        task_id,
        {
            "account_id": 77,
            "_link_checkpoint": {
                "version": 1,
                "account_id": 77,
                "channel_ids": [1, 2],
                "group_ids": [],
                "channel_index": 1,
                "group_index": 0,
            },
        },
        50,
    )
    stored = database.get_task(task_id)
    assert stored
    payload = database._decode_task_payload(stored["payload"])
    assert payload["_link_pause_requested"] is True
    assert payload["_link_checkpoint"]["channel_index"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_name", ["FloodPremiumWaitError", "FloodTestPhoneWaitError"]
)
async def test_additional_timed_flood_errors_use_protected_account_wait(
    monkeypatch, error_name
):
    from telethon import errors
    from services.telegram_service import TelegramService

    class Client:
        def is_connected(self):
            return True

    class Limiter:
        async def acquire(self):
            return None

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = None

    error_type = getattr(errors, error_name)

    async def operation():
        raise error_type(None, capture=9)

    monkeypatch.setattr("services.telegram_service.random.randint", lambda _a, _b: 37)
    with pytest.raises(DeferredTelegramError) as raised:
        await service.execute(operation)
    assert raised.value.code == "flood_wait_deferred"
    assert raised.value.retry_after == 46


@pytest.mark.asyncio
async def test_generic_flood_without_retry_interval_stops_account_activity():
    from telethon.errors import FloodError
    from services.telegram_service import TelegramService
    from core.exceptions import NonRetryableTelegramError

    class Client:
        def is_connected(self):
            return True

    class Limiter:
        async def acquire(self):
            return None

    service = object.__new__(TelegramService)
    service.client = Client()
    service.limiter = Limiter()
    service._connected = True
    service._status_callback = None

    async def operation():
        raise FloodError(None, "FLOOD")

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.execute(operation)
    assert raised.value.code == "peer_flood"


@pytest.mark.asyncio
async def test_stopped_after_floodwait_resumes_only_after_explicit_continue(tmp_path):
    database = Database(tmp_path / "explicit-link-continue.db")
    database.set_setting("telegram.account_id", 77)
    task_id = database.insert_task("link_channels", {"account_id": 77})
    claimed = database.claim_next_pending_task()
    assert claimed and claimed["id"] == task_id

    assert database.update_task_checkpoint(
        task_id,
        {
            "account_id": 77,
            "_link_checkpoint": {
                "version": 1,
                "account_id": 77,
                "channel_ids": [1, 2],
                "group_ids": [],
                "channel_index": 1,
                "group_index": 0,
            },
        },
        50,
    )
    assert (
        database.defer_task(
            task_id,
            retry_at=utc_now() + timedelta(seconds=300),
            error="flood_wait_deferred: Telegram FloodWait",
        )
        == "deferred"
    )
    assert database.request_link_task_pause(task_id) == "waiting"

    # Simulate expiration of Telegram's FloodWait plus the safety buffer.
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET not_before=datetime('now','-1 second') WHERE id=?",
            (task_id,),
        )

    due = database.claim_next_pending_task()
    assert due and due["id"] == task_id
    calls: list[str] = []

    async def handler(_task):
        calls.append("rpc")

    worker = QueueWorker(lambda: {})
    worker._db = database
    worker._handlers = {"link_channels": handler}
    await worker._process_task(due)

    paused = database.get_task(task_id)
    assert paused and paused["status"] == "paused"
    assert paused["progress"] == 50
    assert calls == []
    assert database.claim_next_pending_task() is None

    # Only the explicit Continue action makes the saved task runnable again.
    assert database.resume_link_task(task_id) is True
    resumed = database.claim_next_pending_task()
    assert resumed and resumed["id"] == task_id
    await worker._process_task(resumed)

    assert calls == ["rpc"]
    completed = database.get_task(task_id)
    assert completed and completed["status"] == "completed"

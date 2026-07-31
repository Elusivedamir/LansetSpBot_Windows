from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from workers.handlers.join_slot import create_join_slot_handler


class _Database:
    def __init__(self) -> None:
        self.context = {
            "campaign_status": "running",
            "status": "queued",
            "saved_dialog_id": 7,
            "account_id": 9,
            "peer_id": -1001234567890,
            "username": "target",
            "invite_link": None,
            "title": "Target",
            "max_per_hour": 40,
        }
        self.finalized: dict[str, Any] | None = None
        self.bans: list[tuple[int, int, str]] = []
        self.deferred: list[dict[str, Any]] = []
        self.restricted = False

    def get_account_restriction(self, account_id: int) -> dict[str, Any]:
        return {"active": self.restricted, "account_id": account_id}

    def get_join_slot_context(self, _campaign_id: int, _slot_id: int):
        return dict(self.context)

    def get_join_campaign(self, _campaign_id: int):
        return {"status": self.context["campaign_status"]}

    def mark_join_slot_running(self, _slot_id: int, _task_id: int) -> bool:
        self.context["status"] = "running"
        return True

    def get_join_guard(self, **_kwargs):
        return {"allowed": True, "wait_seconds": 0}

    def is_channel_locally_banned(self, _peer_id: int, *, account_id: int) -> bool:
        return any(owner == account_id for _peer, owner, _reason in self.bans)

    def ban_peer_locally(
        self, peer_id: int, reason: str, *, account_id: int
    ) -> bool:
        self.bans.append((peer_id, account_id, reason))
        return True

    def defer_join_slot_and_set_network_wait(self, *args, **kwargs) -> bool:
        self.deferred.append({"args": args, "kwargs": kwargs})
        return True

    def update_task_progress(self, _task_id: int, _progress: int) -> None:
        return None

    def finalize_join_slot_outcome(
        self, _task_id: int, _slot_id: int, **kwargs
    ) -> None:
        self.finalized = dict(kwargs)

    def stop_join_campaign(self, *_args, **_kwargs) -> None:
        return None

    def pause_join_campaign(self, *_args, **_kwargs) -> None:
        return None

    def defer_join_slot(self, *_args, **_kwargs) -> None:
        return None

    def cancel_join_slot(self, *_args, **_kwargs) -> None:
        return None


class _Queue:
    def __init__(self) -> None:
        self.cancelled_scopes: list[tuple[str, int]] = []

    def is_scope_cancelled(self, *_scope) -> bool:
        return False

    def cancel_scopes_and_run(self, _scopes, mutation):
        return mutation()

    def request_scope_cancellation(self, kind: str, value: int) -> None:
        self.cancelled_scopes.append((kind, value))


class _Telegram:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = 0

    async def join_saved_dialog(self, **_kwargs):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _handler(database: _Database, queue: _Queue, telegram: _Telegram):
    return create_join_slot_handler(
        as_int=lambda value, default=0: int(value) if value is not None else default,
        queue_worker=queue,
        config=SimpleNamespace(min_join_interval_seconds=45),
        worker_db=database,
        telegram=telegram,
        set_runtime=lambda *_args, **_kwargs: None,
    )


async def _run(result: Any):
    database = _Database()
    queue = _Queue()
    telegram = _Telegram(result)
    handler = _handler(database, queue, telegram)
    await handler({"id": 11, "payload": {"campaign_id": 3, "slot_id": 5}})
    return database, queue, telegram


@pytest.mark.asyncio
async def test_already_participant_is_success_without_join_event() -> None:
    database, _queue, telegram = await _run(False)

    assert telegram.calls == 1
    assert database.finalized is not None
    assert database.finalized["status"] == "already_member"
    assert database.finalized["joined"] is False
    assert database.finalized["membership_status"] == "member"
    assert database.finalized["join_event_peer_id"] is None


@pytest.mark.asyncio
async def test_join_request_is_not_active_membership() -> None:
    database, _queue, _telegram = await _run(
        NonRetryableTelegramError("requested", code="join_requested")
    )

    assert database.finalized is not None
    assert database.finalized["status"] == "join_requested"
    assert database.finalized["joined"] is False
    assert database.finalized["membership_status"] == "join_requested"


@pytest.mark.asyncio
async def test_unknown_join_result_is_banned_and_never_replayed() -> None:
    database, _queue, telegram = await _run(
        NonRetryableTelegramError("unknown", code="join_result_unknown")
    )

    assert telegram.calls == 1
    assert database.finalized is not None
    assert database.finalized["status"] == "uncertain"
    assert database.finalized["membership_status"] == "uncertain"
    assert database.bans == [
        (-1001234567890, 9, "Результат вступления неизвестен; цель локально заблокирована")
    ]


@pytest.mark.asyncio
async def test_network_unavailable_defers_without_final_delivery_outcome() -> None:
    database, _queue, telegram = await _run(
        NonRetryableTelegramError("down", code="network_unavailable")
    )

    assert telegram.calls == 1
    assert database.finalized is None
    assert len(database.deferred) == 1


@pytest.mark.asyncio
async def test_flood_wait_defers_slot_without_marking_membership() -> None:
    database = _Database()
    queue = _Queue()
    telegram = _Telegram(
        DeferredTelegramError(
            "wait",
            code="flood_wait_deferred",
            retry_after=73,
        )
    )
    database.set_account_rpc_cooldown = lambda **_kwargs: {"next_allowed_at": "x"}
    queue.remember_account_rpc_cooldown = lambda *_args: None

    await _handler(database, queue, telegram)(
        {"id": 11, "payload": {"campaign_id": 3, "slot_id": 5}}
    )

    assert telegram.calls == 1
    assert database.finalized is None
    assert len(database.deferred) == 1


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_is_uncertain_and_re_raised() -> None:
    database = _Database()
    queue = _Queue()
    telegram = _Telegram(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _handler(database, queue, telegram)(
            {"id": 11, "payload": {"campaign_id": 3, "slot_id": 5}}
        )

    assert telegram.calls == 1
    assert database.finalized is not None
    assert database.finalized["status"] == "uncertain"
    assert database.finalized["task_failed"] is True
    assert len(database.bans) == 1

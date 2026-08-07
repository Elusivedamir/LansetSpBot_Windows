from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.exceptions import DeferredTelegramError
from services.account_runtime_manager import (
    TelegramAccountRuntime,
    TelegramAccountRuntimeManager,
    create_multiaccount_handlers,
)
from workers.handlers.warmup_step import create_warmup_step_handler


class _RouterQueue:
    def __init__(self, database):
        self.database = database

    def get_db(self):
        return self.database

    def is_scope_cancelled(self, *_scope):
        return False


async def _run_warmup_router_scenario():
    worker_db = MagicMock()
    worker_db.get_telegram_account.return_value = {
        "authorized": True,
        "stopped": False,
        "runtime_state": "connected",
    }
    worker_db.get_account_restriction.return_value = None
    container = SimpleNamespace(queue_worker=_RouterQueue(worker_db))
    calls = []

    async def warmup_step(task):
        calls.append(task)
        return {"account_id": task["account_id"]}

    async def runtime_cleanup():
        return None

    def create_handlers(context, **_factories):
        assert context is container
        return {"warmup_step": warmup_step}, runtime_cleanup

    handlers, cleanup = create_multiaccount_handlers(
        container,
        create_worker_handlers=create_handlers,
        TelegramService=object,
        ImportService=object,
        LinkedChatService=object,
        CommentService=object,
    )
    try:
        result = await handlers["warmup_step"](
            {
                "id": 17,
                "account_id": 101,
                "type": "warmup_step",
                "payload": {"account_id": 101, "pair_id": 5},
            }
        )
    finally:
        await cleanup()
    return result, calls


def test_production_router_exposes_warmup_step() -> None:
    result, calls = asyncio.run(_run_warmup_router_scenario())
    assert result == {"account_id": 101}
    assert len(calls) == 1
    assert calls[0]["payload"]["account_id"] == 101


class _CooldownQueue:
    def __init__(self, remaining: int):
        self.remaining = int(remaining)
        self.calls = []

    def _account_rpc_cooldown_remaining(self, account_id, cooldown):
        self.calls.append((int(account_id), dict(cooldown or {})))
        return self.remaining


class _CooldownDatabase:
    def get_account_rpc_cooldown(self, *, account_id):
        assert int(account_id) == 101
        return {
            "next_allowed_at": "2099-01-01T00:00:00+00:00",
            "remaining_seconds": 42,
        }


async def _run_health_cooldown_scenario():
    queue = _CooldownQueue(42)
    container = SimpleNamespace(queue_worker=queue)
    database = _CooldownDatabase()
    health_calls = 0

    async def telegram_health(_task):
        nonlocal health_calls
        health_calls += 1
        return {"ok": True}

    manager = TelegramAccountRuntimeManager(
        container,
        worker_database=database,
        create_worker_handlers=lambda *_args, **_kwargs: {},
        TelegramService=object,
        ImportService=object,
        LinkedChatService=object,
        CommentService=object,
    )
    manager._runtimes[101] = TelegramAccountRuntime(
        account_id=101,
        handlers={"telegram_health": telegram_health},
        cleanup=None,
        lock=asyncio.Lock(),
    )

    with pytest.raises(DeferredTelegramError) as raised:
        await manager.check_runtime(101)

    return manager, queue, health_calls, raised.value


def test_runtime_health_honors_account_floodwait_embargo() -> None:
    manager, queue, health_calls, error = asyncio.run(
        _run_health_cooldown_scenario()
    )
    assert error.code == "account_flood_wait"
    assert error.retry_after == 42
    assert health_calls == 0
    assert queue.calls == [
        (
            101,
            {
                "next_allowed_at": "2099-01-01T00:00:00+00:00",
                "remaining_seconds": 42,
            },
        )
    ]
    assert manager._runtimes[101].reservations == 0

class _WarmupBarrierContext:
    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False


class _WarmupBarrier:
    def dispatch(self, _request=None):
        return _WarmupBarrierContext()


class _WarmupQueue:
    def __init__(self):
        self.barrier = _WarmupBarrier()

    def create_scope_dispatch_barrier(self, *scopes, pre_dispatch_check=None):
        assert scopes == (("warmup_pair", 5), ("account", 101))
        assert callable(pre_dispatch_check)
        assert pre_dispatch_check() is True
        return self.barrier

    def notify_task_available(self):
        return None


class _WarmupDatabase:
    def __init__(self):
        self.deferred = []
        self.failed = []

    def begin_warmup_step(self, step_id, *, account_id):
        assert (int(step_id), int(account_id)) == (303, 101)
        return {
            "owner_token": "0123456789abcdef0123456789abcdef",
            "action": "ensure_contact",
            "target_account_id": 202,
        }

    def acquire_account_activity_lease(self, account_id, **_kwargs):
        assert int(account_id) == 101
        return {"account_id": 101}

    def get_warmup_pair(self, pair_id):
        assert int(pair_id) == 5
        return {"status": "running"}

    def get_telegram_account(self, account_id):
        assert int(account_id) == 202
        return {"display_name": "Partner Account"}

    def defer_warmup_step(self, step_id, *, clear_queue_task):
        self.deferred.append((int(step_id), bool(clear_queue_task)))

    def fail_warmup_step(self, step_id, *, message, uncertain):
        self.failed.append((int(step_id), str(message), bool(uncertain)))


class _RawWarmupClient:
    def __init__(self):
        self.calls = 0

    async def __call__(self, _request):
        self.calls += 1
        raise AssertionError("warmup mutating RPC bypassed TelegramTransportMixin.execute")


class _WarmupTelegram:
    def __init__(self):
        self.client = _RawWarmupClient()
        self.execute_calls = []

    async def execute(self, method, *args, **kwargs):
        self.execute_calls.append((method, args, dict(kwargs)))
        raise DeferredTelegramError(
            "Telegram FloodWait",
            code="flood_wait_deferred",
            retry_after=211,
        )


async def _run_warmup_transport_scenario():
    queue = _WarmupQueue()
    database = _WarmupDatabase()
    telegram = _WarmupTelegram()
    handler = create_warmup_step_handler(
        queue_worker=queue,
        worker_db=database,
        telegram=telegram,
        set_runtime=lambda *_args, **_kwargs: None,
        publish_activity=lambda *_args, **_kwargs: None,
        contact_phone_provider=lambda _account_id: "+10000000000",
    )

    with pytest.raises(DeferredTelegramError) as raised:
        await handler(
            {
                "id": 17,
                "account_id": 101,
                "type": "warmup_step",
                "payload": {
                    "account_id": 101,
                    "pair_id": 5,
                    "step_id": 303,
                },
            }
        )
    return queue, database, telegram, raised.value


def test_warmup_mutating_rpc_uses_transport_and_preserves_floodwait() -> None:
    queue, database, telegram, error = asyncio.run(
        _run_warmup_transport_scenario()
    )
    assert error.code == "flood_wait_deferred"
    assert error.retry_after == 211
    assert telegram.client.calls == 0
    assert len(telegram.execute_calls) == 1
    method, _args, kwargs = telegram.execute_calls[0]
    assert method is telegram.client
    assert kwargs["retry_network"] is False
    assert kwargs["unknown_result_code"] == "warmup_contact_result_unknown"
    assert kwargs["dispatch_barrier"] is queue.barrier
    assert database.deferred == [(303, False)]
    assert database.failed == []

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.exceptions import DeferredTelegramError
from services.account_runtime_manager import (
    TelegramAccountRuntime,
    TelegramAccountRuntimeManager,
)


class RuntimeDatabase:
    def get_telegram_account(self, account_id: int):
        return {
            "telegram_account_id": account_id,
            "authorized": True,
            "runtime_state": "connected",
            "stopped": False,
        }

    def set_account_runtime_state(self, *_args, **_kwargs) -> None:
        return None


def _manager_with_fake_runtimes(monkeypatch, *, capacity: int = 2):
    manager = TelegramAccountRuntimeManager(
        SimpleNamespace(),
        worker_database=RuntimeDatabase(),
        create_worker_handlers=lambda *_args, **_kwargs: {},
        TelegramService=object,
        ImportService=object,
        LinkedChatService=object,
        CommentService=object,
    )
    manager.MAX_ACCOUNTS = capacity
    cleaned: list[int] = []

    async def create(account_id: int) -> TelegramAccountRuntime:
        async def cleanup() -> None:
            cleaned.append(account_id)

        return TelegramAccountRuntime(
            account_id=account_id,
            handlers={},
            cleanup=cleanup,
            lock=asyncio.Lock(),
            last_used=float(account_id),
        )

    monkeypatch.setattr(manager, "_create_runtime", create)
    return manager, cleaned


@pytest.mark.asyncio
async def test_oldest_idle_runtime_is_released_for_a_new_account(monkeypatch) -> None:
    manager, cleaned = _manager_with_fake_runtimes(monkeypatch)
    first = await manager.get_runtime(1)
    second = await manager.get_runtime(2)
    first.last_used = 1.0
    second.last_used = 2.0
    manager._release_runtime(first)
    manager._release_runtime(second)

    third = await manager.get_runtime(3)

    assert third.account_id == 3
    assert set(manager._runtimes) == {2, 3}
    assert cleaned == [1]
    manager._release_runtime(third)
    await manager.close()


@pytest.mark.asyncio
async def test_reserved_runtime_is_never_recycled_before_dispatch(monkeypatch) -> None:
    manager, _cleaned = _manager_with_fake_runtimes(monkeypatch)
    first = await manager.get_runtime(1)
    second = await manager.get_runtime(2)
    with pytest.raises(DeferredTelegramError) as captured:
        await manager.get_runtime(3)
    assert captured.value.code == "account_runtime_capacity"
    assert set(manager._runtimes) == {1, 2}
    manager._release_runtime(first)
    manager._release_runtime(second)
    await manager.close()


@pytest.mark.asyncio
async def test_busy_runtime_capacity_defers_instead_of_failing(monkeypatch) -> None:
    manager, _cleaned = _manager_with_fake_runtimes(monkeypatch)
    first = await manager.get_runtime(1)
    second = await manager.get_runtime(2)
    manager._release_runtime(first)
    manager._release_runtime(second)
    await first.lock.acquire()
    await second.lock.acquire()
    try:
        with pytest.raises(DeferredTelegramError) as captured:
            await manager.get_runtime(3)
        assert captured.value.code == "account_runtime_capacity"
        assert captured.value.retry_after == 1
        assert set(manager._runtimes) == {1, 2}
    finally:
        first.lock.release()
        second.lock.release()
        await manager.close()

# AUDIT-FIX: 70 active runtimes and cleanup ownership
@pytest.mark.asyncio
async def test_all_seventy_runtimes_can_remain_active(monkeypatch) -> None:
    manager, cleaned = _manager_with_fake_runtimes(monkeypatch, capacity=70)
    runtimes = [
        await manager.get_runtime(account_id)
        for account_id in range(1, 71)
    ]
    for runtime in runtimes:
        manager._release_runtime(runtime)

    assert set(manager._runtimes) == set(range(1, 71))
    assert cleaned == []

    await manager.close()
    assert len(cleaned) == 70


@pytest.mark.asyncio
async def test_failed_eviction_keeps_runtime_owned_until_explicit_stop(
    monkeypatch,
) -> None:
    manager, _cleaned = _manager_with_fake_runtimes(monkeypatch, capacity=1)
    first = await manager.get_runtime(1)
    manager._release_runtime(first)
    real_cleanup = manager._cleanup_runtime

    async def fail_cleanup(_runtime: TelegramAccountRuntime) -> None:
        raise RuntimeError("disconnect failed")

    monkeypatch.setattr(manager, "_cleanup_runtime", fail_cleanup)
    with pytest.raises(RuntimeError, match="disconnect failed"):
        await manager.get_runtime(2)

    assert manager._runtimes == {1: first}
    assert 1 in manager._evicting_accounts
    assert 2 not in manager._runtimes

    monkeypatch.setattr(manager, "_cleanup_runtime", real_cleanup)
    stopped = await manager.stop_runtime(1)
    assert stopped == {"account_id": 1, "disconnected": True}
    assert manager._runtimes == {}
    assert 1 not in manager._evicting_accounts

    second = await manager.get_runtime(2)
    manager._release_runtime(second)
    await manager.close()


@pytest.mark.asyncio
async def test_close_failure_retains_runtime_for_cleanup_retry(monkeypatch) -> None:
    manager, _cleaned = _manager_with_fake_runtimes(monkeypatch, capacity=1)
    first = await manager.get_runtime(1)
    manager._release_runtime(first)
    real_cleanup = manager._cleanup_runtime

    async def fail_cleanup(_runtime: TelegramAccountRuntime) -> None:
        raise RuntimeError("shutdown disconnect failed")

    monkeypatch.setattr(manager, "_cleanup_runtime", fail_cleanup)
    with pytest.raises(RuntimeError, match="shutdown disconnect failed"):
        await manager.close()

    assert manager._runtimes == {1: first}

    monkeypatch.setattr(manager, "_cleanup_runtime", real_cleanup)
    await manager.close()
    assert manager._runtimes == {}

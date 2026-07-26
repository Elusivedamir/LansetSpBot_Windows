from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from telethon.tl.functions.messages import SendMessageRequest

from core.exceptions import DeferredTelegramError
from services.telegram.transport import TelegramTransportMixin
from workers.queue_worker import QueueWorker


class _Limiter:
    pass


class _PacedClient:
    _marlen_request_pacing = True

    def __init__(self) -> None:
        self._observer = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    def is_connected(self):
        return True

    @contextmanager
    def observe_requests(self, observer):
        previous = self._observer
        self._observer = observer
        try:
            yield
        finally:
            self._observer = previous

    async def __call__(self, request):
        self.entered.set()
        await self.release.wait()
        dispatch_context = self._observer(request) if self._observer else None
        if dispatch_context is None:
            self.calls.append(request)
        else:
            with dispatch_context:
                self.calls.append(request)
        return SimpleNamespace(id=9001)


class _DeterministicQueueWorker(QueueWorker):
    def __init__(self) -> None:
        super().__init__(handler_factory=lambda: {})
        self._audit_interrupted = False

    def requestInterruption(self) -> None:  # deterministic Qt flag substitute
        self._audit_interrupted = True

    def isInterruptionRequested(self) -> bool:
        return self._audit_interrupted


class _Service(TelegramTransportMixin):
    AUTHORIZATION_RECHECK_SECONDS = 300.0
    FLOOD_WAIT_BUFFER_MIN_SECONDS = 30
    FLOOD_WAIT_BUFFER_MAX_SECONDS = 45

    def __init__(self, worker: QueueWorker) -> None:
        self.client = _PacedClient()
        self.limiter = _Limiter()
        self.settings = SimpleNamespace(expected_account_id=777)
        self._connected = True
        self._last_authorization_check = time.monotonic()
        self._status_callback = None
        self.worker = worker

    def _interruption_requested(self):
        return self.worker.isInterruptionRequested()


@pytest.mark.asyncio
async def test_shutdown_requested_at_exact_dispatch_boundary_blocks_mutating_rpc():
    worker = _DeterministicQueueWorker()
    service = _Service(worker)
    barrier = worker.create_scope_dispatch_barrier(("task", 1))
    request = SendMessageRequest(peer="example", message="hello", random_id=1)

    operation = asyncio.create_task(
        service.execute(
            service.client,
            request,
            retry_network=False,
            dispatch_barrier=barrier,
        )
    )
    await asyncio.wait_for(service.client.entered.wait(), timeout=2)

    # execute() has passed its early interruption check, while the request has
    # not yet crossed the PacedTelegramClient observer/dispatch boundary.
    worker.request_shutdown()
    service.client.release.set()

    with pytest.raises(DeferredTelegramError) as raised:
        await asyncio.wait_for(operation, timeout=2)

    assert raised.value.code == "shutdown_before_dispatch"
    assert service.client.calls == []

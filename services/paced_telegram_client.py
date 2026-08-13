from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any, Callable, Iterator

from telethon import TelegramClient

from core.rate_limiter import RateLimiter, classify_rpc_request


_request_observer: ContextVar[Callable[[Any], None] | None] = ContextVar(
    "marlen_telegram_request_observer", default=None
)
_request_gate_depth: ContextVar[int] = ContextVar(
    "marlen_telegram_request_gate_depth", default=0
)


class PacedTelegramClient(TelegramClient):
    """TelegramClient with one process-wide, non-overlapping request gate.

    Every MTProto API request issued by public client methods, iterators and
    authorization helpers passes through ``TelegramClient.__call__``. Pacing at
    this boundary prevents helper methods and paginators from bypassing the
    application's minimum request interval.
    """

    _marlen_request_pacing = True

    def __init__(
        self,
        *args: Any,
        request_limiter: RateLimiter | None = None,
        request_safety_gate: Any | None = None,
        request_timeout: float = 30.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._marlen_request_limiter = request_limiter or RateLimiter()
        self._marlen_request_safety_gate = request_safety_gate
        self._marlen_request_timeout = max(1.0, float(request_timeout))

    async def _call_one(
        self,
        request: Any,
        *,
        ordered: bool,
        flood_sleep_threshold: int | None,
    ) -> Any:
        depth = _request_gate_depth.get()
        safety_gate = self._marlen_request_safety_gate
        if safety_gate is not None:
            waiter = getattr(safety_gate, "wait_for_request", None)
            if callable(waiter):
                await waiter(request)
        category = classify_rpc_request(request)
        slot_factory = (
            self._marlen_request_limiter.reentrant_request_slot
            if depth > 0
            else self._marlen_request_limiter.request_slot
        )
        try:
            slot = slot_factory(category)
        except TypeError:
            # Compatibility with deliberately tiny test/third-party limiters
            # implementing the pre-category no-argument protocol.
            slot = slot_factory()
        async with slot:
            token = _request_gate_depth.set(depth + 1)
            try:
                observer = _request_observer.get()
                dispatch_context = nullcontext()
                if observer is not None:
                    dispatch_context = observer(request) or nullcontext()
                with dispatch_context:
                    operation = super().__call__(
                        request,
                        ordered=ordered,
                        flood_sleep_threshold=flood_sleep_threshold,
                    )
                    task = asyncio.ensure_future(operation)
                    # Let the Telethon coroutine enter its send path while the
                    # Stop/dispatch barrier is still held. The lock is released
                    # after one event-loop turn, not for the network round-trip.
                    await asyncio.sleep(0)
                return await asyncio.wait_for(
                    task, timeout=self._marlen_request_timeout
                )
            finally:
                _request_gate_depth.reset(token)

    @contextmanager
    def observe_requests(self, observer: Callable[[Any], None]) -> Iterator[None]:
        """Expose the exact MTProto dispatch boundary to the transport."""
        token = _request_observer.set(observer)
        try:
            yield
        finally:
            _request_observer.reset(token)

    async def __call__(
        self,
        request: Any,
        ordered: bool = False,
        flood_sleep_threshold: int | None = None,
    ) -> Any:
        # Telethon accepts a list of requests and may submit the whole batch at
        # once. Marlen never needs burst batching, so split it deliberately and
        # preserve a minimum gap between every individual API request.
        if isinstance(request, (list, tuple)):
            results = []
            for item in request:
                results.append(
                    await self._call_one(
                        item,
                        ordered=True,
                        flood_sleep_threshold=flood_sleep_threshold,
                    )
                )
            return results
        return await self._call_one(
            request,
            ordered=ordered,
            flood_sleep_threshold=flood_sleep_threshold,
        )

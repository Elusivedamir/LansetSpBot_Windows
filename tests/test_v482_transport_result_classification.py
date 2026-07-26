"""The sent / not-sent / unknown decision made by the MTProto transport.

This is the single most safety-critical classification in the application: a
lost response to a mutating request must never be reported as a failure and
must never be retried, because Telegram may already have accepted the message.
Reads, by contrast, are freely retryable.
"""

from __future__ import annotations

import asyncio

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import GetHistoryRequest, SendMessageRequest

from core.exceptions import (
    DeferredTelegramError,
    NonRetryableTelegramError,
)
from services.telegram_service import TelegramService


class _Limiter:
    async def acquire(self):
        return None


def _service(exception: BaseException) -> TelegramService:
    service = object.__new__(TelegramService)

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        def is_connected(self) -> bool:
            return True

        async def __call__(self, request):
            self.calls += 1
            raise exception

    service.client = _Client()
    service.limiter = _Limiter()
    service._connected = True  # noqa: SLF001
    service._status_callback = None  # noqa: SLF001

    async def _noop():
        return None

    service.disconnect = _noop
    service.ensure_connected = _noop
    service.safe_sleep = lambda seconds: asyncio.sleep(0, result=True)
    return service


def _send_request() -> SendMessageRequest:
    return SendMessageRequest(peer=1, message="comment")


def _read_request() -> GetHistoryRequest:
    return GetHistoryRequest(
        peer=1,
        offset_id=0,
        offset_date=None,
        add_offset=0,
        limit=1,
        max_id=0,
        min_id=0,
        hash=0,
    )


@pytest.mark.parametrize(
    "failure",
    [
        asyncio.TimeoutError(),
        ConnectionError("response lost after the server accepted the request"),
        OSError("network went away mid-flight"),
    ],
    ids=["timeout", "connection-reset", "os-error"],
)
@pytest.mark.asyncio
async def test_a_lost_response_to_a_send_is_unknown_never_failed(failure) -> None:
    """Telegram may already have accepted the comment; do not guess."""

    service = _service(failure)
    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.execute(service.client, _send_request(), retry_network=False)
    assert raised.value.code == "delivery_result_unknown"
    # The mutating request must never be replayed.
    assert service.client.calls == 1


@pytest.mark.asyncio
async def test_the_unknown_code_is_configurable_per_operation() -> None:
    """Joins report their own unknown code, not the delivery one."""

    service = _service(ConnectionError("ambiguous join"))
    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.execute(
            service.client,
            JoinChannelRequest(channel=1),
            retry_network=False,
            unknown_result_code="join_result_unknown",
        )
    assert raised.value.code == "join_result_unknown"
    assert service.client.calls == 1


@pytest.mark.asyncio
async def test_a_read_is_retried_and_then_reported_as_network_unavailable() -> None:
    """A read has no side effect, so bounded retries are safe."""

    service = _service(ConnectionError("no route to host"))
    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.execute(service.client, _read_request(), retry_network=True)
    assert raised.value.code == "network_unavailable"
    assert service.client.calls == 3, "network retries must stay bounded"


@pytest.mark.asyncio
async def test_a_flood_wait_is_a_deferral_not_a_failed_delivery() -> None:
    """FloodWait is a pre-execution rejection: nothing was sent."""

    service = _service(FloodWaitError(None, capture=42))
    with pytest.raises(DeferredTelegramError) as raised:
        await service.execute(service.client, _send_request(), retry_network=False)
    assert raised.value.code == "flood_wait_deferred"
    assert raised.value.retry_after >= 42
    assert service.client.calls == 1


@pytest.mark.asyncio
async def test_a_stop_before_dispatch_is_a_safe_deferral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping before the request ever left the client loses nothing.

    The transport marks a request as dispatched the moment it enters the
    dispatch context, so the only genuinely pre-dispatch stop is an
    interruption observed before that point.
    """

    service = _service(ConnectionError("never reached"))
    monkeypatch.setattr(
        type(service), "_interruption_requested", lambda self: True, raising=False
    )
    with pytest.raises(DeferredTelegramError) as raised:
        await service.execute(service.client, _read_request(), retry_network=False)
    assert raised.value.code == "shutdown_before_dispatch"
    assert service.client.calls == 0, "nothing may be sent after a stop"


@pytest.mark.asyncio
async def test_a_request_is_marked_dispatched_as_soon_as_it_is_handed_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conservative by design: once handed over, a stop is never 'nothing sent'."""

    service = _service(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await service.execute(service.client, _read_request(), retry_network=False)
    assert service.client.calls == 1


@pytest.mark.asyncio
async def test_a_cancellation_after_dispatch_is_not_downgraded_to_a_deferral() -> None:
    """Once the send is in flight, a stop must not claim nothing happened."""

    service = _service(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await service.execute(service.client, _send_request(), retry_network=False)


def test_only_mutating_request_types_mark_the_request_as_dispatched() -> None:
    """The unknown-result rule keys off this set, so guard its contents."""

    mutating = TelegramService._MUTATING_REQUEST_TYPES  # noqa: SLF001
    assert issubclass(SendMessageRequest, mutating)
    assert issubclass(JoinChannelRequest, mutating)
    assert not issubclass(GetHistoryRequest, mutating)

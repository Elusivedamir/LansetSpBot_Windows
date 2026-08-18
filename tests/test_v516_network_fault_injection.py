from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest
from telethon.tl.functions.messages import SendMessageRequest

from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from services.comment_service import CommentService
from services.telegram_service import TelegramService
from storage.database import Database


ACCOUNT_ID = 77
CHANNEL_ID = -1001001
DISCUSSION_ID = -1002002
POST_ID = 55
REPLY_TO = 77
DIRECT_CHAT_ID = -1003003
JOIN_CHAT_ID = -1004004


class _ScriptedPacedClient:
    """Deterministic stand-in for PacedTelegramClient's exact request boundary."""

    _marlen_request_pacing = True

    def __init__(self, events: list[str], *, identity_id: int = ACCOUNT_ID) -> None:
        self.events = list(events)
        self.identity_id = int(identity_id)
        self.connected = True
        self.observer = None
        self.attempts = 0
        self.dispatches = 0
        self.connect_calls = 0
        self.disconnect_calls = 0

    @contextmanager
    def observe_requests(self, observer):
        previous = self.observer
        self.observer = observer
        try:
            yield
        finally:
            self.observer = previous

    def is_connected(self) -> bool:
        return bool(self.connected)

    async def connect(self):
        self.connect_calls += 1
        self.connected = True
        return True

    async def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False

    async def get_me(self):
        return SimpleNamespace(id=self.identity_id)

    def _next_event(self) -> str:
        self.attempts += 1
        if not self.events:
            raise AssertionError("network fault script exhausted")
        return str(self.events.pop(0))

    async def _run_event(self, event: str, request, *, message_id: int):
        if event == "pre_connection":
            raise ConnectionError("connection lost before MTProto dispatch")
        if event == "pre_timeout":
            raise asyncio.TimeoutError("timeout before MTProto dispatch")
        if event == "pre_oserror":
            raise OSError("proxy socket failed before MTProto dispatch")
        if event == "pre_cancel":
            raise asyncio.CancelledError
        if event == "pre_wrong_identity":
            # The socket dies before dispatch. The reconnect resolves a different
            # Telegram identity, which must fail closed before a second mutation.
            self.identity_id = ACCOUNT_ID + 999
            raise ConnectionError("proxy reconnect changed Telegram identity")

        dispatch_context = (
            self.observer(request) if self.observer is not None else nullcontext()
        )
        if dispatch_context is None:
            dispatch_context = nullcontext()

        with dispatch_context:
            self.dispatches += 1
            # Match PacedTelegramClient: the dispatch barrier covers the point
            # where the Telethon coroutine enters its send path, not the whole
            # network round-trip.
            await asyncio.sleep(0)

        if event == "post_connection":
            raise ConnectionError("response lost after MTProto dispatch")
        if event == "post_timeout":
            raise asyncio.TimeoutError("timeout after MTProto dispatch")
        if event == "post_oserror":
            raise OSError("proxy socket reset after MTProto dispatch")
        if event == "post_cancel":
            raise asyncio.CancelledError
        if event != "ok":
            raise AssertionError(f"unknown network event: {event}")

        return SimpleNamespace(
            id=int(message_id),
            sender_id=ACCOUNT_ID,
            date=None,
        )

    async def send_message(self, _chat_id, _text, **_kwargs):
        event = self._next_event()
        # _execute_once only marks a request as mutating when the observer sees
        # an actual Telethon mutating request type. Construction is unnecessary
        # for this boundary test; isinstance() is the contract under test.
        request = object.__new__(SendMessageRequest)
        return await self._run_event(event, request, message_id=9901)

    async def __call__(self, request, **_kwargs):
        event = self._next_event()
        return await self._run_event(event, request, message_id=8801)


def _make_telegram(events: list[str]) -> TelegramService:
    service = object.__new__(TelegramService)
    service.client = _ScriptedPacedClient(events)
    service.limiter = SimpleNamespace()
    service.settings = SimpleNamespace(
        configured=True,
        expected_account_id=ACCOUNT_ID,
        proxy_password="",
        proxy_secret="",
        api_hash="",
        phone="",
    )
    service.account_id = ACCOUNT_ID
    service._connected = True
    service._last_authorization_check = time.monotonic()
    service._authorized_user = SimpleNamespace(id=ACCOUNT_ID)
    service._status_callback = None
    service._terminal_account_error_callback = None
    service._peer_references = {}

    async def _fast_sleep(_seconds: float) -> bool:
        return True

    # Remove real exponential waits while preserving the transport decision path.
    service.safe_sleep = _fast_sleep
    service._interruption_requested = lambda: False
    return service


def _prepare_database(tmp_path) -> Database:
    db = Database(tmp_path / "network-fault.db")
    db.set_setting("telegram.account_id", ACCOUNT_ID)
    db.upsert_channels_batch(
        [
            {
                "channel_id": CHANNEL_ID,
                "linked_chat_id": DISCUSSION_ID,
                "title": "Source",
                "target_kind": "channel",
                "comment_mode": "channel_post",
                "link_status": "linked",
            }
        ],
        account_id=ACCOUNT_ID,
    )
    assert db.is_comment_link_membership_confirmed(
        CHANNEL_ID,
        DISCUSSION_ID,
        account_id=ACCOUNT_ID,
    )
    return db


def _comment_kwargs():
    return {
        "channel_id": CHANNEL_ID,
        "post_message_id": POST_ID,
        "text": "hello",
        "linked_chat_id": DISCUSSION_ID,
        "reply_to": REPLY_TO,
        "account_id": ACCOUNT_ID,
    }


def _delivery_row(db: Database):
    with db.get_connection() as conn:
        return conn.execute(
            """SELECT status, error, comment_message_id
               FROM comment_deliveries
               WHERE account_id=? AND campaign_id=0 AND action_type='comment'
                 AND channel_id=? AND post_id=? AND linked_chat_id=?""",
            (ACCOUNT_ID, CHANNEL_ID, POST_ID, DISCUSSION_ID),
        ).fetchone()


async def _assert_comment_second_send_blocked(
    service: CommentService,
    telegram: TelegramService,
    *,
    expected_status: str,
):
    attempts_before = telegram.client.attempts
    dispatches_before = telegram.client.dispatches
    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.ensure_and_send_comment(**_comment_kwargs())
    assert raised.value.code == "comment_already_reserved"
    assert telegram.client.attempts == attempts_before
    assert telegram.client.dispatches == dispatches_before
    row = _delivery_row(service.db)
    assert row is not None
    assert row["status"] == expected_status


@pytest.mark.asyncio
async def test_predispatch_connection_drop_reconnects_and_dispatches_exactly_once():
    telegram = _make_telegram(["pre_connection", "ok"])

    result = await telegram.send_message(DIRECT_CHAT_ID, "hello")

    assert int(result.id) == 9901
    assert telegram.client.attempts == 2
    assert telegram.client.dispatches == 1
    assert telegram.client.disconnect_calls >= 1
    assert telegram.client.connect_calls >= 1


@pytest.mark.parametrize(
    "event",
    ["post_connection", "post_timeout", "post_oserror"],
)
@pytest.mark.asyncio
async def test_postdispatch_network_loss_becomes_uncertain_without_replay(
    tmp_path,
    event,
):
    db = _prepare_database(tmp_path)
    telegram = _make_telegram([event])
    service = CommentService(telegram, db=db)

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.ensure_and_send_comment(**_comment_kwargs())

    assert raised.value.code == "delivery_result_unknown"
    assert telegram.client.attempts == 1
    assert telegram.client.dispatches == 1

    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "uncertain"
    await _assert_comment_second_send_blocked(
        service,
        telegram,
        expected_status="uncertain",
    )


@pytest.mark.asyncio
async def test_three_predispatch_network_failures_release_guard_for_one_safe_retry(
    tmp_path,
):
    db = _prepare_database(tmp_path)
    telegram = _make_telegram(
        ["pre_connection", "pre_timeout", "pre_oserror"]
    )
    service = CommentService(telegram, db=db)

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.ensure_and_send_comment(**_comment_kwargs())

    assert raised.value.code == "network_unavailable"
    assert telegram.client.attempts == 3
    assert telegram.client.dispatches == 0
    # network_unavailable is proven pre-dispatch, so CommentService may release
    # the reservation instead of poisoning the post as uncertain.
    assert _delivery_row(db) is None

    telegram.client.events.append("ok")
    result = await service.ensure_and_send_comment(**_comment_kwargs())

    assert int(result.id) == 9901
    assert telegram.client.attempts == 4
    assert telegram.client.dispatches == 1
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "sent"


@pytest.mark.asyncio
async def test_cancellation_before_dispatch_is_deferred_and_safe_to_retry(tmp_path):
    db = _prepare_database(tmp_path)
    telegram = _make_telegram(["pre_cancel"])
    service = CommentService(telegram, db=db)

    with pytest.raises(DeferredTelegramError) as raised:
        await service.ensure_and_send_comment(**_comment_kwargs())

    assert raised.value.code == "shutdown_before_dispatch"
    assert telegram.client.attempts == 1
    assert telegram.client.dispatches == 0
    assert _delivery_row(db) is None

    telegram.client.events.append("ok")
    result = await service.ensure_and_send_comment(**_comment_kwargs())
    assert int(result.id) == 9901
    assert telegram.client.dispatches == 1


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_marks_uncertain_and_blocks_replay(tmp_path):
    db = _prepare_database(tmp_path)
    telegram = _make_telegram(["post_cancel"])
    service = CommentService(telegram, db=db)

    with pytest.raises(asyncio.CancelledError):
        await service.ensure_and_send_comment(**_comment_kwargs())

    assert telegram.client.attempts == 1
    assert telegram.client.dispatches == 1
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "uncertain"

    await _assert_comment_second_send_blocked(
        service,
        telegram,
        expected_status="uncertain",
    )


@pytest.mark.asyncio
async def test_direct_message_lost_response_uses_direct_unknown_code_and_no_replay(
    tmp_path,
):
    db = _prepare_database(tmp_path)
    task_id = int(
        db.insert_task(
            "direct_message",
            {
                "account_id": ACCOUNT_ID,
                "chat_id": DIRECT_CHAT_ID,
                "text": "hello",
            },
        )
    )
    telegram = _make_telegram(["post_connection"])
    service = CommentService(telegram, db=db)

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.send_direct_message(
            DIRECT_CHAT_ID,
            "hello",
            task_id=task_id,
            account_id=ACCOUNT_ID,
        )

    assert raised.value.code == "direct_message_result_unknown"
    assert telegram.client.attempts == 1
    assert telegram.client.dispatches == 1

    delivery = db.get_direct_message_delivery(task_id)
    assert delivery is not None
    assert delivery["status"] == "uncertain"

    with pytest.raises(NonRetryableTelegramError) as duplicate:
        await service.send_direct_message(
            DIRECT_CHAT_ID,
            "hello",
            task_id=task_id,
            account_id=ACCOUNT_ID,
        )
    assert duplicate.value.code == "direct_message_duplicate_guard"
    assert telegram.client.attempts == 1
    assert telegram.client.dispatches == 1


@pytest.mark.asyncio
async def test_join_lost_response_is_unknown_and_never_replayed_inside_transport():
    telegram = _make_telegram(["post_connection"])

    with pytest.raises(NonRetryableTelegramError) as raised:
        await telegram.join(JOIN_CHAT_ID)

    assert raised.value.code == "join_result_unknown"
    assert telegram.client.attempts == 1
    assert telegram.client.dispatches == 1


@pytest.mark.asyncio
async def test_reconnect_identity_mismatch_fails_closed_before_second_dispatch(tmp_path):
    db = _prepare_database(tmp_path)
    telegram = _make_telegram(["pre_wrong_identity"])
    service = CommentService(telegram, db=db)

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.ensure_and_send_comment(**_comment_kwargs())

    assert raised.value.code == "account_state_mismatch"
    assert telegram.client.attempts == 1
    assert telegram.client.dispatches == 0
    assert telegram.client.connect_calls >= 1

    # The failure is definitive and pre-dispatch. The reservation is released,
    # but no mutation crossed the MTProto boundary under the wrong identity.
    assert _delivery_row(db) is None

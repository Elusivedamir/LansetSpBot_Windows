from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from services.comment_service import CommentService
from storage.database import Database
from storage.db_common import DatabaseError
from workers.queue_worker import QueueWorker


ACCOUNT_ID = 77
CHANNEL_ID = -1001001
DISCUSSION_ID = -1002002
POST_ID = 55
REPLY_TO = 77
WAIT_SECONDS = 5.0


def _prepare_comment_database(path) -> Database:
    db = Database(path)
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


def _delivery_row(db: Database):
    with db.get_connection() as conn:
        return conn.execute(
            """SELECT status, comment_message_id, error
               FROM comment_deliveries
               WHERE account_id=? AND campaign_id=0 AND action_type='comment'
                 AND channel_id=? AND post_id=? AND linked_chat_id=?""",
            (ACCOUNT_ID, CHANNEL_ID, POST_ID, DISCUSSION_ID),
        ).fetchone()


def _comment_kwargs(*, dispatch_barrier=None):
    return {
        "channel_id": CHANNEL_ID,
        "post_message_id": POST_ID,
        "text": "hello",
        "linked_chat_id": DISCUSSION_ID,
        "reply_to": REPLY_TO,
        "account_id": ACCOUNT_ID,
        "dispatch_barrier": dispatch_barrier,
    }


def _join_thread(thread: threading.Thread) -> None:
    thread.join(timeout=WAIT_SECONDS)
    assert not thread.is_alive(), f"thread did not finish: {thread.name}"


def test_before_claim_two_threads_cannot_claim_same_task(tmp_path):
    path = tmp_path / "claim-race.db"
    db = Database(path)
    task_id = db.insert_task("noop", {"account_id": ACCOUNT_ID})

    start = threading.Barrier(2)
    outcomes: list[int | None] = []
    errors: list[BaseException] = []
    outcome_lock = threading.Lock()

    def claim() -> None:
        local = None
        try:
            local = Database(path, bootstrap=False)
            start.wait(timeout=WAIT_SECONDS)
            task = local.claim_next_pending_task()
            with outcome_lock:
                outcomes.append(None if task is None else int(task["id"]))
        except BaseException as exc:  # pragma: no cover - surfaced below
            with outcome_lock:
                errors.append(exc)
            try:
                start.abort()
            except threading.BrokenBarrierError:
                pass
        finally:
            if local is not None:
                local.close_thread_connection()

    threads = [
        threading.Thread(target=claim, name=f"claim-racer-{index}")
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        _join_thread(thread)

    assert errors == []
    assert outcomes.count(task_id) == 1
    assert outcomes.count(None) == 1
    assert db.get_task(task_id)["status"] == "running"


def test_dispatch_started_during_stop_cannot_cross_scope_barrier():
    worker = QueueWorker(lambda: {})
    scope = ("account", ACCOUNT_ID)
    barrier = worker.create_scope_dispatch_barrier(scope)

    stop_inside_mutation = threading.Event()
    release_stop = threading.Event()
    dispatch_started = threading.Event()
    crossed: list[bool] = []
    dispatch_errors: list[BaseException] = []
    stop_errors: list[BaseException] = []

    def durable_stop_mutation():
        stop_inside_mutation.set()
        assert release_stop.wait(WAIT_SECONDS)
        return True

    def stop_account() -> None:
        try:
            worker.cancel_scopes_and_run((scope,), durable_stop_mutation)
        except BaseException as exc:  # pragma: no cover - surfaced below
            stop_errors.append(exc)

    def dispatch() -> None:
        dispatch_started.set()
        try:
            with barrier.dispatch():
                crossed.append(True)
        except BaseException as exc:
            dispatch_errors.append(exc)

    stop_thread = threading.Thread(target=stop_account, name="stop-racer")
    stop_thread.start()
    assert stop_inside_mutation.wait(WAIT_SECONDS)

    dispatch_thread = threading.Thread(target=dispatch, name="dispatch-racer")
    dispatch_thread.start()
    assert dispatch_started.wait(WAIT_SECONDS)

    # Stop owns the same scope lock as the dispatch boundary. The competing
    # dispatch must be waiting here, not crossing the mutating RPC boundary.
    time.sleep(0.05)
    assert dispatch_thread.is_alive()
    assert crossed == []

    release_stop.set()
    _join_thread(stop_thread)
    _join_thread(dispatch_thread)

    assert stop_errors == []
    assert crossed == []
    assert len(dispatch_errors) == 1
    assert isinstance(dispatch_errors[0], DeferredTelegramError)
    assert dispatch_errors[0].code == "shutdown_before_dispatch"


@pytest.mark.asyncio
async def test_stop_after_reservation_before_dispatch_sends_nothing_and_releases_reservation(
    tmp_path,
):
    db = _prepare_comment_database(tmp_path / "stop-before-dispatch.db")
    worker = QueueWorker(lambda: {})
    scope = ("account", ACCOUNT_ID)
    dispatch_barrier = worker.create_scope_dispatch_barrier(scope)

    entered_send = threading.Event()
    release_dispatch = threading.Event()

    class Telegram:
        def __init__(self) -> None:
            self.remote_sends = 0

        async def send_comment(self, *_args, dispatch_barrier=None, **_kwargs):
            entered_send.set()
            ready = await asyncio.to_thread(
                release_dispatch.wait,
                WAIT_SECONDS,
            )
            assert ready
            assert dispatch_barrier is not None
            with dispatch_barrier.dispatch():
                self.remote_sends += 1
            return SimpleNamespace(id=9001, sender_id=ACCOUNT_ID, date=None)

    telegram = Telegram()
    service = CommentService(telegram, db=db)
    send_task = asyncio.create_task(
        service.ensure_and_send_comment(
            **_comment_kwargs(dispatch_barrier=dispatch_barrier)
        )
    )

    assert await asyncio.to_thread(entered_send.wait, WAIT_SECONDS)
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "sending"

    assert worker.cancel_scopes_and_run((scope,), lambda: True) is True
    release_dispatch.set()

    with pytest.raises(DeferredTelegramError) as raised:
        await send_task

    assert raised.value.code == "shutdown_before_dispatch"
    assert telegram.remote_sends == 0
    assert _delivery_row(db) is None


@pytest.mark.asyncio
async def test_stop_after_dispatch_does_not_erase_confirmed_receipt(tmp_path):
    db = _prepare_comment_database(tmp_path / "stop-after-dispatch.db")
    worker = QueueWorker(lambda: {})
    scope = ("account", ACCOUNT_ID)
    dispatch_barrier = worker.create_scope_dispatch_barrier(scope)

    dispatched = threading.Event()
    release_response = threading.Event()

    class Telegram:
        def __init__(self) -> None:
            self.remote_sends = 0

        async def send_comment(self, *_args, dispatch_barrier=None, **_kwargs):
            assert dispatch_barrier is not None
            with dispatch_barrier.dispatch():
                self.remote_sends += 1
                dispatched.set()
            ready = await asyncio.to_thread(
                release_response.wait,
                WAIT_SECONDS,
            )
            assert ready
            return SimpleNamespace(id=9002, sender_id=ACCOUNT_ID, date=None)

    telegram = Telegram()
    service = CommentService(telegram, db=db)
    send_task = asyncio.create_task(
        service.ensure_and_send_comment(
            **_comment_kwargs(dispatch_barrier=dispatch_barrier)
        )
    )

    assert await asyncio.to_thread(dispatched.wait, WAIT_SECONDS)
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "sending"

    # The mutating boundary has already been crossed. Stop must linearize after
    # that fact; it may stop future work but must not rewrite a confirmed reply
    # into a false local failure once Telegram returns the message id.
    assert worker.cancel_scopes_and_run((scope,), lambda: True) is True
    release_response.set()

    result = await send_task

    assert int(result.id) == 9002
    assert telegram.remote_sends == 1
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "sent"
    assert int(row["comment_message_id"]) == 9002
    assert row["error"] is None


def test_after_dispatch_before_db_receipt_second_attempt_is_blocked(
    tmp_path,
    monkeypatch,
):
    db = _prepare_comment_database(tmp_path / "receipt-window.db")
    before_receipt = threading.Event()
    release_receipt = threading.Event()
    send_lock = threading.Lock()

    class Telegram:
        def __init__(self) -> None:
            self.remote_sends = 0

        async def send_comment(self, *_args, **_kwargs):
            with send_lock:
                self.remote_sends += 1
                message_id = 9100 + self.remote_sends
            return SimpleNamespace(id=message_id, sender_id=ACCOUNT_ID, date=None)

    telegram = Telegram()
    original_finalize = db.finalize_comment_delivery

    def blocked_finalize(data):
        before_receipt.set()
        assert release_receipt.wait(WAIT_SECONDS)
        return original_finalize(data)

    monkeypatch.setattr(db, "finalize_comment_delivery", blocked_finalize)

    first_errors: list[BaseException] = []
    first_results: list[object] = []

    def first_attempt() -> None:
        service = CommentService(telegram, db=db)
        try:
            result = asyncio.run(
                service.ensure_and_send_comment(**_comment_kwargs())
            )
            first_results.append(result)
        except BaseException as exc:  # pragma: no cover - surfaced below
            first_errors.append(exc)
        finally:
            db.close_thread_connection()

    first_thread = threading.Thread(
        target=first_attempt,
        name="first-delivery-before-receipt",
    )
    first_thread.start()

    assert before_receipt.wait(WAIT_SECONDS)
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "sending"
    assert telegram.remote_sends == 1

    second_service = CommentService(telegram, db=db)
    with pytest.raises(NonRetryableTelegramError) as raised:
        asyncio.run(second_service.ensure_and_send_comment(**_comment_kwargs()))

    assert raised.value.code == "comment_already_reserved"
    assert telegram.remote_sends == 1

    release_receipt.set()
    _join_thread(first_thread)

    assert first_errors == []
    assert len(first_results) == 1
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "sent"
    assert int(row["comment_message_id"]) == 9101
    assert telegram.remote_sends == 1


@pytest.mark.asyncio
async def test_receipt_persist_failure_becomes_uncertain_and_blocks_replay(
    tmp_path,
    monkeypatch,
):
    db = _prepare_comment_database(tmp_path / "receipt-failure.db")

    class Telegram:
        def __init__(self) -> None:
            self.remote_sends = 0

        async def send_comment(self, *_args, **_kwargs):
            self.remote_sends += 1
            return SimpleNamespace(id=9201, sender_id=ACCOUNT_ID, date=None)

    telegram = Telegram()
    service = CommentService(telegram, db=db)

    def fail_finalize(_data):
        raise DatabaseError("injected receipt persistence failure")

    monkeypatch.setattr(db, "finalize_comment_delivery", fail_finalize)

    with pytest.raises(NonRetryableTelegramError) as first:
        await service.ensure_and_send_comment(**_comment_kwargs())

    assert first.value.code == "delivery_persist_failed"
    assert telegram.remote_sends == 1
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "uncertain"

    with pytest.raises(NonRetryableTelegramError) as replay:
        await service.ensure_and_send_comment(**_comment_kwargs())

    assert replay.value.code == "comment_already_reserved"
    assert telegram.remote_sends == 1
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "uncertain"

from __future__ import annotations

import random
from datetime import timedelta
from types import SimpleNamespace

import pytest

from core.campaign_schedule import to_db_time, utc_now
from core.account_restriction import activate_account_restriction
from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from services.comment_service import CommentService
from storage.database import Database
from workers.comment_slot.handler import create_comment_slot_handler
from workers.handlers.manual_comment import create_manual_comment_handler
from workers.queue_worker import QueueWorker


SOURCE_ID = -1001001
DISCUSSION_ID = -1002002


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _insert_source(db: Database, account_id: int) -> None:
    db.set_setting("telegram.account_id", account_id)
    db.upsert_channels_batch(
        [
            {
                "channel_id": SOURCE_ID,
                "linked_chat_id": DISCUSSION_ID,
                "title": "Source",
                "target_kind": "channel",
                "comment_mode": "channel_post",
                "link_status": "linked",
            }
        ],
        account_id=account_id,
    )


def _make_due_comment_task(db: Database, account_id: int):
    _insert_source(db, account_id)
    campaign = db.create_comment_campaign(
        ["hello"],
        daily_limit=1,
        slot_count=1,
        continuous=False,
        start_at=utc_now() - timedelta(hours=1),
        account_id=account_id,
        rng=random.Random(1),
    )
    slot = db.get_comment_schedule(campaign["id"], limit=1)[0]
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE comment_schedule SET scheduled_at=? WHERE id=?",
            (to_db_time(utc_now() - timedelta(seconds=1)), int(slot["id"])),
        )
    assert db.queue_due_comment_slot(now=utc_now()) is not None
    task = db.claim_next_pending_task()
    assert task is not None
    return campaign, slot, task


class _ResolvedTelegram:
    def __init__(self, *, before_result=None):
        self.before_result = before_result
        self.read_calls = 0

    def register_peer_reference(self, *_args, **_kwargs):
        return None

    async def get_latest_post_for_commenting(self, _channel_id):
        self.read_calls += 1
        if self.before_result is not None:
            self.before_result()
        return SimpleNamespace(
            status="ok",
            message=SimpleNamespace(id=55),
            discussion_chat_id=DISCUSSION_ID,
            discussion_message_id=77,
        )

    async def get_post_for_commenting(
        self, _channel_id, post_id, *, dispatch_barrier=None
    ):
        self.read_calls += 1
        if self.before_result is not None:
            self.before_result()
        if dispatch_barrier is not None:
            with dispatch_barrier.dispatch():
                pass
        return SimpleNamespace(
            status="ok",
            message=SimpleNamespace(id=int(post_id)),
            discussion_chat_id=DISCUSSION_ID,
            discussion_message_id=77,
        )


class _BarrierComments:
    def __init__(self, *, before_dispatch=None):
        self.before_dispatch = before_dispatch
        self.calls = 0
        self.sent = 0
        self.barrier = None

    async def ensure_and_send_comment(self, **kwargs):
        self.calls += 1
        self.barrier = kwargs.get("dispatch_barrier")
        if self.before_dispatch is not None:
            self.before_dispatch()
        if self.barrier is None:
            self.sent += 1
            return SimpleNamespace(id=999)
        with self.barrier.dispatch():
            self.sent += 1
        return SimpleNamespace(id=999)


def _comment_slot_handler(db, worker, telegram, comments):
    return create_comment_slot_handler(
        as_int=_as_int,
        queue_worker=worker,
        config=SimpleNamespace(
            post_join_delay_min_seconds=0,
            post_join_delay_max_seconds=0,
        ),
        worker_db=db,
        telegram=telegram,
        comments=comments,
        set_runtime=lambda *_args, **_kwargs: None,
    )


@pytest.mark.asyncio
async def test_comment_slot_ban_created_during_route_resolution_blocks_send(tmp_path):
    account_id = 77
    db = Database(tmp_path / "route-race.db")
    _campaign, _slot, task = _make_due_comment_task(db, account_id)
    worker = QueueWorker(lambda: {})
    worker._db = db
    telegram = _ResolvedTelegram(
        before_result=lambda: db.ban_channel_locally(
            SOURCE_ID,
            "race",
            related_peer_id=DISCUSSION_ID,
            account_id=account_id,
        )
    )
    comments = _BarrierComments()
    handler = _comment_slot_handler(db, worker, telegram, comments)
    worker._handlers = {"auto_comment_slot": handler}

    await worker._process_task(task)

    assert db.is_channel_locally_banned(SOURCE_ID, account_id=account_id)
    assert comments.calls == 0
    assert comments.sent == 0


@pytest.mark.asyncio
async def test_comment_slot_ban_committed_at_dispatch_barrier_blocks_send(tmp_path):
    account_id = 78
    db = Database(tmp_path / "dispatch-race.db")
    _campaign, _slot, task = _make_due_comment_task(db, account_id)
    worker = QueueWorker(lambda: {})
    worker._db = db
    telegram = _ResolvedTelegram()
    comments = _BarrierComments(
        before_dispatch=lambda: db.ban_peer_locally(
            DISCUSSION_ID,
            "dispatch race",
            account_id=account_id,
            source_channel_id=SOURCE_ID,
        )
    )
    handler = _comment_slot_handler(db, worker, telegram, comments)
    worker._handlers = {"auto_comment_slot": handler}

    await worker._process_task(task)

    assert comments.barrier is not None
    assert comments.calls == 1
    assert comments.sent == 0
    assert db.is_channel_locally_banned(DISCUSSION_ID, account_id=account_id)


@pytest.mark.asyncio
async def test_comment_slot_restricted_at_dispatch_barrier_blocks_send(tmp_path):
    account_id = 79
    db = Database(tmp_path / "restricted-race.db")
    _campaign, _slot, task = _make_due_comment_task(db, account_id)
    worker = QueueWorker(lambda: {})
    worker._db = db
    comments = _BarrierComments(
        before_dispatch=lambda: activate_account_restriction(
            db,
            account_id=account_id,
            code="user_restricted",
            message="restricted",
        )
    )
    handler = _comment_slot_handler(db, worker, _ResolvedTelegram(), comments)
    worker._handlers = {"auto_comment_slot": handler}

    await worker._process_task(task)

    assert comments.barrier is not None
    assert comments.calls == 1
    assert comments.sent == 0
    assert db.get_account_restriction(account_id)["active"] is True


@pytest.mark.asyncio
async def test_manual_comment_stop_before_read_blocks_all_rpc(tmp_path):
    account_id = 88
    db = Database(tmp_path / "manual-stop.db")
    _insert_source(db, account_id)
    worker = QueueWorker(lambda: {})
    worker._db = db
    telegram = _ResolvedTelegram()
    comments = _BarrierComments()
    task = {
        "id": 123,
        "payload": {
            "account_id": account_id,
            "channel_id": SOURCE_ID,
            "post_id": 55,
            "text": "hello",
        },
    }
    worker.request_scope_cancellation("task", 123)
    handler = create_manual_comment_handler(
        as_int=_as_int,
        queue_worker=worker,
        config=SimpleNamespace(),
        worker_db=db,
        telegram=telegram,
        comments=comments,
    )

    with pytest.raises(DeferredTelegramError) as raised:
        await handler(task)

    assert raised.value.code == "shutdown_before_dispatch"
    assert telegram.read_calls == 0
    assert comments.sent == 0


@pytest.mark.asyncio
async def test_manual_comment_local_ban_at_dispatch_blocks_send(tmp_path):
    account_id = 89
    db = Database(tmp_path / "manual-ban.db")
    _insert_source(db, account_id)
    worker = QueueWorker(lambda: {})
    worker._db = db
    comments = _BarrierComments(
        before_dispatch=lambda: db.ban_peer_locally(
            DISCUSSION_ID,
            "manual dispatch race",
            account_id=account_id,
            source_channel_id=SOURCE_ID,
        )
    )
    handler = create_manual_comment_handler(
        as_int=_as_int,
        queue_worker=worker,
        config=SimpleNamespace(),
        worker_db=db,
        telegram=_ResolvedTelegram(),
        comments=comments,
    )

    with pytest.raises(NonRetryableTelegramError) as raised:
        await handler(
            {
                "id": 124,
                "payload": {
                    "account_id": account_id,
                    "channel_id": SOURCE_ID,
                    "post_id": 55,
                    "text": "hello",
                },
            }
        )

    assert raised.value.code == "channel_locally_banned"
    assert comments.barrier is not None
    assert comments.sent == 0


@pytest.mark.asyncio
async def test_manual_comment_restricted_at_dispatch_blocks_send(tmp_path):
    account_id = 92
    db = Database(tmp_path / "manual-restricted.db")
    _insert_source(db, account_id)
    worker = QueueWorker(lambda: {})
    worker._db = db

    def commit_restriction_only():
        with db.get_connection() as conn:
            conn.execute(
                """INSERT INTO account_restrictions(
                       account_id, active, code, message, detected_at,
                       details_json, updated_at)
                   VALUES(?, 1, 'user_restricted', 'restricted',
                          CURRENT_TIMESTAMP, '{}', CURRENT_TIMESTAMP)
                   ON CONFLICT(account_id) DO UPDATE SET
                       active=1, code='user_restricted', message='restricted',
                       updated_at=CURRENT_TIMESTAMP""",
                (account_id,),
            )

    comments = _BarrierComments(before_dispatch=commit_restriction_only)
    handler = create_manual_comment_handler(
        as_int=_as_int,
        queue_worker=worker,
        config=SimpleNamespace(),
        worker_db=db,
        telegram=_ResolvedTelegram(),
        comments=comments,
    )

    with pytest.raises(NonRetryableTelegramError):
        await handler(
            {
                "id": 126,
                "payload": {
                    "account_id": account_id,
                    "channel_id": SOURCE_ID,
                    "post_id": 55,
                    "text": "hello",
                },
            }
        )

    assert comments.barrier is not None
    assert comments.sent == 0
    assert db.get_account_restriction(account_id)["active"] is True


@pytest.mark.asyncio
async def test_manual_comment_missing_source_fails_closed_without_rpc(tmp_path):
    account_id = 90
    db = Database(tmp_path / "manual-missing.db")
    db.set_setting("telegram.account_id", account_id)
    telegram = _ResolvedTelegram()
    comments = _BarrierComments()
    handler = create_manual_comment_handler(
        as_int=_as_int,
        queue_worker=QueueWorker(lambda: {}),
        config=SimpleNamespace(),
        worker_db=db,
        telegram=telegram,
        comments=comments,
    )

    with pytest.raises(NonRetryableTelegramError) as raised:
        await handler(
            {
                "id": 125,
                "payload": {
                    "account_id": account_id,
                    "channel_id": SOURCE_ID,
                    "post_id": 55,
                    "text": "hello",
                },
            }
        )

    assert raised.value.code == "channel_locally_banned"
    assert telegram.read_calls == 0
    assert comments.sent == 0


class _SendTelegram:
    def __init__(self):
        self.send_calls = 0

    async def send_comment(self, *_args, **_kwargs):
        self.send_calls += 1
        return SimpleNamespace(id=999, sender_id=1, date=None)


@pytest.mark.asyncio
async def test_comment_service_checks_related_peer_ban_before_reservation(tmp_path):
    account_id = 91
    db = Database(tmp_path / "service-related-ban.db")
    _insert_source(db, account_id)
    assert db.ban_peer_locally(
        DISCUSSION_ID,
        "related peer",
        account_id=account_id,
        source_channel_id=SOURCE_ID,
    )
    telegram = _SendTelegram()
    service = CommentService(telegram, db=db)

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.ensure_and_send_comment(
            channel_id=SOURCE_ID,
            linked_chat_id=DISCUSSION_ID,
            post_message_id=55,
            text="hello",
            account_id=account_id,
            campaign_id=7,
        )

    assert raised.value.code == "channel_locally_banned"
    assert telegram.send_calls == 0
    with db.get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM comment_deliveries").fetchone()[0]
    assert count == 0

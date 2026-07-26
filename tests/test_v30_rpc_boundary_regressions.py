from __future__ import annotations

from contextlib import contextmanager
import random
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl.functions.channels import GetFullChannelRequest

from core.account_restriction import activate_account_restriction
from core.campaign_schedule import to_db_time, utc_now
from core.exceptions import DeferredTelegramError
from services.telegram.models import LatestPostResult
from services.telegram.transport import TelegramTransportMixin
from core.config import TelegramSettings
from storage.database import Database
from workers.handler_registry import create_worker_handlers
from workers.comment_slot.handler import create_comment_slot_handler
from workers.handlers.join_slot import create_join_slot_handler
from workers.handlers.manual_comment import create_manual_comment_handler
from workers.queue_worker import QueueWorker


SOURCE_ID = -1001001
DISCUSSION_ID = -1002002


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


class _ReadBoundaryTelegram:
    def __init__(self, before_dispatch):
        self.before_dispatch = before_dispatch
        self.rpc_calls = 0

    def register_peer_reference(self, *_args, **_kwargs):
        return None

    async def get_latest_post_for_commenting(
        self, _channel_id, *, dispatch_barrier=None
    ):
        self.before_dispatch()
        if dispatch_barrier is None:
            self.rpc_calls += 1
        else:
            with dispatch_barrier.dispatch(GetFullChannelRequest(channel=1)):
                self.rpc_calls += 1
        return LatestPostResult(
            "ok",
            SimpleNamespace(id=55),
            discussion_chat_id=DISCUSSION_ID,
            discussion_message_id=77,
        )


class _NoSendComments:
    def __init__(self):
        self.calls = 0

    async def ensure_and_send_comment(self, **_kwargs):
        self.calls += 1
        return SimpleNamespace(id=1)


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
    return task


@pytest.mark.asyncio
async def test_comment_campaign_read_rpc_obeys_fresh_local_ban(tmp_path):
    account_id = 300
    db = Database(tmp_path / "campaign-read-ban.db")
    task = _make_due_comment_task(db, account_id)
    worker = QueueWorker(lambda: {})
    worker._db = db
    telegram = _ReadBoundaryTelegram(
        lambda: db.ban_channel_locally(
            SOURCE_ID,
            "campaign read race",
            related_peer_id=DISCUSSION_ID,
            account_id=account_id,
        )
    )
    comments = _NoSendComments()
    handler = create_comment_slot_handler(
        as_int=lambda value, default=0: int(value if value is not None else default),
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
    worker._handlers = {"auto_comment_slot": handler}

    await worker._process_task(task)

    assert telegram.rpc_calls == 0
    assert comments.calls == 0


@pytest.mark.asyncio
async def test_manual_comment_read_rpc_obeys_fresh_local_ban(tmp_path):
    account_id = 301
    db = Database(tmp_path / "manual-read-ban.db")
    _insert_source(db, account_id)
    worker = QueueWorker(lambda: {})
    worker._db = db
    telegram = _ReadBoundaryTelegram(
        lambda: db.ban_channel_locally(
            SOURCE_ID,
            "read race",
            related_peer_id=DISCUSSION_ID,
            account_id=account_id,
        )
    )
    comments = _NoSendComments()
    handler = create_manual_comment_handler(
        as_int=lambda value, default=0: int(value if value is not None else default),
        queue_worker=worker,
        config=SimpleNamespace(),
        worker_db=db,
        telegram=telegram,
        comments=comments,
    )

    with pytest.raises(Exception) as raised:
        await handler(
            {
                "id": 991,
                "payload": {
                    "account_id": account_id,
                    "channel_id": SOURCE_ID,
                    "post_id": 55,
                    "text": "hello",
                },
            }
        )

    assert getattr(raised.value, "code", "") in {
        "local_ban_before_dispatch",
        "channel_locally_banned",
    }
    assert telegram.rpc_calls == 0
    assert comments.calls == 0


class _PacedReadClient:
    _marlen_request_pacing = True

    def __init__(self):
        self.observer = None
        self.calls = 0

    @contextmanager
    def observe_requests(self, observer):
        previous = self.observer
        self.observer = observer
        try:
            yield
        finally:
            self.observer = previous

    async def request(self):
        request = GetFullChannelRequest(channel=1)
        ctx = self.observer(request) if self.observer else None
        if ctx is None:
            self.calls += 1
        else:
            with ctx:
                self.calls += 1
        return "ok"


class _ExecuteHarness(TelegramTransportMixin):
    def __init__(self):
        self.client = _PacedReadClient()
        self.limiter = SimpleNamespace()

    async def ensure_connected(self):
        return None


class _DenyBarrier:
    @contextmanager
    def dispatch(self, _request=None):
        raise DeferredTelegramError(
            "blocked", code="local_ban_before_dispatch", retry_after=1
        )
        yield


@pytest.mark.asyncio
async def test_transport_applies_dispatch_barrier_to_non_mutating_rpc():
    service = _ExecuteHarness()
    with pytest.raises(DeferredTelegramError) as raised:
        await service.execute(service.client.request, dispatch_barrier=_DenyBarrier())
    assert raised.value.code == "local_ban_before_dispatch"
    assert service.client.calls == 0


class _JoinBoundaryTelegram:
    def __init__(self, db: Database, account_id: int):
        self.db = db
        self.account_id = account_id
        self.rpc_calls = 0

    async def join_saved_dialog(
        self, *, username=None, invite_link=None, dispatch_barrier=None
    ):
        # Simulate a restriction committed by another worker at the exact
        # dispatch boundary without also stopping this campaign.  This isolates
        # the explicit RESTRICTED barrier from the campaign-status barrier.
        with self.db.get_connection() as conn:
            conn.execute(
                """INSERT INTO account_restrictions(
                       account_id, active, code, message, detected_at,
                       details_json, updated_at)
                   VALUES(?, 1, 'user_restricted', 'restriction race',
                          CURRENT_TIMESTAMP, '{}', CURRENT_TIMESTAMP)
                   ON CONFLICT(account_id) DO UPDATE SET
                       active=1, code='user_restricted',
                       message='restriction race',
                       updated_at=CURRENT_TIMESTAMP""",
                (self.account_id,),
            )
        if dispatch_barrier is None:
            self.rpc_calls += 1
        else:
            with dispatch_barrier.dispatch():
                self.rpc_calls += 1
        return True


def _create_one_join_slot(db: Database, account_id: int):
    now = utc_now()
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        dialog_id = int(
            conn.execute(
                """INSERT INTO saved_dialogs(
                       peer_id, username, title, kind, source_account_id)
                   VALUES(?, 'target', 'target', 'channel', ?)""",
                (7001, account_id),
            ).lastrowid
        )
        campaign_id = int(
            conn.execute(
                """INSERT INTO join_campaigns(
                       account_id,status,started_at,ends_at,max_per_hour,total_count)
                   VALUES(?,'running',?,?,100,1)""",
                (account_id, to_db_time(now), to_db_time(now + timedelta(hours=1))),
            ).lastrowid
        )
        task_id = int(
            conn.execute(
                """INSERT INTO tasks(type,payload,status,progress,max_retries)
                   VALUES('join_saved_slot','{}','running',0,0)"""
            ).lastrowid
        )
        slot_id = int(
            conn.execute(
                """INSERT INTO join_schedule(
                       campaign_id,slot_index,scheduled_at,status,task_id,saved_dialog_id)
                   VALUES(?,1,?,'queued',?,?)""",
                (campaign_id, to_db_time(now), task_id, dialog_id),
            ).lastrowid
        )
    return campaign_id, task_id, slot_id


@pytest.mark.asyncio
async def test_join_slot_rechecks_restricted_state_at_dispatch(tmp_path):
    account_id = 302
    db = Database(tmp_path / "join-restricted-race.db")
    db.set_setting("telegram.account_id", account_id)
    campaign_id, task_id, slot_id = _create_one_join_slot(db, account_id)
    worker = QueueWorker(lambda: {})
    worker._db = db
    telegram = _JoinBoundaryTelegram(db, account_id)
    handler = create_join_slot_handler(
        as_int=lambda value, default=0: int(value if value is not None else default),
        queue_worker=worker,
        config=SimpleNamespace(min_join_interval_seconds=0),
        worker_db=db,
        telegram=telegram,
        set_runtime=lambda *_args, **_kwargs: None,
    )

    await handler(
        {
            "id": task_id,
            "payload": {
                "account_id": account_id,
                "campaign_id": campaign_id,
                "slot_id": slot_id,
            },
        }
    )

    assert db.get_account_restriction(account_id)["active"] is True
    assert telegram.rpc_calls == 0


class _NoopImporter:
    def __init__(self, db):
        self.db = db


class _NoopComments:
    def __init__(self, *args, **kwargs):
        pass


class _LinkTelegram:
    def __init__(self, db: Database, account_id: int):
        self.db = db
        self.account_id = account_id
        self.join_calls = 0

    def register_peer_reference(self, *_args, **_kwargs):
        return None

    @staticmethod
    def is_channel_peer(value):
        return value is not None

    async def join_without_confirmation(self, peer_id, *, dispatch_barrier=None):
        activate_account_restriction(
            self.db,
            account_id=self.account_id,
            code="user_restricted",
            message="link restriction race",
        )
        if dispatch_barrier is None:
            self.join_calls += 1
        else:
            with dispatch_barrier.dispatch():
                self.join_calls += 1
        return True

    async def disconnect(self):
        return None


class _LinkResolver:
    def __init__(self, db: Database, account_id: int, *, ban_on_read=False):
        self.db = db
        self.account_id = account_id
        self.ban_on_read = ban_on_read
        self.rpc_calls = 0

    async def get_linked_chat_id(self, channel_id, *, dispatch_barrier=None):
        if self.ban_on_read:
            self.db.ban_channel_locally(
                int(channel_id),
                "link read race",
                related_peer_id=DISCUSSION_ID,
                account_id=self.account_id,
            )
        if dispatch_barrier is None:
            self.rpc_calls += 1
        else:
            with dispatch_barrier.dispatch(GetFullChannelRequest(channel=1)):
                self.rpc_calls += 1
        return DISCUSSION_ID


def _build_link_handler(db, worker, telegram, resolver):
    host = SimpleNamespace(
        queue_worker=worker,
        config=SimpleNamespace(
            rate_limit=0.01,
            max_joins_per_hour=40,
            min_join_interval_seconds=1,
            post_join_delay_min_seconds=0,
            post_join_delay_max_seconds=0,
            link_join_delay_min_seconds=0,
            link_join_delay_max_seconds=0,
            link_check_delay_min_seconds=0,
            link_check_delay_max_seconds=0,
        ),
        _telegram_settings=lambda _db: TelegramSettings(
            api_id=1, api_hash="x", session_dir=Path("/tmp")
        ),
        _as_int=lambda value, default=0: int(
            value if value not in (None, "") else default
        ),
        api=None,
    )
    handlers, _cleanup = create_worker_handlers(
        host,
        TelegramService=lambda *_a, **_kw: telegram,
        ImportService=_NoopImporter,
        LinkedChatService=lambda _telegram: resolver,
        CommentService=_NoopComments,
    )
    return handlers["link_channels"]


@pytest.mark.asyncio
async def test_link_resolution_read_rpc_obeys_fresh_local_ban(tmp_path):
    account_id = 303
    db = Database(tmp_path / "link-read-race.db")
    _insert_source(db, account_id)
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE channels SET linked_chat_id=NULL, link_checked_at=NULL WHERE channel_id=?",
            (SOURCE_ID,),
        )
    worker = QueueWorker(lambda: {})
    worker._db = db
    telegram = _LinkTelegram(db, account_id)
    resolver = _LinkResolver(db, account_id, ban_on_read=True)
    handler = _build_link_handler(db, worker, telegram, resolver)
    db.insert_task("link_channels", {"account_id": account_id})
    task = db.claim_next_pending_task()
    assert task is not None

    await handler(task)

    assert resolver.rpc_calls == 0
    assert telegram.join_calls == 0


@pytest.mark.asyncio
async def test_link_join_rechecks_restricted_state_at_dispatch(tmp_path):
    account_id = 304
    db = Database(tmp_path / "link-restricted-race.db")
    _insert_source(db, account_id)
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE channels SET linked_chat_id=NULL, link_checked_at=NULL WHERE channel_id=?",
            (SOURCE_ID,),
        )
    worker = QueueWorker(lambda: {})
    worker._db = db
    telegram = _LinkTelegram(db, account_id)
    resolver = _LinkResolver(db, account_id)
    handler = _build_link_handler(db, worker, telegram, resolver)
    db.insert_task("link_channels", {"account_id": account_id})
    task = db.claim_next_pending_task()
    assert task is not None

    with pytest.raises(Exception) as raised:
        await handler(task)

    assert getattr(raised.value, "code", "") == "user_restricted"
    assert db.get_account_restriction(account_id)["active"] is True
    assert telegram.join_calls == 0


def test_v28_migration_late_failure_rolls_back_all_changes(tmp_path, monkeypatch):
    import sqlite3
    from storage.migrations import safety_invariants_v28 as migration

    path = tmp_path / "schema-27-late-failure.db"
    raw = sqlite3.connect(path)
    try:
        raw.executescript(
            """
            PRAGMA user_version = 27;
            CREATE TABLE migrations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL UNIQUE,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO migrations(version) VALUES(27);
            CREATE TABLE channels(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL DEFAULT 0,
                channel_id INTEGER NOT NULL,
                local_ban_reason TEXT,
                local_ban_peer_id INTEGER,
                local_banned_at DATETIME
            );
            INSERT INTO channels(
                account_id, channel_id, local_ban_reason,
                local_ban_peer_id, local_banned_at
            ) VALUES(77, -1001001, 'unknown join', -1002002, CURRENT_TIMESTAMP);
            """
        )
        raw.commit()
    finally:
        raw.close()

    real_connect = sqlite3.connect

    class LateFailureConnection:
        def __init__(self, connection):
            self._connection = connection

        @property
        def row_factory(self):
            return self._connection.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self._connection.row_factory = value

        def execute(self, sql, parameters=()):
            if str(sql).strip() == "PRAGMA user_version = 28":
                raise sqlite3.OperationalError("injected late migration failure")
            return self._connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    monkeypatch.setattr(
        migration.sqlite3,
        "connect",
        lambda *args, **kwargs: LateFailureConnection(real_connect(*args, **kwargs)),
    )

    with pytest.raises(
        sqlite3.OperationalError, match="injected late migration failure"
    ):
        migration.migrate_safety_invariants_v28(
            path,
            sqlite_timeout_seconds=5.0,
            busy_timeout_ms=5000,
        )

    check = real_connect(path)
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 27
        assert (
            check.execute(
                "SELECT COUNT(*) FROM migrations WHERE version=28"
            ).fetchone()[0]
            == 0
        )
        assert (
            check.execute(
                "SELECT COUNT(*) FROM channels WHERE account_id=77 AND channel_id=-1001001"
            ).fetchone()[0]
            == 1
        )
        assert (
            check.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='local_ban_targets'"
            ).fetchone()[0]
            == 0
        )
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        check.close()

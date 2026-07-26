from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.campaign_schedule import to_db_time, utc_now
from core.config import TelegramSettings
from core.exceptions import NonRetryableTelegramError, TaskPausedError
from storage.database import Database
from workers.handler_registry import create_worker_handlers
from workers.handlers.join_slot import create_join_slot_handler
from workers.queue_worker import QueueWorker


class _LinkQueue:
    def __init__(self, db) -> None:
        self.db = db
        self.cancelled = False

    def get_db(self):
        return self.db

    def isInterruptionRequested(self):
        return False

    def is_scope_cancelled(self, scope, scope_id, account_id=None):
        return bool(self.cancelled and scope == "task")

    async def safe_sleep(self, seconds, *, cancel_scope=None):
        return True


class _LinkTelegram:
    def __init__(self) -> None:
        self.join_calls: list[int] = []

    async def join_without_confirmation(self, peer_id, **kwargs):
        self.join_calls.append(int(peer_id))
        return True

    async def disconnect(self):
        return None

    @staticmethod
    def is_channel_peer(value):
        return value is not None

    def register_peer_reference(self, *_args, **_kwargs):
        return None


class _LinkResolver:
    def __init__(self, callback) -> None:
        self.callback = callback

    async def get_linked_chat_id(self, channel_id):
        self.callback()
        return 2002


class _NoopImporter:
    def __init__(self, db):
        self.db = db


class _NoopComments:
    def __init__(self, *args, **kwargs):
        pass


def _build_link_handler(db, queue, telegram, resolver):
    host = SimpleNamespace(
        queue_worker=queue,
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
    return handlers


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["stop", "ban"])
async def test_link_join_is_blocked_when_state_changes_after_resolve(race):
    state = {"banned": False}
    row = {
        "channel_id": 1001,
        "title": "Target",
        "target_kind": "channel",
        "linked_chat_id": None,
        "link_checked_at": None,
        "local_banned_at": None,
    }
    db = MagicMock()
    db.get_setting.side_effect = lambda key, default=None: (
        77 if key == "telegram.account_id" else default
    )
    db.get_channels.return_value = [dict(row)]
    db.get_channel_by_id.side_effect = lambda *_a, **_kw: {
        **row,
        "local_banned_at": "2026-07-20 00:00:00" if state["banned"] else None,
    }
    db.update_task_checkpoint.return_value = True
    queue = _LinkQueue(db)
    telegram = _LinkTelegram()

    def after_resolve():
        if race == "stop":
            queue.cancelled = True
        else:
            state["banned"] = True

    handlers = _build_link_handler(db, queue, telegram, _LinkResolver(after_resolve))
    if race == "stop":
        with pytest.raises(TaskPausedError):
            await handlers["link_channels"]({"id": 901, "payload": {}})
    else:
        await handlers["link_channels"]({"id": 901, "payload": {}})
    assert telegram.join_calls == []
    assert db.get_channel_by_id.call_count >= 2


def test_dispatch_barrier_checks_current_local_state():
    worker = QueueWorker(lambda: {})
    state = {"banned": False}
    barrier = worker.create_scope_dispatch_barrier(
        ("task", 1), pre_dispatch_check=lambda: not state["banned"]
    )
    with barrier.dispatch():
        pass
    state["banned"] = True
    with pytest.raises(Exception) as raised:
        with barrier.dispatch():
            pass
    assert getattr(raised.value, "code", "") == "local_ban_before_dispatch"


def test_related_peer_ban_and_cross_campaign_delivery_survive_restart(tmp_path):
    path = tmp_path / "v28.db"
    db = Database(path)
    db.set_setting("telegram.account_id", 77)
    db.upsert_channels_batch(
        [
            {
                "channel_id": 1001,
                "title": "Source",
                "target_kind": "channel",
                "comment_mode": "channel_post",
                "linked_chat_id": 2002,
            }
        ],
        account_id=77,
    )
    assert db.ban_channel_locally(1001, "unknown", related_peer_id=2002, account_id=77)
    assert db.is_channel_locally_banned(1001, account_id=77)
    assert db.is_channel_locally_banned(2002, account_id=77)
    with db.get_connection() as conn:
        persisted_targets = {
            int(row[0])
            for row in conn.execute(
                "SELECT peer_id FROM local_ban_targets WHERE account_id=77"
            ).fetchall()
        }
    assert persisted_targets == {1001, 2002}
    assert db.reserve_comment_delivery(1001, 50, account_id=77, campaign_id=1)
    db.mark_comment_delivery_uncertain(1001, 50, "lost", account_id=77, campaign_id=1)
    db.close_thread_connection()

    restarted = Database(path)
    assert restarted.is_channel_locally_banned(2002, account_id=77)
    assert not restarted.reserve_comment_delivery(
        1001, 50, account_id=77, campaign_id=2
    )
    with restarted.get_connection() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


class _JoinQueue:
    def __init__(self) -> None:
        self.cancelled: list[tuple] = []

    def is_scope_cancelled(self, *_args):
        return False

    def cancel_scopes_and_run(self, scopes, mutation):
        self.cancelled.extend(scopes)
        return mutation()

    def request_scope_cancellation(self, *scope):
        self.cancelled.append(scope)


class _JoinTelegram:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def join_saved_dialog(self, *, username=None, invite_link=None, **kwargs):
        self.calls.append(str(username))
        if username == "target_b":
            raise NonRetryableTelegramError("unknown", code="join_result_unknown")
        return True


def _create_join_fixture(db: Database):
    now = utc_now()
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        dialog_ids = []
        for index, username in enumerate(("target_a", "target_b", "target_c"), start=1):
            dialog_ids.append(
                int(
                    conn.execute(
                        """INSERT INTO saved_dialogs(
                               peer_id, username, title, kind, source_account_id)
                           VALUES(?, ?, ?, 'channel', 5)""",
                        (7000 + index, username, username),
                    ).lastrowid
                )
            )
        campaign_id = int(
            conn.execute(
                """INSERT INTO join_campaigns(
                       account_id,status,started_at,ends_at,max_per_hour,total_count)
                   VALUES(5,'running',?,?,100,3)""",
                (to_db_time(now), to_db_time(now + timedelta(hours=1))),
            ).lastrowid
        )
        task_ids = []
        slot_ids = []
        for index, dialog_id in enumerate(dialog_ids, start=1):
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
                       VALUES(?,?,?,'queued',?,?)""",
                    (campaign_id, index, to_db_time(now), task_id, dialog_id),
                ).lastrowid
            )
            task_ids.append(task_id)
            slot_ids.append(slot_id)
    return campaign_id, task_ids, slot_ids


@pytest.mark.asyncio
async def test_unknown_join_bans_only_target_and_batch_continues(tmp_path):
    db = Database(tmp_path / "join-batch.db")
    db.set_setting("telegram.account_id", 5)
    campaign_id, task_ids, slot_ids = _create_join_fixture(db)
    queue = _JoinQueue()
    telegram = _JoinTelegram()
    handler = create_join_slot_handler(
        as_int=lambda value, default=0: int(value if value is not None else default),
        queue_worker=queue,
        config=SimpleNamespace(min_join_interval_seconds=0),
        worker_db=db,
        telegram=telegram,
        set_runtime=lambda *_a, **_kw: None,
    )

    for task_id, slot_id in zip(task_ids, slot_ids):
        await handler(
            {
                "id": task_id,
                "payload": {
                    "account_id": 5,
                    "campaign_id": campaign_id,
                    "slot_id": slot_id,
                },
            }
        )

    schedule = db.get_join_schedule(campaign_id)
    assert [row["status"] for row in schedule] == ["joined", "uncertain", "joined"]
    assert db.is_channel_locally_banned(7002, account_id=5)
    assert db.get_join_campaign(campaign_id)["status"] == "completed"
    assert telegram.calls == ["target_a", "target_b", "target_c"]

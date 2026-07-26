from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.campaign_schedule import to_db_time, utc_now
from core.exceptions import DeferredTelegramError
from services.api_parts.task_queue import TaskQueueAPIMixin
from storage.database import Database
from workers.comment_slot.handler import create_comment_slot_handler
from workers.handlers.join_slot import create_join_slot_handler
from workers.queue_worker import QueueWorker


def _bind_channel_delete_api(database: Database, worker: QueueWorker):
    host = SimpleNamespace(database=database, queue_worker=worker)
    host._cancel_scopes_and_mutate = (
        TaskQueueAPIMixin._cancel_scopes_and_mutate.__get__(host)
    )
    host._request_scope_cancellation = (
        TaskQueueAPIMixin._request_scope_cancellation.__get__(host)
    )
    host.delete_channels = TaskQueueAPIMixin.delete_channels.__get__(host)
    return host


def _prepare_account_work(
    db: Database,
    *,
    account_id: int,
    peer_id: int,
    dialog_id: int,
) -> dict[str, int]:
    db.upsert_channels_batch(
        [
            {
                "channel_id": peer_id,
                "username": f"peer_{account_id}",
                "title": f"Peer {account_id}",
                "comment_mode": "channel_post",
                "linked_chat_id": peer_id - 1,
                "link_status": "linked",
            }
        ],
        account_id=account_id,
    )
    db.set_saved_dialog_membership(dialog_id, account_id, "left")

    comment_campaign = db.create_comment_campaign(
        [f"comment-{account_id}"],
        daily_limit=1,
        slot_count=1,
        account_id=account_id,
    )
    comment_task = db.insert_task(
        "auto_comment_slot",
        {
            "account_id": account_id,
            "campaign_id": comment_campaign["id"],
            "slot_id": 1,
        },
        0,
    )
    join_campaign = db.create_join_campaign(account_id, max_per_hour=1000)
    join_task = db.insert_task(
        "join_saved_slot",
        {
            "account_id": account_id,
            "campaign_id": join_campaign["id"],
            "slot_id": 1,
        },
        0,
    )

    with db.get_connection() as conn:
        comment_slot = int(
            conn.execute(
                "SELECT id FROM comment_schedule WHERE campaign_id=?",
                (comment_campaign["id"],),
            ).fetchone()[0]
        )
        join_slot = int(
            conn.execute(
                "SELECT id FROM join_schedule WHERE campaign_id=?",
                (join_campaign["id"],),
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE tasks SET status='running' WHERE id IN (?, ?)",
            (comment_task, join_task),
        )
        conn.execute(
            """UPDATE comment_schedule
               SET status='running', task_id=?, channel_id=? WHERE id=?""",
            (comment_task, peer_id, comment_slot),
        )
        conn.execute(
            "UPDATE join_schedule SET status='running', task_id=? WHERE id=?",
            (join_task, join_slot),
        )

    return {
        "comment_campaign": int(comment_campaign["id"]),
        "comment_slot": comment_slot,
        "comment_task": int(comment_task),
        "join_campaign": int(join_campaign["id"]),
        "join_slot": join_slot,
        "join_task": int(join_task),
    }


def test_delete_channel_sql_is_account_scoped_for_same_peer(tmp_path: Path) -> None:
    db = Database(tmp_path / "account-delete.db")
    account_a = 101
    account_b = 202
    peer_id = -100777001
    db.set_setting("telegram.account_id", account_a)

    dialog_id = db.upsert_saved_dialog(
        {
            "peer_id": peer_id,
            "username": "shared_peer",
            "title": "Shared peer",
            "kind": "channel",
        },
        account_id=account_a,
    )
    work_a = _prepare_account_work(
        db, account_id=account_a, peer_id=peer_id, dialog_id=dialog_id
    )
    work_b = _prepare_account_work(
        db, account_id=account_b, peer_id=peer_id, dialog_id=dialog_id
    )

    result = db.delete_channels_transactional([peer_id], account_id=account_a)

    assert result["cancelled_task_ids"] == sorted(
        [work_a["comment_task"], work_a["join_task"]]
    )
    assert result["comment_slot_count"] == 1
    assert result["join_slot_count"] == 1
    assert result["deleted_membership_count"] == 1
    assert result["saved_dialog_count"] == 0

    with db.get_connection() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM channels WHERE account_id=? AND channel_id=?",
                (account_a, peer_id),
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM channels WHERE account_id=? AND channel_id=?",
                (account_b, peer_id),
            ).fetchone()
            is not None
        )

        assert (
            conn.execute(
                "SELECT status FROM comment_schedule WHERE id=?",
                (work_a["comment_slot"],),
            ).fetchone()[0]
            == "cancelled"
        )
        assert (
            conn.execute(
                "SELECT status FROM comment_schedule WHERE id=?",
                (work_b["comment_slot"],),
            ).fetchone()[0]
            == "running"
        )

        assert (
            conn.execute(
                "SELECT 1 FROM join_schedule WHERE id=?", (work_a["join_slot"],)
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT status FROM join_schedule WHERE id=?", (work_b["join_slot"],)
            ).fetchone()[0]
            == "running"
        )

        assert (
            conn.execute(
                "SELECT status FROM tasks WHERE id=?", (work_a["comment_task"],)
            ).fetchone()[0]
            == "cancelled"
        )
        assert (
            conn.execute(
                "SELECT status FROM tasks WHERE id=?", (work_a["join_task"],)
            ).fetchone()[0]
            == "cancelled"
        )
        assert (
            conn.execute(
                "SELECT status FROM tasks WHERE id=?", (work_b["comment_task"],)
            ).fetchone()[0]
            == "running"
        )
        assert (
            conn.execute(
                "SELECT status FROM tasks WHERE id=?", (work_b["join_task"],)
            ).fetchone()[0]
            == "running"
        )

        assert (
            conn.execute(
                """SELECT 1 FROM saved_dialog_memberships
               WHERE saved_dialog_id=? AND account_id=?""",
                (dialog_id, account_a),
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                """SELECT 1 FROM saved_dialog_memberships
               WHERE saved_dialog_id=? AND account_id=?""",
                (dialog_id, account_b),
            ).fetchone()
            is not None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM saved_dialogs WHERE id=?", (dialog_id,)
            ).fetchone()
            is not None
        )


def test_channel_cancellation_scope_is_account_scoped() -> None:
    worker = QueueWorker(lambda: {})
    peer_id = -100888002
    account_a = 101
    account_b = 202

    worker.request_scope_cancellation("channel", peer_id, account_a)

    assert worker.is_scope_cancelled("channel", peer_id, account_a)
    assert not worker.is_scope_cancelled("channel", peer_id, account_b)

    with worker.create_scope_dispatch_barrier(
        ("channel", peer_id, account_b)
    ).dispatch():
        pass

    with pytest.raises(DeferredTelegramError):
        with worker.create_scope_dispatch_barrier(
            ("channel", peer_id, account_a)
        ).dispatch():
            pass


def test_full_delete_accepts_negative_marked_peer_and_preserves_other_scope(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "negative-peer-delete.db")
    account_a = 303
    account_b = 404
    peer_id = -123456
    db.set_setting("telegram.account_id", account_a)
    db.upsert_channels_batch(
        [
            {
                "channel_id": peer_id,
                "username": None,
                "title": "Marked group",
                "target_kind": "group",
                "comment_mode": "direct_group",
            }
        ],
        account_id=account_a,
    )
    db.upsert_channels_batch(
        [
            {
                "channel_id": peer_id,
                "username": None,
                "title": "Marked group B",
                "target_kind": "group",
                "comment_mode": "direct_group",
            }
        ],
        account_id=account_b,
    )

    worker = QueueWorker(lambda: {})
    api = _bind_channel_delete_api(db, worker)
    result = api.delete_channels([peer_id])

    assert result["deleted_channel_count"] == 1
    assert worker.is_scope_cancelled("channel", peer_id, account_a)
    assert not worker.is_scope_cancelled("channel", peer_id, account_b)
    with db.get_connection() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM channels WHERE account_id=? AND channel_id=?",
                (account_a, peer_id),
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM channels WHERE account_id=? AND channel_id=?",
                (account_b, peer_id),
            ).fetchone()
            is not None
        )


def _as_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _make_due_direct_group_task(
    db: Database, *, account_id: int, peer_id: int
) -> tuple[dict, dict]:
    db.set_setting("telegram.account_id", account_id)
    db.upsert_channels_batch(
        [
            {
                "channel_id": peer_id,
                "title": "Account-scoped group",
                "target_kind": "group",
                "comment_mode": "direct_group",
                "linked_chat_id": peer_id,
                "link_status": "Группа · связь не обнаружена",
            }
        ],
        account_id=account_id,
    )
    campaign = db.create_comment_campaign(
        ["account-scoped message"],
        daily_limit=1,
        slot_count=1,
        continuous=False,
        start_at=utc_now() - timedelta(hours=1),
        account_id=account_id,
        rng=random.Random(11),
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
    return campaign, task


class _BarrierCapturingComments:
    def __init__(self) -> None:
        self.calls = 0
        self.scopes: tuple[tuple[str, int], ...] = ()

    async def send_direct_message(self, _chat_id, _text, **kwargs) -> None:
        barrier = kwargs["dispatch_barrier"]
        self.scopes = barrier._scopes
        with barrier.dispatch():
            self.calls += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled_account", (701, 702))
async def test_legacy_direct_group_never_dispatches_after_v25(
    tmp_path: Path, cancelled_account: int
) -> None:
    account_id = 701
    peer_id = -1007001
    db = Database(tmp_path / f"comment-handler-{cancelled_account}.db")
    campaign, task = _make_due_direct_group_task(
        db, account_id=account_id, peer_id=peer_id
    )
    worker = QueueWorker(lambda: {})
    worker._db = db
    worker.request_scope_cancellation("channel", peer_id, cancelled_account)
    comments = _BarrierCapturingComments()
    handler = create_comment_slot_handler(
        as_int=_as_int,
        queue_worker=worker,
        config=SimpleNamespace(
            post_join_delay_min_seconds=0,
            post_join_delay_max_seconds=0,
        ),
        worker_db=db,
        telegram=SimpleNamespace(),
        comments=comments,
        set_runtime=lambda *_args, **_kwargs: None,
    )
    worker._handlers = {"auto_comment_slot": handler}

    await worker._process_task(task)

    assert comments.calls == 0
    assert comments.scopes == ()
    assert db.get_comment_campaign(campaign["id"])["sent_count"] == 0


def _make_due_join_task(
    db: Database, *, account_id: int, peer_id: int
) -> tuple[dict, dict]:
    db.set_setting("telegram.account_id", account_id)
    dialog_id = db.upsert_saved_dialog(
        {
            "peer_id": peer_id,
            "username": f"join_{account_id}",
            "title": "Account-scoped join",
            "kind": "channel",
        },
        account_id=account_id,
    )
    db.set_saved_dialog_membership(dialog_id, account_id, "left")
    campaign = db.create_join_campaign(
        account_id, max_per_hour=1000, rng=random.Random(12)
    )
    slot = db.get_join_schedule(campaign["id"], limit=1)[0]
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE join_schedule SET scheduled_at=? WHERE id=?",
            (to_db_time(utc_now() - timedelta(seconds=1)), int(slot["id"])),
        )
    assert db.queue_due_join_slot(now=utc_now()) is not None
    task = db.claim_next_pending_task()
    assert task is not None
    return campaign, task


class _BarrierCapturingTelegram:
    def __init__(self) -> None:
        self.calls = 0
        self.scopes: tuple[tuple[str, int], ...] = ()

    async def join_saved_dialog(self, **kwargs) -> bool:
        barrier = kwargs["dispatch_barrier"]
        self.scopes = barrier._scopes
        with barrier.dispatch():
            self.calls += 1
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancelled_account", "expected_calls"),
    ((801, 0), (802, 1)),
)
async def test_join_handler_uses_account_scoped_channel_barrier(
    tmp_path: Path, cancelled_account: int, expected_calls: int
) -> None:
    account_id = 801
    peer_id = -1008001
    db = Database(tmp_path / f"join-handler-{cancelled_account}.db")
    campaign, task = _make_due_join_task(db, account_id=account_id, peer_id=peer_id)
    worker = QueueWorker(lambda: {})
    worker._db = db
    worker.request_scope_cancellation("channel", peer_id, cancelled_account)
    telegram = _BarrierCapturingTelegram()
    handler = create_join_slot_handler(
        as_int=_as_int,
        queue_worker=worker,
        config=SimpleNamespace(min_join_interval_seconds=1),
        worker_db=db,
        telegram=telegram,
        set_runtime=lambda *_args, **_kwargs: None,
    )
    worker._handlers = {"join_saved_slot": handler}

    await worker._process_task(task)

    assert telegram.calls == expected_calls
    if expected_calls:
        assert (f"channel:{account_id}", peer_id) in telegram.scopes
        assert db.get_join_campaign(campaign["id"])["joined_count"] == 1
    else:
        assert db.get_join_campaign(campaign["id"])["joined_count"] == 0

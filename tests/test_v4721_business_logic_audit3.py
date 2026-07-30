from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon.errors import InviteRequestSentError

from core.campaign_schedule import generate_random_slots, to_db_time, utc_now
from core.exceptions import NonRetryableTelegramError
from services.api import ServiceAPI
from services.comment_service import CommentService
from services.telegram.transport import TelegramTransportMixin
from storage.database import Database
from workers.comment_slot.finalization import finalize_comment_slot
from workers.comment_slot.handler import create_comment_slot_handler
from workers.handlers.join_slot import create_join_slot_handler
from workers.queue_worker import QueueWorker

UTC = timezone.utc


class _NoExternalCalls:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __getattr__(self, name):
        async def fail_if_called(*args, **kwargs):
            self.calls.append((name, (args, kwargs)))
            raise AssertionError(
                f"Telegram call started despite account mismatch: {name}"
            )

        return fail_if_called


class _CommentSink:
    async def ensure_and_send_comment(self, **_kwargs):
        raise AssertionError("comment send must not start")

    async def send_direct_message(self, *_args, **_kwargs):
        raise AssertionError("direct send must not start")


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _make_due_comment_task(db: Database, account_id: int):
    db.set_setting("telegram.account_id", account_id)
    db.insert_channel(
        {
            "channel_id": 10,
            "linked_chat_id": 20,
            "title": "Account-owned source",
        }
    )
    campaign = db.create_comment_campaign(
        ["snapshot"],
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
    queued = db.queue_due_comment_slot(now=utc_now())
    assert queued is not None
    task = db.claim_next_pending_task()
    assert task is not None
    return campaign, slot, task


def _make_due_join_task(db: Database, account_id: int):
    db.set_setting("telegram.account_id", account_id)
    dialog_id = db.upsert_saved_dialog(
        {
            "peer_id": 555,
            "username": "audit_target",
            "title": "Audit target",
            "kind": "channel",
        },
        account_id=account_id,
    )
    db.set_saved_dialog_membership(dialog_id, account_id, "left")
    campaign = db.create_join_campaign(account_id, rng=random.Random(2))
    slot = db.get_join_schedule(campaign["id"], limit=1)[0]
    assert int(slot["saved_dialog_id"]) == dialog_id
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE join_schedule SET scheduled_at=? WHERE id=?",
            (to_db_time(utc_now() - timedelta(seconds=1)), int(slot["id"])),
        )
    queued = db.queue_due_join_slot(now=utc_now())
    assert queued is not None
    task = db.claim_next_pending_task()
    assert task is not None
    return campaign, slot, task, dialog_id


def test_real_comment_atomic_finalizer_is_bound_and_updates_all_local_state(tmp_path):
    db = Database(tmp_path / "comment-finalizer-bound.db")
    campaign, slot, task = _make_due_comment_task(db, 909)

    finalize_comment_slot(
        worker_db=db,
        task_id=int(task["id"]),
        slot_id=int(slot["id"]),
        campaign_id=int(campaign["id"]),
        channel_id=10,
        post_id=15,
        selected="snapshot",
        final_status="sent",
        final_message="Комментарий отправлен",
        sent=True,
        consume_channel=True,
        campaign_pause_reason=None,
        internal_error=None,
        slot_deferred=False,
    )

    stored_campaign = db.get_comment_campaign(campaign["id"])
    stored_slot = db.get_comment_schedule(campaign["id"], limit=1)[0]
    stored_task = db.get_task(task["id"])
    history = db.get_comment_history(campaign_id=campaign["id"], account_id=909)
    assert stored_slot["status"] == "sent"
    assert stored_task["status"] == "completed"
    assert stored_campaign["sent_count"] == 1
    assert stored_campaign["attempted_count"] == 1
    assert len(history) == 1
    assert history[0]["post_id"] == 15


@pytest.mark.asyncio
async def test_manual_telegram_task_snapshots_account_and_is_blocked_after_switch(
    tmp_path,
):
    db = Database(tmp_path / "manual-account-isolation.db")
    db.register_telegram_account(
        telegram_account_id=1111,
        session_name="account_1111",
        display_name="Manual isolation account",
        authorized=True,
    )
    db.set_setting("telegram.account_id", 1111)
    db.set_setting("ui.selected_account_id", 1111)
    api = ServiceAPI(db)
    assert api.wait_for_secret_migration(5_000)
    created = api.create_task(
        "comment",
        {"channel_id": 42, "post_id": 7, "text": "snapshot"},
    )
    api.prepare_shutdown()
    assert created["payload"]["account_id"] == 1111

    task = db.claim_next_pending_task()
    assert task is not None
    db.set_setting("telegram.account_id", 2222)
    calls: list[int] = []

    async def handler(_task):
        calls.append(1)

    queue = QueueWorker(lambda: {})
    queue._db = db
    queue._handlers = {"comment": handler}
    await queue._process_task(task)

    assert calls == []
    stored = db.get_task(task["id"])
    assert stored["status"] == "failed"
    assert "account_state_mismatch" in stored["error"]


@pytest.mark.asyncio
async def test_comment_task_never_runs_through_another_account_session(tmp_path):
    db = Database(tmp_path / "comment-account-isolation.db")
    campaign, slot, task = _make_due_comment_task(db, 111)
    db.set_setting("telegram.account_id", 222)
    telegram = _NoExternalCalls()
    queue = QueueWorker(lambda: {})
    queue._db = db
    handler = create_comment_slot_handler(
        as_int=_as_int,
        queue_worker=queue,
        config=SimpleNamespace(
            post_join_delay_min_seconds=0,
            post_join_delay_max_seconds=0,
        ),
        worker_db=db,
        telegram=telegram,
        comments=_CommentSink(),
        set_runtime=lambda *_args, **_kwargs: None,
    )
    queue._handlers = {"auto_comment_slot": handler}

    await queue._process_task(task)

    assert telegram.calls == []
    assert db.get_task(task["id"])["status"] == "failed"
    assert "account_state_mismatch" in db.get_task(task["id"])["error"]
    assert db.get_comment_campaign(campaign["id"])["status"] == "paused"
    assert db.get_comment_schedule(campaign["id"], limit=1)[0]["status"] == "pending"
    assert int(task["payload"]["account_id"]) == 111
    assert int(slot["id"]) > 0


@pytest.mark.asyncio
async def test_join_task_never_runs_through_another_account_session(tmp_path):
    db = Database(tmp_path / "join-account-isolation.db")
    campaign, _slot, task, _dialog_id = _make_due_join_task(db, 333)
    db.set_setting("telegram.account_id", 444)
    telegram = _NoExternalCalls()
    queue = QueueWorker(lambda: {})
    queue._db = db
    handler = create_join_slot_handler(
        as_int=_as_int,
        queue_worker=queue,
        config=SimpleNamespace(min_join_interval_seconds=1),
        worker_db=db,
        telegram=telegram,
        set_runtime=lambda *_args, **_kwargs: None,
    )
    queue._handlers = {"join_saved_slot": handler}

    await queue._process_task(task)

    assert telegram.calls == []
    assert db.get_task(task["id"])["status"] == "failed"
    assert "account_state_mismatch" in db.get_task(task["id"])["error"]
    assert db.get_join_campaign(campaign["id"])["status"] == "paused"
    assert db.get_join_schedule(campaign["id"], limit=1)[0]["status"] == "pending"


def test_sources_history_and_delivery_keys_are_account_scoped(tmp_path):
    db = Database(tmp_path / "account-scoped-ledger.db")
    for account_id, title, message_id in ((1, "A", 701), (2, "B", 702)):
        db.set_setting("telegram.account_id", account_id)
        db.insert_channel({"channel_id": 50, "linked_chat_id": 60, "title": title})
        assert db.reserve_comment_delivery(
            50, 15, linked_chat_id=60, text=title, account_id=account_id
        )
        db.finalize_comment_delivery(
            {
                "account_id": account_id,
                "channel_id": 50,
                "linked_chat_id": 60,
                "post_message_id": 15,
                "comment_message_id": message_id,
                "reply_to": 72 if account_id == 1 else 91,
                "author_id": account_id,
                "text": title,
                "date": "2026-07-15T00:00:00+00:00",
            }
        )

    assert db.get_channel_by_id(50, account_id=1)["title"] == "A"
    assert db.get_channel_by_id(50, account_id=2)["title"] == "B"
    assert db.has_commented(50, 15, account_id=1)
    assert db.has_commented(50, 15, account_id=2)
    assert not db.reserve_comment_delivery(50, 15, account_id=1)
    assert not db.reserve_comment_delivery(50, 15, account_id=2)
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT account_id, comment_message_id, reply_to FROM comments "
            "WHERE channel_id=50 AND post_message_id=15 ORDER BY account_id"
        ).fetchall()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (1, 701, 72),
        (2, 702, 91),
    ]


class _NoIdTelegram:
    async def send_comment(self, *_args, **_kwargs):
        return SimpleNamespace(id=None)

    async def send_message(self, *_args, **_kwargs):
        return SimpleNamespace(id=None)


@pytest.mark.asyncio
async def test_comment_without_confirmed_telegram_message_id_is_uncertain(tmp_path):
    db = Database(tmp_path / "comment-no-id.db")
    db.set_setting("telegram.account_id", 7)
    db.insert_channel({"channel_id": 10, "linked_chat_id": 20, "title": "X"})
    service = CommentService(_NoIdTelegram(), db=db)

    with pytest.raises(NonRetryableTelegramError) as caught:
        await service.ensure_and_send_comment(
            channel_id=10,
            linked_chat_id=20,
            post_message_id=15,
            text="snapshot",
            membership_ready=True,
            account_id=7,
        )

    assert caught.value.code == "delivery_result_unknown"
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT status, comment_message_id FROM comment_deliveries "
            "WHERE account_id=7 AND channel_id=10 AND post_id=15"
        ).fetchone()
    assert tuple(row) == ("uncertain", None)


@pytest.mark.asyncio
async def test_direct_message_service_is_fail_closed_without_telegram_call(tmp_path):
    db = Database(tmp_path / "direct-disabled.db")
    task_id = db.insert_task("direct_message", {"chat_id": 10, "text": "x"})
    telegram = _NoIdTelegram()
    service = CommentService(telegram, db=db)

    with pytest.raises(NonRetryableTelegramError) as caught:
        await service.send_direct_message(10, "x", task_id=task_id)
    assert caught.value.code == "direct_group_disabled"
    assert db.get_direct_message_delivery(task_id) is None


class _JoinRequestedTelegram:
    async def is_member(self, _target):
        return False

    async def join_saved_dialog(self, **_kwargs):
        raise NonRetryableTelegramError("request sent", code="join_requested")


@pytest.mark.asyncio
async def test_join_request_is_distinct_from_joined_and_does_not_spend_join_quota(
    tmp_path,
):
    db = Database(tmp_path / "join-requested.db")
    campaign, _slot, task, dialog_id = _make_due_join_task(db, 8)
    queue = QueueWorker(lambda: {})
    queue._db = db
    handler = create_join_slot_handler(
        as_int=_as_int,
        queue_worker=queue,
        config=SimpleNamespace(min_join_interval_seconds=1),
        worker_db=db,
        telegram=_JoinRequestedTelegram(),
        set_runtime=lambda *_args, **_kwargs: None,
    )
    queue._handlers = {"join_saved_slot": handler}

    await queue._process_task(task)

    slot = db.get_join_schedule(campaign["id"], limit=1)[0]
    stored_campaign = db.get_join_campaign(campaign["id"])
    membership = db.get_saved_dialogs(8)[0]
    assert slot["status"] == "join_requested"
    assert membership["id"] == dialog_id
    assert membership["membership_status"] == "join_requested"
    assert stored_campaign["attempted_count"] == 1
    assert stored_campaign["joined_count"] == 0
    assert (
        db.get_join_guard(
            max_joins=1,
            min_interval_seconds=1,
            window_seconds=3600,
            account_id=8,
        )["remaining"]
        == 1
    )


class _ImmediateLimiter:
    async def acquire(self):
        return None


class _TransportHarness(TelegramTransportMixin):
    def __init__(self) -> None:
        self.client = SimpleNamespace(_marlen_request_pacing=False)
        self.limiter = _ImmediateLimiter()
        self._connected = True

    async def ensure_connected(self):
        return None

    def _interruption_requested(self) -> bool:
        return False

    async def _await_interruptible(self, awaitable, timeout=None):
        return await awaitable


@pytest.mark.asyncio
async def test_transport_maps_invite_request_sent_to_definitive_join_requested():
    async def operation():
        raise InviteRequestSentError(request=None)

    with pytest.raises(NonRetryableTelegramError) as caught:
        await _TransportHarness().execute(operation, retry_network=False)
    assert caught.value.code == "join_requested"


@pytest.mark.parametrize("hours", [23, 24, 25])
@pytest.mark.parametrize("count", [0, 1, 2, 11, 40, 1000])
def test_comment_slot_math_preserves_exact_unique_one_second_slots(hours, count):
    start = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=hours)
    slots = generate_random_slots(start, end, count, rng=random.Random(1000 + count))
    assert len(slots) == count
    assert slots == sorted(slots)
    assert all(start <= slot < end for slot in slots)
    persisted = [to_db_time(slot) for slot in slots]
    assert len(set(persisted)) == count
    assert all(
        (later - earlier).total_seconds() >= 1
        for earlier, later in zip(slots, slots[1:])
    )


def test_comment_slot_math_rejects_window_one_second_too_short():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="too short"):
        generate_random_slots(
            start,
            start + timedelta(seconds=999),
            1000,
            rng=random.Random(1),
        )


def test_two_threads_queue_one_due_comment_slot_only_once(tmp_path):
    path = tmp_path / "slot-cas.db"
    db = Database(path)
    campaign, _slot, _task = _make_due_comment_task(db, 77)
    # Put the pre-created task/slot back to pending to race the production claim.
    with db.get_connection() as conn:
        conn.execute("DELETE FROM tasks")
        conn.execute(
            "UPDATE comment_schedule SET status='pending', task_id=NULL, scheduled_at=? "
            "WHERE campaign_id=?",
            (to_db_time(utc_now() - timedelta(seconds=1)), campaign["id"]),
        )
    barrier = threading.Barrier(2)
    results: list[object] = []

    def queue_once():
        local = Database(path)
        barrier.wait()
        results.append(local.queue_due_comment_slot(now=utc_now()))
        local.close_thread_connection()

    threads = [threading.Thread(target=queue_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(result is not None for result in results) == 1
    with db.get_connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE type='auto_comment_slot'"
            ).fetchone()[0]
            == 1
        )


def test_delivery_reservation_is_atomic_per_account_but_independent_between_accounts(
    tmp_path,
):
    path = tmp_path / "ledger-cas.db"
    db = Database(path)
    for account in (1, 2):
        db.insert_channel(
            {
                "account_id": account,
                "channel_id": 90,
                "linked_chat_id": 91,
                "title": str(account),
            }
        )

    results: list[tuple[int, bool]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def reserve(account: int):
        local = Database(path)
        barrier.wait()
        value = local.reserve_comment_delivery(90, 15, account_id=account)
        with lock:
            results.append((account, bool(value)))
        local.close_thread_connection()

    threads = [
        threading.Thread(target=reserve, args=(account,)) for account in (1, 1, 2, 2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [(1, False), (1, True), (2, False), (2, True)]


@dataclass
class _ReferenceCampaign:
    owner: int
    state: str = "running"
    current_account: int = 0
    source_exists: bool = True
    pending: int = 3
    attempted: int = 0
    sent: int = 0
    deliveries: dict[int, str] | None = None

    def __post_init__(self) -> None:
        self.current_account = self.owner
        self.deliveries = {}

    @property
    def can_process(self) -> bool:
        return (
            self.state == "running"
            and self.current_account == self.owner
            and self.source_exists
            and self.pending > 0
        )

    def transition(self, command: str) -> bool:
        if command == "pause" and self.state == "running":
            self.state = "paused"
            return True
        if command == "resume" and self.state == "paused":
            self.state = "running"
            return True
        if command == "stop" and self.state in {"running", "paused"}:
            self.state = "stopped"
            self.pending = 0
            return True
        if command == "change_account":
            self.current_account = self.owner + 1
            return True
        if command == "restore_account":
            self.current_account = self.owner
            return True
        if command == "delete_source":
            self.source_exists = False
            return True
        if command == "restore_source":
            self.source_exists = True
            return True
        if command == "restart":
            return True
        return False

    def finish(self, outcome: str, post_id: int) -> None:
        assert self.can_process
        self.pending -= 1
        self.attempted += 1
        if outcome == "success":
            self.sent += 1
            assert self.deliveries is not None
            self.deliveries[post_id] = "sent"
        elif outcome == "uncertain":
            assert self.deliveries is not None
            self.deliveries[post_id] = "uncertain"
        if self.pending == 0:
            self.state = "completed"


def test_model_based_100_independent_business_state_sequences(tmp_path, monkeypatch):
    commands = (
        "pause",
        "resume",
        "stop",
        "restart",
        "change_account",
        "restore_account",
        "delete_source",
        "restore_source",
        "process_success",
        "process_failure",
        "process_uncertain",
    )
    observed_outcomes: set[str] = set()
    path = tmp_path / "model-sequences.db"
    db = Database(path)
    # The constructor above exercises the real SQLCipher artifact hardening.
    # Thousands of later model transitions test campaign behavior, not ACL
    # execution latency, which has dedicated storage tests.
    monkeypatch.setattr(
        db,
        "_harden_database_artifacts",
        lambda *, force=False: None,
    )
    model_start = utc_now() + timedelta(days=1)
    for sequence_id in range(100):
        owner = 10_000 + sequence_id
        db.set_setting("telegram.account_id", owner)
        db.insert_channel(
            {
                "account_id": owner,
                "channel_id": 10,
                "linked_chat_id": 20,
                "title": f"source-{sequence_id}",
            }
        )
        campaign = db.create_comment_campaign(
            [f"snapshot-{sequence_id}"],
            daily_limit=3,
            slot_count=3,
            continuous=False,
            account_id=owner,
            rng=random.Random(sequence_id),
            start_at=model_start,
        )
        model = _ReferenceCampaign(owner=owner)
        source = random.Random(sequence_id)
        sequence = [source.choice(commands) for _ in range(14)]

        for command in sequence:
            if command == "pause":
                expected = model.transition(command)
                actual = db.pause_comment_campaign(campaign["id"], "model")
                assert bool(actual) == expected
            elif command == "resume":
                expected = model.transition(command)
                actual = db.resume_comment_campaign(
                    campaign["id"],
                    now=model_start,
                    rng=random.Random(sequence_id + 1),
                )
                assert bool(actual) == expected
            elif command == "stop":
                expected = model.transition(command)
                actual = db.stop_comment_campaign(campaign["id"], "model")
                assert bool(actual) == expected
            elif command == "restart":
                assert model.transition(command)
                db.close_thread_connection()
            elif command == "change_account":
                assert model.transition(command)
                db.set_setting("telegram.account_id", model.current_account)
            elif command == "restore_account":
                assert model.transition(command)
                db.set_setting("telegram.account_id", model.current_account)
            elif command == "delete_source":
                assert model.transition(command)
                db.prune_channels_except([], account_id=owner)
            elif command == "restore_source":
                assert model.transition(command)
                db.insert_channel(
                    {
                        "account_id": owner,
                        "channel_id": 10,
                        "linked_chat_id": 20,
                        "title": f"source-{sequence_id}",
                    }
                )
            else:
                outcome = command.removeprefix("process_")
                if model.can_process:
                    observed_outcomes.add(outcome)
                    with db.get_connection() as conn:
                        row = conn.execute(
                            "SELECT id FROM comment_schedule "
                            "WHERE campaign_id=? AND status='pending' "
                            "ORDER BY slot_index LIMIT 1",
                            (campaign["id"],),
                        ).fetchone()
                        assert row is not None
                        conn.execute(
                            "UPDATE comment_schedule SET scheduled_at=? WHERE id=?",
                            (
                                to_db_time(utc_now() - timedelta(seconds=1)),
                                int(row["id"]),
                            ),
                        )
                    queued = db.queue_due_comment_slot(now=utc_now())
                    assert queued is not None
                    task = db.claim_next_pending_task()
                    assert task is not None
                    slot_id = int(task["payload"]["slot_id"])
                    post_id = 100 + model.attempted
                    if outcome in {"success", "uncertain"}:
                        assert db.reserve_comment_delivery(
                            10,
                            post_id,
                            linked_chat_id=20,
                            text=f"snapshot-{sequence_id}",
                            account_id=owner,
                        )
                    if outcome == "success":
                        db.finalize_comment_delivery(
                            {
                                "account_id": owner,
                                "channel_id": 10,
                                "linked_chat_id": 20,
                                "post_message_id": post_id,
                                "comment_message_id": 900_000 + post_id,
                                "reply_to": post_id,
                                "author_id": owner,
                                "text": f"snapshot-{sequence_id}",
                                "date": "2026-07-15T00:00:00+00:00",
                            }
                        )
                        final_status = "sent"
                    elif outcome == "uncertain":
                        db.mark_comment_delivery_uncertain(
                            10, post_id, "model uncertain", account_id=owner
                        )
                        final_status = "uncertain"
                    else:
                        final_status = "failed"
                    finalize_comment_slot(
                        worker_db=db,
                        task_id=int(task["id"]),
                        slot_id=slot_id,
                        campaign_id=int(campaign["id"]),
                        channel_id=10,
                        post_id=post_id,
                        selected=f"snapshot-{sequence_id}",
                        final_status=final_status,
                        final_message=f"model {outcome}",
                        sent=outcome == "success",
                        consume_channel=False,
                        campaign_pause_reason=None,
                        internal_error=None,
                        slot_deferred=False,
                    )
                    model.finish(outcome, post_id)

            stored_campaign = db.get_comment_campaign(campaign["id"])
            schedule = db.get_comment_schedule(campaign["id"], limit=10)
            open_slots = sum(
                row["status"] in {"pending", "queued", "running"} for row in schedule
            )
            assert stored_campaign["status"] == model.state
            assert int(stored_campaign["account_id"]) == model.owner
            assert (
                int(db.get_setting("telegram.account_id", 0) or 0)
                == model.current_account
            )
            assert bool(db.get_channels(account_id=owner)) == model.source_exists
            assert int(stored_campaign["attempted_count"]) == model.attempted
            assert int(stored_campaign["sent_count"]) == model.sent
            assert open_slots == model.pending
            with db.get_connection() as conn:
                delivery_rows = conn.execute(
                    "SELECT post_id,status FROM comment_deliveries "
                    "WHERE account_id=? AND channel_id=10 ORDER BY post_id",
                    (owner,),
                ).fetchall()
            assert {int(row["post_id"]): row["status"] for row in delivery_rows} == (
                model.deliveries or {}
            )
            production_can_process = (
                stored_campaign["status"] == "running"
                and int(db.get_setting("telegram.account_id", 0) or 0) == owner
                and bool(db.get_channels(account_id=owner))
                and open_slots > 0
            )
            assert production_can_process == model.can_process
    db.close_thread_connection()

    assert observed_outcomes == {"success", "failure", "uncertain"}


def test_24_virtual_hour_schedule_and_ledger_simulation_has_no_duplicates(tmp_path):
    db = Database(tmp_path / "virtual-24h.db")
    start = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
    expected_total = 0
    for account_id, count in ((101, 40), (202, 1000)):
        db.set_setting("telegram.account_id", account_id)
        slots = generate_random_slots(
            start, start + timedelta(hours=24), count, rng=random.Random(account_id)
        )
        assert len({to_db_time(slot) for slot in slots}) == count
        for index in range(count):
            channel_id = account_id * 10_000 + index
            db.insert_channel(
                {
                    "account_id": account_id,
                    "channel_id": channel_id,
                    "linked_chat_id": channel_id + 1,
                    "title": f"{account_id}-{index}",
                }
            )
            assert db.reserve_comment_delivery(channel_id, 15, account_id=account_id)
            assert not db.reserve_comment_delivery(
                channel_id, 15, account_id=account_id
            )
        expected_total += count
    with db.get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM comment_deliveries").fetchone()[0]
        duplicates = conn.execute(
            """SELECT COUNT(*) FROM (
                   SELECT account_id, channel_id, post_id, COUNT(*) AS n
                   FROM comment_deliveries
                   GROUP BY account_id, channel_id, post_id HAVING n>1)"""
        ).fetchone()[0]
    assert total == expected_total == 1040
    assert duplicates == 0

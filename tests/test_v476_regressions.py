from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.campaign_schedule import generate_join_slots, utc_now
from core.exceptions import NonRetryableTelegramError
from core.secret_store import SecretStore
from services.comment_service import CommentService
from storage.database import Database, DatabaseError
from workers.handlers.join_slot import create_join_slot_handler


def test_nested_repository_call_does_not_commit_outer_transaction(tmp_path):
    db = Database(tmp_path / "nested.db")

    with pytest.raises(RuntimeError):
        with db.get_connection() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES('outer','1')")
            db.set_setting("inner", "2")
            raise RuntimeError("rollback")

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT key FROM settings WHERE key IN ('outer','inner')"
        ).fetchall()
    assert rows == []


def test_sent_comment_delivery_cannot_be_downgraded_to_uncertain(tmp_path):
    db = Database(tmp_path / "delivery.db")
    db.insert_channel({"channel_id": 10, "linked_chat_id": 20, "title": "A"})
    assert db.reserve_comment_delivery(10, 30, linked_chat_id=20, text="hello")
    db.finalize_comment_delivery(
        {
            "channel_id": 10,
            "linked_chat_id": 20,
            "post_message_id": 30,
            "comment_message_id": 40,
            "reply_to": None,
            "author_id": 50,
            "text": "hello",
            "date": None,
        }
    )

    db.mark_comment_delivery_uncertain(10, 30, "late local exception")

    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT status,error FROM comment_deliveries WHERE channel_id=10 AND post_id=30"
        ).fetchone()
    assert row["status"] == "sent"
    assert row["error"] is None


def _make_join_candidate(db: Database, *, peer_id=100, username="candidate", account=1):
    return db.upsert_saved_dialog(
        {
            "peer_id": peer_id,
            "username": username,
            "title": username,
            "kind": "channel",
        },
        account_id=account,
    )


def test_join_campaign_creation_is_atomic_across_threads(tmp_path):
    path = tmp_path / "join-race.db"
    db = Database(path)
    dialog_id = _make_join_candidate(db)
    db.set_saved_dialog_membership(dialog_id, 1, "left")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def create_campaign():
        local = Database(path, bootstrap=False)
        barrier.wait()
        try:
            local.create_join_campaign(1, max_per_hour=40)
        except DatabaseError:
            outcomes.append("blocked")
        else:
            outcomes.append("created")
        finally:
            local.close_thread_connection()

    threads = [threading.Thread(target=create_campaign) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["blocked", "created"]
    with db.get_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM join_campaigns WHERE status IN ('running','paused','network_wait')"
        ).fetchone()[0]
    assert active == 1


def test_reused_username_does_not_move_peer_identity_or_membership(tmp_path):
    db = Database(tmp_path / "username-reuse.db")
    first_id = _make_join_candidate(db, peer_id=101, username="shared", account=1)
    second_id = _make_join_candidate(db, peer_id=202, username="shared", account=2)

    assert first_id != second_id
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id,peer_id,username FROM saved_dialogs ORDER BY id"
        ).fetchall()
        memberships = conn.execute(
            "SELECT saved_dialog_id,account_id,status FROM saved_dialog_memberships ORDER BY id"
        ).fetchall()
    assert [(row["peer_id"], row["username"]) for row in rows] == [
        (101, None),
        (202, "shared"),
    ]
    assert [(row["saved_dialog_id"], row["account_id"]) for row in memberships] == [
        (first_id, 1),
        (second_id, 2),
    ]


def test_join_final_state_is_validated_and_campaign_completes(tmp_path):
    db = Database(tmp_path / "join-final.db")
    dialog_id = _make_join_candidate(db)
    db.set_saved_dialog_membership(dialog_id, 1, "left")
    campaign = db.create_join_campaign(1)
    slot = db.get_join_schedule(campaign["id"])[0]
    task_id = db.insert_task(
        "join_saved_slot", {"campaign_id": campaign["id"], "slot_id": slot["id"]}
    )
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE join_schedule SET status='queued',task_id=? WHERE id=?",
            (task_id, slot["id"]),
        )

    with pytest.raises(DatabaseError, match="Invalid final"):
        db.finish_join_slot(slot["id"], status="banana", result="bad", joined=True)

    assert db.finish_join_slot(slot["id"], status="joined", result="ok", joined=True)
    assert db.get_join_campaign(campaign["id"])["status"] == "completed"


def test_retention_keeps_task_referenced_by_open_slot_and_reconcile_repairs_orphan(
    tmp_path,
):
    db = Database(tmp_path / "retention.db")
    db.insert_channel({"channel_id": 1, "linked_chat_id": 2, "title": "A"})
    campaign = db.create_comment_campaign(
        ["hello"], daily_limit=1, slot_count=1, continuous=False
    )
    slot = db.get_comment_schedule(campaign["id"], limit=1)[0]
    task_id = db.insert_task(
        "auto_comment_slot", {"campaign_id": campaign["id"], "slot_id": slot["id"]}
    )
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET status='completed',updated_at='2000-01-01 00:00:00' WHERE id=?",
            (task_id,),
        )
        conn.execute(
            "UPDATE comment_schedule SET status='queued',task_id=? WHERE id=?",
            (task_id, slot["id"]),
        )

    db.prune_old_data(task_days=1)
    assert db.get_task(task_id) is not None

    with db.get_connection() as conn:
        conn.execute("DROP TRIGGER null_task_references")
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.execute(
            "UPDATE comment_schedule SET task_id=NULL WHERE id=?", (slot["id"],)
        )
    assert db.reconcile_comment_schedule() == 1
    repaired = db.get_comment_schedule(campaign["id"], limit=1)[0]
    assert repaired["status"] == "pending"
    assert repaired["task_id"] is None


def test_corrupt_oldest_task_does_not_hide_next_valid_task(tmp_path):
    db = Database(tmp_path / "corrupt-task.db")
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO tasks(type,payload,status,created_at,updated_at) VALUES('noop','{bad','pending','2000-01-01','2000-01-01')"
        )
    valid_id = db.insert_task("noop", {})

    claimed = db.claim_next_pending_task()

    assert claimed is not None
    assert claimed["id"] == valid_id
    with db.get_connection() as conn:
        first = conn.execute("SELECT status FROM tasks ORDER BY id LIMIT 1").fetchone()
    assert first["status"] == "failed"


def test_saved_dialog_sync_marks_unseen_memberships_left(tmp_path):
    db = Database(tmp_path / "sync-left.db")
    first = _make_join_candidate(db, peer_id=1, username="one")
    second = _make_join_candidate(db, peer_id=2, username="two")

    changed = db.mark_unseen_saved_dialogs_left(account_id=1, seen_dialog_ids=[second])

    assert changed == 1
    rows = {row["id"]: row for row in db.get_saved_dialogs(1)}
    assert rows[first]["membership_status"] == "left"
    assert rows[second]["membership_status"] == "member"


def test_join_guard_is_scoped_to_account(tmp_path):
    db = Database(tmp_path / "account-guard.db")
    now = utc_now()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO join_events(linked_chat_id,joined_at,result,account_id) VALUES(1,?,'joined',1)",
            (now.strftime("%Y-%m-%d %H:%M:%S"),),
        )
    blocked = db.get_join_guard(
        max_joins=1,
        min_interval_seconds=3600,
        now=now,
        window_seconds=3600,
        account_id=1,
    )
    free = db.get_join_guard(
        max_joins=1,
        min_interval_seconds=3600,
        now=now,
        window_seconds=3600,
        account_id=2,
    )
    assert blocked["allowed"] is False
    assert free["allowed"] is True


def test_join_schedule_respects_configured_hourly_limit():
    moments = generate_join_slots(utc_now(), 3, max_per_hour=10)
    gaps = [(right - left).total_seconds() for left, right in zip(moments, moments[1:])]
    assert all(gap >= 360 for gap in gaps)


@pytest.mark.asyncio
async def test_direct_message_service_never_calls_telegram(tmp_path):
    db = Database(tmp_path / "dm-disabled.db")
    task_id = db.insert_task("direct_message", {"chat_id": 100, "text": "hello"})

    class Telegram:
        def __init__(self):
            self.calls = 0

        async def send_message(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(id=321)

    telegram = Telegram()
    service = CommentService(telegram, db=db)
    with pytest.raises(NonRetryableTelegramError) as caught:
        await service.send_direct_message(100, "hello", task_id=task_id)
    assert caught.value.code == "direct_group_disabled"
    assert telegram.calls == 0
    assert db.get_direct_message_delivery(task_id) is None


@pytest.mark.asyncio
async def test_join_scope_cancel_before_external_call_defers_slot():
    context = {
        "campaign_status": "running",
        "status": "queued",
        "title": "A",
        "account_id": 1,
        "saved_dialog_id": 2,
        "peer_id": 3,
        "username": "a",
        "invite_link": None,
        "max_per_hour": 40,
    }
    db = MagicMock()
    db.get_join_slot_context.return_value = context
    db.get_join_campaign.return_value = {"status": "paused"}
    db.mark_join_slot_running.return_value = True

    class Worker:
        def is_scope_cancelled(self, *args):
            return True

    telegram = MagicMock()
    handler = create_join_slot_handler(
        as_int=lambda value, default=0: int(value or default),
        queue_worker=Worker(),
        config=SimpleNamespace(min_join_interval_seconds=45),
        worker_db=db,
        telegram=telegram,
        set_runtime=lambda *_args, **_kwargs: None,
    )

    await handler({"id": 1, "payload": {"campaign_id": 1, "slot_id": 2}})

    db.defer_join_slot.assert_called_once()
    db.finish_join_slot.assert_not_called()
    telegram.join_saved_dialog.assert_not_called()


@pytest.mark.asyncio
async def test_internal_join_error_pauses_campaign_and_propagates():
    context = {
        "campaign_status": "running",
        "status": "queued",
        "title": "A",
        "account_id": 1,
        "saved_dialog_id": 2,
        "peer_id": 3,
        "username": "a",
        "invite_link": None,
        "max_per_hour": 40,
    }
    db = MagicMock()
    db.get_join_slot_context.side_effect = [context, {**context, "status": "running"}]
    db.get_join_campaign.return_value = {"status": "running"}
    db.mark_join_slot_running.return_value = True
    db.get_join_guard.return_value = {"allowed": True, "wait_seconds": 0}

    class Telegram:
        async def join_saved_dialog(self, **_kwargs):
            raise RuntimeError("broken adapter")

    handler = create_join_slot_handler(
        as_int=lambda value, default=0: int(value or default),
        queue_worker=SimpleNamespace(),
        config=SimpleNamespace(min_join_interval_seconds=45),
        worker_db=db,
        telegram=Telegram(),
        set_runtime=lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="broken adapter"):
        await handler({"id": 1, "payload": {"campaign_id": 1, "slot_id": 2}})

    db.pause_join_campaign.assert_called_once()
    assert db.finish_join_slot.call_args.kwargs["status"] == "failed"


def test_local_secret_store_strict_read_reports_corruption(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text("not-json", encoding="utf-8")
    store = SecretStore(path)

    with pytest.raises(RuntimeError, match="corrupted"):
        store.get_strict_optional("api")


def test_local_secret_store_delete_updates_persisted_file(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(path)
    store.set("api", "secret")

    store.delete("api")

    assert store.get_strict_optional("api") is None


def test_daily_maintenance_claim_allows_one_runner(tmp_path):
    path = tmp_path / "maintenance.db"
    Database(path).close_thread_connection()
    barrier = threading.Barrier(2)
    calls = []
    errors = []
    lock = threading.Lock()

    class CountingDatabase(Database):
        def prune_old_data(self, **kwargs):
            with lock:
                calls.append(1)
            return {"ok": 1}

    results = []

    def run():
        db = None
        try:
            db = CountingDatabase(path, bootstrap=False)
            barrier.wait(timeout=5)
            results.append(db.run_daily_maintenance())
        except BaseException as exc:
            errors.append(exc)
            try:
                barrier.abort()
            except threading.BrokenBarrierError:
                pass
        finally:
            if db is not None:
                db.close_thread_connection()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(calls) == 1
    assert sum(result is not None for result in results) == 1

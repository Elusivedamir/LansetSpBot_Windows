from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.campaign_schedule import to_db_time, utc_now
from core.exceptions import NonRetryableTelegramError
from services.comment_service import CommentService
from services.multiaccount_scheduler import AccountCampaignDatabaseView
from storage.database import Database


ACCOUNT_ID = 77
CHANNEL_ID = -1001001
DISCUSSION_ID = -1002002
POST_ID = 55
REPLY_TO = 77
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT_SECONDS = 20.0


def _new_profile(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "process-death.db"
    key_dir = tmp_path / "keys"
    db = Database(db_path, key_storage_dir=key_dir)
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
    db.close_thread_connection()
    return db_path, key_dir


def _reopen(db_path: Path, key_dir: Path) -> Database:
    """Mirror the recovery order used by main.py after Database construction."""
    db = Database(db_path, key_storage_dir=key_dir)
    db.reset_running_tasks()
    db.reconcile_comment_schedule()
    return db


def _child_env(
    db_path: Path,
    key_dir: Path,
    **values: object,
) -> dict[str, str]:
    env = os.environ.copy()
    env["CRASH_DB"] = str(db_path)
    env["CRASH_KEYS"] = str(key_dir)
    for key, value in values.items():
        env[key] = str(value)
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(PROJECT_ROOT)
        if not inherited
        else str(PROJECT_ROOT) + os.pathsep + inherited
    )
    return env


def _run_crash_child(
    db_path: Path,
    key_dir: Path,
    body: str,
    *,
    exit_code: int,
    **env_values: object,
) -> subprocess.CompletedProcess[str]:
    preamble = textwrap.dedent(
        """
        import os
        from pathlib import Path

        from storage.database import Database

        db = Database(
            Path(os.environ["CRASH_DB"]),
            bootstrap=False,
            key_storage_dir=Path(os.environ["CRASH_KEYS"]),
        )
        """
    )
    code = preamble + "\n" + textwrap.dedent(body)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        env=_child_env(db_path, key_dir, **env_values),
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == exit_code, (
        f"child exit={result.returncode}, expected={exit_code}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return result


def _delivery_row(db: Database):
    with db.get_connection() as conn:
        return conn.execute(
            """SELECT status, comment_message_id, error, reserved_at, updated_at
               FROM comment_deliveries
               WHERE account_id=? AND campaign_id=0 AND action_type='comment'
                 AND channel_id=? AND post_id=? AND linked_chat_id=?""",
            (ACCOUNT_ID, CHANNEL_ID, POST_ID, DISCUSSION_ID),
        ).fetchone()


def _comment_count(db: Database) -> int:
    with db.get_connection() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total
               FROM comments
               WHERE account_id=? AND channel_id=? AND post_message_id=?""",
            (ACCOUNT_ID, CHANNEL_ID, POST_ID),
        ).fetchone()
    return int(row["total"] or 0)


def _comment_kwargs():
    return {
        "channel_id": CHANNEL_ID,
        "post_message_id": POST_ID,
        "text": "hello",
        "linked_chat_id": DISCUSSION_ID,
        "reply_to": REPLY_TO,
        "account_id": ACCOUNT_ID,
    }


class _CommentTelegram:
    def __init__(self, *, message_id: int = 9901) -> None:
        self.calls = 0
        self.message_id = int(message_id)

    async def send_comment(self, *_args, **_kwargs):
        self.calls += 1
        return SimpleNamespace(
            id=self.message_id,
            sender_id=ACCOUNT_ID,
            date=None,
        )


class _DirectTelegram:
    def __init__(self, *, message_id: int = 8801) -> None:
        self.calls = 0
        self.message_id = int(message_id)

    async def send_message(self, *_args, **_kwargs):
        self.calls += 1
        return SimpleNamespace(
            id=self.message_id,
            sender_id=ACCOUNT_ID,
            date=None,
        )


def _assert_comment_replay_blocked(db: Database, *, expected_status: str) -> None:
    telegram = _CommentTelegram()
    service = CommentService(telegram, db=db)
    with pytest.raises(NonRetryableTelegramError) as raised:
        asyncio.run(service.ensure_and_send_comment(**_comment_kwargs()))
    assert raised.value.code == "comment_already_reserved"
    assert telegram.calls == 0
    row = _delivery_row(db)
    assert row is not None
    assert str(row["status"]) == expected_status


def test_process_death_after_external_effect_before_receipt_blocks_immediate_replay(
    tmp_path,
):
    """A crash after a possible Telegram side effect must never cause auto replay."""
    db_path, key_dir = _new_profile(tmp_path)
    marker = tmp_path / "telegram-side-effect.txt"

    _run_crash_child(
        db_path,
        key_dir,
        """
        assert db.reserve_comment_delivery(
            -1001001,
            55,
            linked_chat_id=-1002002,
            text="hello",
            account_id=77,
        )
        Path(os.environ["CRASH_MARKER"]).write_text(
            "telegram accepted one mutation",
            encoding="utf-8",
        )
        os._exit(71)
        """,
        exit_code=71,
        CRASH_MARKER=marker,
    )

    assert marker.read_text(encoding="utf-8") == "telegram accepted one mutation"

    db = _reopen(db_path, key_dir)
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "sending"
    assert row["comment_message_id"] is None
    assert _comment_count(db) == 0
    _assert_comment_replay_blocked(db, expected_status="sending")


def test_process_death_inside_uncommitted_receipt_transaction_rolls_back_to_guard(
    tmp_path,
):
    """Partial local finalization must roll back, preserving duplicate protection."""
    db_path, key_dir = _new_profile(tmp_path)
    db = Database(db_path, bootstrap=False, key_storage_dir=key_dir)
    assert db.reserve_comment_delivery(
        CHANNEL_ID,
        POST_ID,
        linked_chat_id=DISCUSSION_ID,
        text="hello",
        account_id=ACCOUNT_ID,
    )
    db.close_thread_connection()

    marker = tmp_path / "receipt-transaction-entered.txt"
    _run_crash_child(
        db_path,
        key_dir,
        """
        with db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE comment_deliveries "
                "SET status='sent', comment_message_id=9902, "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE account_id=77 AND campaign_id=0 "
                "AND action_type='comment' "
                "AND channel_id=-1001001 AND post_id=55 "
                "AND linked_chat_id=-1002002 "
                "AND status='sending'"
            )
            assert cursor.rowcount == 1
            Path(os.environ["CRASH_MARKER"]).write_text(
                "receipt transaction was in-flight",
                encoding="utf-8",
            )
            os._exit(72)
        """,
        exit_code=72,
        CRASH_MARKER=marker,
    )

    assert marker.exists()

    db = _reopen(db_path, key_dir)
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "sending"
    assert row["comment_message_id"] is None
    assert _comment_count(db) == 0
    _assert_comment_replay_blocked(db, expected_status="sending")


def test_process_death_after_committed_receipt_preserves_sent_state(tmp_path):
    """A committed receipt must survive an immediate hard process exit."""
    db_path, key_dir = _new_profile(tmp_path)
    marker = tmp_path / "receipt-committed.txt"

    _run_crash_child(
        db_path,
        key_dir,
        """
        assert db.reserve_comment_delivery(
            -1001001,
            55,
            linked_chat_id=-1002002,
            text="hello",
            account_id=77,
        )
        assert db.finalize_comment_delivery(
            {
                "channel_id": -1001001,
                "linked_chat_id": -1002002,
                "post_message_id": 55,
                "comment_message_id": 9903,
                "reply_to": 77,
                "author_id": 77,
                "text": "hello",
                "date": None,
                "account_id": 77,
                "campaign_id": 0,
                "action_type": "comment",
            }
        )
        Path(os.environ["CRASH_MARKER"]).write_text(
            "receipt committed",
            encoding="utf-8",
        )
        os._exit(73)
        """,
        exit_code=73,
        CRASH_MARKER=marker,
    )

    assert marker.exists()

    db = _reopen(db_path, key_dir)
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "sent"
    assert int(row["comment_message_id"]) == 9903
    assert _comment_count(db) == 1
    _assert_comment_replay_blocked(db, expected_status="sent")


def test_stale_process_death_reservation_becomes_uncertain_on_restart(tmp_path):
    """Old crash reservations must be promoted to explicit manual-review state."""
    db_path, key_dir = _new_profile(tmp_path)

    _run_crash_child(
        db_path,
        key_dir,
        """
        assert db.reserve_comment_delivery(
            -1001001,
            55,
            linked_chat_id=-1002002,
            text="hello",
            account_id=77,
        )
        with db.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE comment_deliveries "
                "SET reserved_at=datetime('now', '-10 minutes') "
                "WHERE account_id=77 AND campaign_id=0 "
                "AND action_type='comment' "
                "AND channel_id=-1001001 AND post_id=55 "
                "AND linked_chat_id=-1002002 "
                "AND status='sending'"
            )
            assert cursor.rowcount == 1
        os._exit(74)
        """,
        exit_code=74,
    )

    db = _reopen(db_path, key_dir)
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "uncertain"
    assert "manual review required" in str(row["error"] or "").lower()
    _assert_comment_replay_blocked(db, expected_status="uncertain")


def test_process_death_before_reservation_commit_allows_exactly_one_safe_retry(
    tmp_path,
):
    """If the reservation never committed, no Telegram mutation was allowed yet."""
    db_path, key_dir = _new_profile(tmp_path)

    _run_crash_child(
        db_path,
        key_dir,
        """
        with db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "INSERT INTO comment_deliveries("
                "account_id, campaign_id, action_type, channel_id, post_id, "
                "linked_chat_id, text, status, reserved_at, updated_at"
                ") VALUES("
                "77, 0, 'comment', -1001001, 55, "
                "-1002002, 'hello', 'sending', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP"
                ")"
            )
            assert cursor.rowcount == 1
            os._exit(75)
        """,
        exit_code=75,
    )

    db = _reopen(db_path, key_dir)
    assert _delivery_row(db) is None

    telegram = _CommentTelegram(message_id=9905)
    service = CommentService(telegram, db=db)
    result = asyncio.run(service.ensure_and_send_comment(**_comment_kwargs()))

    assert int(result.id) == 9905
    assert telegram.calls == 1
    row = _delivery_row(db)
    assert row is not None
    assert row["status"] == "sent"
    assert int(row["comment_message_id"]) == 9905


def test_direct_message_process_death_reservation_blocks_replay(tmp_path):
    """The ordinary-group SEND ledger must be crash-safe too, not only comments."""
    db_path, key_dir = _new_profile(tmp_path)
    db = Database(db_path, bootstrap=False, key_storage_dir=key_dir)
    task_id = int(
        db.insert_task(
            "direct_message",
            {"account_id": ACCOUNT_ID, "chat_id": -1003003, "text": "hello"},
        )
    )
    db.close_thread_connection()
    marker = tmp_path / "direct-message-side-effect.txt"

    _run_crash_child(
        db_path,
        key_dir,
        """
        task_id = int(os.environ["CRASH_TASK_ID"])
        assert db.reserve_direct_message_delivery(
            task_id,
            -1003003,
            "hello",
            account_id=77,
        )
        Path(os.environ["CRASH_MARKER"]).write_text(
            "direct mutation may have happened",
            encoding="utf-8",
        )
        os._exit(76)
        """,
        exit_code=76,
        CRASH_TASK_ID=task_id,
        CRASH_MARKER=marker,
    )

    assert marker.exists()

    db = _reopen(db_path, key_dir)
    delivery = db.get_direct_message_delivery(task_id)
    assert delivery is not None
    assert delivery["status"] == "sending"

    telegram = _DirectTelegram()
    service = CommentService(telegram, db=db)
    with pytest.raises(NonRetryableTelegramError) as raised:
        asyncio.run(
            service.send_direct_message(
                -1003003,
                "hello",
                task_id=task_id,
                account_id=ACCOUNT_ID,
            )
        )

    assert raised.value.code == "direct_message_duplicate_guard"
    assert telegram.calls == 0
    delivery = db.get_direct_message_delivery(task_id)
    assert delivery is not None
    assert delivery["status"] == "sending"



def test_join_process_death_becomes_uncertain_and_is_not_requeued(tmp_path):
    """A crash after a possible JOIN must pause the target instead of joining twice."""
    db_path, key_dir = _new_profile(tmp_path)
    db = Database(db_path, bootstrap=False, key_storage_dir=key_dir)
    now = utc_now()
    db.register_telegram_account(
        telegram_account_id=ACCOUNT_ID,
        session_name=f"account_{ACCOUNT_ID}",
        display_name="Crash account",
        username="crash_account",
        authorized=True,
    )
    dialog_id = int(
        db.upsert_saved_dialog(
            {
                "peer_id": -1004004,
                "username": "crash_join_target",
                "title": "Crash join target",
                "kind": "channel",
            },
            account_id=ACCOUNT_ID,
        )
    )
    db.set_saved_dialog_membership(dialog_id, ACCOUNT_ID, "left")
    campaign = db.create_join_campaign(
        ACCOUNT_ID,
        max_per_hour=40,
        start_at=now,
    )
    campaign_id = int(campaign["id"])
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE join_schedule SET scheduled_at=? WHERE campaign_id=?",
            (to_db_time(now - timedelta(seconds=1)), campaign_id),
        )
    view = AccountCampaignDatabaseView(db, ACCOUNT_ID)
    queued = view.queue_due_join_slot(now=now)
    assert queued is not None
    task_id = int(queued["task_id"])
    slot_id = int(queued["slot_id"])
    db.close_thread_connection()

    marker = tmp_path / "join-side-effect.txt"
    _run_crash_child(
        db_path,
        key_dir,
        """
        task_id = int(os.environ["CRASH_TASK_ID"])
        assert db.set_processing(task_id)
        Path(os.environ["CRASH_MARKER"]).write_text(
            "join request may have reached Telegram",
            encoding="utf-8",
        )
        os._exit(78)
        """,
        exit_code=78,
        CRASH_TASK_ID=task_id,
        CRASH_MARKER=marker,
    )

    assert marker.exists()

    # main.py first classifies the interrupted mutating task as failed/uncertain.
    # The next join scheduler tick calls reconcile_join_schedule(), which must
    # convert the slot and membership proof to uncertain and pause the campaign.
    db = _reopen(db_path, key_dir)
    repaired = db.reconcile_join_schedule(account_id=ACCOUNT_ID)
    assert repaired >= 1

    with db.get_connection() as conn:
        task = conn.execute(
            "SELECT status, error FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        slot = conn.execute(
            "SELECT status, task_id, result FROM join_schedule WHERE id=?",
            (slot_id,),
        ).fetchone()
        membership = conn.execute(
            """SELECT status, last_error
               FROM saved_dialog_memberships
               WHERE saved_dialog_id=? AND account_id=?""",
            (dialog_id, ACCOUNT_ID),
        ).fetchone()
        campaign_row = conn.execute(
            "SELECT status, pause_reason FROM join_campaigns WHERE id=?",
            (campaign_id,),
        ).fetchone()

    assert task is not None
    assert task["status"] == "failed"
    assert "uncertain external result" in str(task["error"] or "")

    assert slot is not None
    assert slot["status"] == "uncertain"
    assert int(slot["task_id"]) == task_id
    assert "uncertain" in str(slot["result"] or "").lower()

    assert membership is not None
    assert membership["status"] == "uncertain"

    assert campaign_row is not None
    assert campaign_row["status"] == "paused"
    assert "неизвестен" in str(campaign_row["pause_reason"] or "").lower()

    # A paused campaign with an uncertain slot cannot silently create a second
    # join_saved_slot task for the same target.
    assert AccountCampaignDatabaseView(
        db,
        ACCOUNT_ID,
    ).queue_due_join_slot(now=utc_now()) is None

def test_process_death_running_tasks_follow_startup_idempotency_policy(tmp_path):
    """Startup may requeue idempotent work but must fail mutating work closed."""
    db_path, key_dir = _new_profile(tmp_path)
    db = Database(db_path, bootstrap=False, key_storage_dir=key_dir)
    safe_task_id = int(
        db.insert_task("sync_channels", {"account_id": ACCOUNT_ID})
    )
    mutating_task_id = int(
        db.insert_task(
            "comment",
            {
                "account_id": ACCOUNT_ID,
                "channel_id": CHANNEL_ID,
                "post_message_id": POST_ID,
                "linked_chat_id": DISCUSSION_ID,
                "reply_to": REPLY_TO,
                "text": "hello",
            },
        )
    )
    db.close_thread_connection()

    _run_crash_child(
        db_path,
        key_dir,
        """
        safe_id = int(os.environ["CRASH_SAFE_TASK_ID"])
        mutating_id = int(os.environ["CRASH_MUTATING_TASK_ID"])
        assert db.set_processing(safe_id)
        assert db.set_processing(mutating_id)
        os._exit(77)
        """,
        exit_code=77,
        CRASH_SAFE_TASK_ID=safe_task_id,
        CRASH_MUTATING_TASK_ID=mutating_task_id,
    )

    db = Database(db_path, key_storage_dir=key_dir)
    recovered = db.reset_running_tasks()
    assert recovered == 2

    with db.get_connection() as conn:
        safe = conn.execute(
            "SELECT status, error FROM tasks WHERE id=?",
            (safe_task_id,),
        ).fetchone()
        mutating = conn.execute(
            "SELECT status, error FROM tasks WHERE id=?",
            (mutating_task_id,),
        ).fetchone()

    assert safe is not None
    assert safe["status"] == "pending"
    assert "Recovered after unclean shutdown" in str(safe["error"] or "")

    assert mutating is not None
    assert mutating["status"] == "failed"
    assert "uncertain external result" in str(mutating["error"] or "")

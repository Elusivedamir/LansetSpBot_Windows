from __future__ import annotations

import json
import logging
import random
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.campaign_schedule import to_db_time, utc_now
from core.exceptions import NonRetryableTelegramError
from core.factory_reset import FactoryResetError, reset_local_state
from core.paths import AppPaths
from gui.background import BackgroundCall
from storage.database import Database
from workers.comment_slot.handler import create_comment_slot_handler


ACCOUNT_ID = 77


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        database=root / "marlen.db",
        logs=root / "logs",
        sessions=root / "sessions",
        backups=root / "backups",
    )


def _create_campaign(db: Database, text: str):
    return db.create_comment_campaign(
        [text],
        daily_limit=1,
        slot_count=1,
        continuous=False,
        start_at=utc_now() - timedelta(hours=1),
        allow_existing=True,
        account_id=ACCOUNT_ID,
        rng=random.Random(1),
    )


def _bind_running_task(
    db: Database,
    *,
    campaign_id: int,
    slot_id: int,
    payload_campaign_id: int | None = None,
):
    task_id = db.insert_task(
        "auto_comment_slot",
        {
            "campaign_id": int(payload_campaign_id or campaign_id),
            "slot_id": int(slot_id),
            "account_id": ACCOUNT_ID,
        },
        max_retries=0,
    )
    with db.get_connection() as conn:
        conn.execute(
            """UPDATE tasks SET status='running', updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (task_id,),
        )
        conn.execute(
            """UPDATE comment_schedule
               SET status='queued', task_id=?, scheduled_at=?
               WHERE id=?""",
            (task_id, to_db_time(utc_now() - timedelta(seconds=1)), slot_id),
        )
    task = db.get_task(task_id)
    assert task is not None
    if isinstance(task.get("payload"), str):
        task["payload"] = json.loads(task["payload"])
    return task


class _NoTelegramCalls:
    def __getattr__(self, name):
        raise AssertionError(f"Telegram boundary was reached: {name}")


class _NoCommentCalls:
    def __getattr__(self, name):
        raise AssertionError(f"Comment boundary was reached: {name}")


class _CurrentRouteTelegram:
    def __init__(self) -> None:
        self.exact_calls: list[tuple[int, int]] = []
        self.registered: list[tuple[int, object, object]] = []

    def register_peer_reference(self, peer_id, *, access_hash=None, peer_type=None):
        self.registered.append((int(peer_id), access_hash, peer_type))

    async def get_post_for_commenting(self, channel_id, post_id):
        self.exact_calls.append((int(channel_id), int(post_id)))
        return SimpleNamespace(
            status="ok",
            message=SimpleNamespace(id=int(post_id)),
            discussion_chat_id=30,
            discussion_message_id=800,
        )

    async def get_latest_post_for_commenting(self, _channel_id):
        raise AssertionError("A persisted source post must be re-resolved exactly")


class _CommentRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def ensure_and_send_comment(self, **kwargs):
        self.calls.append(dict(kwargs))


class _QueueBoundary:
    def is_scope_cancelled(self, *_scope) -> bool:
        return False


def _handler(db: Database, telegram, comments):
    return create_comment_slot_handler(
        as_int=_as_int,
        queue_worker=_QueueBoundary(),
        config=SimpleNamespace(),
        worker_db=db,
        telegram=telegram,
        comments=comments,
        set_runtime=lambda _task_id, _message, **_kwargs: None,
    )


def test_factory_reset_fails_closed_when_managed_directory_is_swapped(
    tmp_path, monkeypatch
):
    root = tmp_path / "Marlen"
    paths = _paths(root)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "must-survive.txt"
    sentinel.write_text("outside", encoding="utf-8")
    for directory in (root, paths.sessions, paths.logs, paths.backups):
        directory.mkdir(parents=True, exist_ok=True)
    (paths.sessions / "main.session").write_text("session", encoding="utf-8")
    paths.database.write_text("db", encoding="utf-8")

    original_rename = Path.rename
    swapped = False

    def swap_then_rename(self: Path, target: Path):
        nonlocal swapped
        if self == paths.sessions and not swapped:
            swapped = True
            backup = root / ".sessions-race-backup"
            original_rename(self, backup)
            self.symlink_to(external, target_is_directory=True)
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", swap_then_rename)

    with pytest.raises(FactoryResetError, match="заменён"):
        reset_local_state(database_path=paths.database, paths=paths)

    assert swapped is True
    assert sentinel.read_text(encoding="utf-8") == "outside"
    assert not paths.sessions.is_symlink()
    assert (paths.sessions / "main.session").read_text(encoding="utf-8") == "session"


def test_background_call_redacts_callback_and_cleanup_errors_everywhere(caplog, capsys):
    callback_secret = "V23_CALLBACK_SECRET_771"
    cleanup_secret = "V23_CLEANUP_SECRET_882"
    failures: list[str] = []

    def callback():
        try:
            raise ValueError({"password": callback_secret})
        except ValueError as cause:
            raise RuntimeError("callback failed") from cause

    def cleanup():
        raise RuntimeError(f"proxy_password={cleanup_secret}")

    job = BackgroundCall(callback, cleanup=cleanup)
    job.signals.failed.connect(failures.append)
    with caplog.at_level(logging.ERROR, logger="gui.background"):
        job.run()

    captured = capsys.readouterr()
    combined = "\n".join([captured.out, captured.err, caplog.text, *failures])
    assert callback_secret not in combined
    assert cleanup_secret not in combined
    assert "<redacted>" in combined
    assert failures and "callback failed" in failures[0]


@pytest.mark.asyncio
async def test_comment_task_campaign_slot_mismatch_blocks_all_external_actions(
    tmp_path,
):
    db = Database(tmp_path / "context-mismatch.db")
    db.set_setting("telegram.account_id", ACCOUNT_ID)
    db.insert_channel({"channel_id": 10, "linked_chat_id": 20, "title": "Source"})
    campaign_a = _create_campaign(db, "TEXT_FROM_CAMPAIGN_A")
    campaign_b = _create_campaign(db, "TEXT_FROM_CAMPAIGN_B")
    slot_b = db.get_comment_schedule(campaign_b["id"], limit=1)[0]
    task = _bind_running_task(
        db,
        campaign_id=campaign_b["id"],
        slot_id=slot_b["id"],
        payload_campaign_id=campaign_a["id"],
    )
    handler = _handler(db, _NoTelegramCalls(), _NoCommentCalls())

    with pytest.raises(NonRetryableTelegramError) as error:
        await handler(task)

    assert error.value.code == "comment_context_mismatch"
    stored_a = db.get_comment_campaign(campaign_a["id"])
    stored_b = db.get_comment_campaign(campaign_b["id"])
    stored_slot = db.get_comment_schedule(campaign_b["id"], limit=1)[0]
    assert stored_a["attempted_count"] == stored_a["sent_count"] == 0
    assert stored_b["attempted_count"] == stored_b["sent_count"] == 0
    assert stored_slot["status"] == "queued"
    assert not str(stored_slot.get("selected_text") or "")
    assert (
        db.get_comment_history(campaign_id=campaign_a["id"], account_id=ACCOUNT_ID)
        == []
    )
    assert (
        db.get_comment_history(campaign_id=campaign_b["id"], account_id=ACCOUNT_ID)
        == []
    )


@pytest.mark.asyncio
async def test_persisted_comment_route_is_re_resolved_before_send(tmp_path):
    db = Database(tmp_path / "stale-route.db")
    db.set_setting("telegram.account_id", ACCOUNT_ID)
    db.insert_channel({"channel_id": 10, "linked_chat_id": 20, "title": "Source"})
    campaign = _create_campaign(db, "audit")
    slot = db.get_comment_schedule(campaign["id"], limit=1)[0]
    task = _bind_running_task(
        db,
        campaign_id=campaign["id"],
        slot_id=slot["id"],
    )
    with db.get_connection() as conn:
        conn.execute(
            """UPDATE comment_schedule
               SET channel_id=10, post_id=901, linked_chat_id=20,
                   discussion_message_id=700, route_cached_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (slot["id"],),
        )

    telegram = _CurrentRouteTelegram()
    comments = _CommentRecorder()
    handler = _handler(db, telegram, comments)
    await handler(task)

    assert telegram.exact_calls == [(10, 901)]
    assert len(comments.calls) == 1
    sent = comments.calls[0]
    assert sent["linked_chat_id"] == 30
    assert sent["post_message_id"] == 901
    assert sent["reply_to"] == 800
    assert sent["linked_chat_id"] != 20
    route = db.get_comment_slot_route(slot["id"], task["id"])
    assert route["linked_chat_id"] == 30
    assert route["discussion_message_id"] == 800
    stored_campaign = db.get_comment_campaign(campaign["id"])
    assert stored_campaign["attempted_count"] == 1
    assert stored_campaign["sent_count"] == 1

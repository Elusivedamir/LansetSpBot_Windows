from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import core.composition as composition
from core.composition import ApplicationContainer
from core.config import TelegramSettings
from core.exceptions import NonRetryableTelegramError
from services.comment_service import CommentService
from services.telegram_service import TelegramService
from storage.database import Database
from tests.conftest import open_project_database, project_row_factory
from storage.migrations.local_channel_ban_v27 import migrate_local_channel_ban_v27


class _Worker:
    def __init__(self, database) -> None:
        self.database = database
        self.sleep_calls: list[float] = []

    def get_db(self):
        return self.database

    def isInterruptionRequested(self) -> bool:  # noqa: N802 - Qt API
        return False

    async def safe_sleep(self, seconds: float, *, cancel_scope=None) -> bool:
        self.sleep_calls.append(seconds)
        return True


class _TelegramForLinks:
    def __init__(self) -> None:
        self.join_calls: list[int] = []

    async def join_without_confirmation(self, chat_id: int) -> bool:
        self.join_calls.append(int(chat_id))
        if len(self.join_calls) == 1:
            raise NonRetryableTelegramError(
                "Telegram delivery result is unknown after a network failure",
                code="join_result_unknown",
            )
        return True

    async def disconnect(self) -> None:
        return None

    @staticmethod
    def is_channel_peer(value) -> bool:
        return value is not None

    def register_peer_reference(self, *_args, **_kwargs) -> None:
        return None


class _LinkedForLinks:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def get_linked_chat_id(self, channel_id: int):
        self.calls.append(int(channel_id))
        return {1: 101, 2: None}.get(int(channel_id))


class _CommentsStub:
    pass


def _link_handlers(
    monkeypatch,
    database: MagicMock,
    telegram: _TelegramForLinks,
    *,
    linked_service: _LinkedForLinks | None = None,
):
    container = object.__new__(ApplicationContainer)
    container.config = SimpleNamespace(
        rate_limit=0.01,
        max_joins_per_hour=40,
        min_join_interval_seconds=45,
        post_join_delay_min_seconds=1,
        post_join_delay_max_seconds=1,
        link_join_delay_min_seconds=0,
        link_join_delay_max_seconds=0,
        link_check_delay_min_seconds=0,
        link_check_delay_max_seconds=0,
    )
    container.queue_worker = _Worker(database)
    container.secret_store = MagicMock()
    container._telegram_settings = lambda _db: TelegramSettings(
        api_id=1,
        api_hash="hash",
        session_dir=Path("/tmp"),
    )
    monkeypatch.setattr(composition, "TelegramService", lambda *_a, **_k: telegram)
    linked_service = linked_service or _LinkedForLinks()
    monkeypatch.setattr(
        composition, "LinkedChatService", lambda _telegram: linked_service
    )
    monkeypatch.setattr(
        composition, "CommentService", lambda *_a, **_k: _CommentsStub()
    )
    handlers, cleanup = container._create_worker_handlers()
    return handlers, cleanup


def test_v26_migration_converts_ambiguous_join_to_persistent_local_ban(tmp_path):
    path = tmp_path / "v26.db"
    # Production always runs migrations against an already-encrypted database,
    # because prepare_encrypted_database() executes before the migration chain.
    with open_project_database(path) as conn:
        conn.executescript(
            """
            CREATE TABLE channels(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                target_kind TEXT NOT NULL DEFAULT 'channel',
                linked_chat_id INTEGER,
                linked_chat_title TEXT,
                link_status TEXT,
                link_checked_at DATETIME,
                last_sync_at DATETIME
            );
            INSERT INTO channels(
                account_id, channel_id, linked_chat_id, linked_chat_title, link_status
            ) VALUES(
                77, 10, 20, 'Discussion',
                'Недоступно: Telegram delivery result is unknown after a network failure'
            );
            PRAGMA user_version=26;
            """
        )

    migrate_local_channel_ban_v27(
        path,
        sqlite_timeout_seconds=5.0,
        busy_timeout_ms=5_000,
    )

    with open_project_database(path) as conn:
        conn.row_factory = project_row_factory()
        row = conn.execute("SELECT * FROM channels WHERE channel_id=10").fetchone()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 27
        assert row["linked_chat_id"] is None
        assert row["linked_chat_title"] is None
        assert row["link_checked_at"] is not None
        assert row["local_banned_at"] is not None
        assert row["local_ban_reason"] == "Результат вступления неизвестен"
        assert row["link_status"] == "Заблокирован · результат вступления неизвестен"


def test_local_ban_survives_resync_and_excludes_commenting(tmp_path):
    db = Database(tmp_path / "local-ban.db")
    db.upsert_channels_batch(
        [
            {
                "channel_id": 100,
                "title": "Source",
                "target_kind": "channel",
                "comment_mode": "channel_post",
                "linked_chat_id": 200,
                "linked_chat_title": "Discussion",
                "link_status": "Связано",
            }
        ],
        account_id=7,
    )
    assert db.count_channels_for_commenting(account_id=7) == 1

    campaign = db.create_comment_campaign(
        ["hello"], daily_limit=2, slot_count=2, account_id=7
    )
    slots = db.get_comment_schedule(campaign["id"])
    task_id = db.insert_task(
        "auto_comment_slot",
        {"account_id": 7, "campaign_id": campaign["id"], "slot_id": slots[1]["id"]},
    )
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE comment_schedule SET channel_id=? WHERE id=?",
            (100, slots[0]["id"]),
        )
        conn.execute(
            """UPDATE comment_schedule
               SET channel_id=?, status='queued', task_id=? WHERE id=?""",
            (100, task_id, slots[1]["id"]),
        )

    assert db.ban_channel_locally(
        100,
        "Результат вступления неизвестен",
        related_peer_id=200,
        account_id=7,
    )
    banned = db.get_channel_by_id(100, account_id=7)
    assert banned["linked_chat_id"] is None
    assert banned["linked_chat_title"] is None
    assert banned["local_ban_peer_id"] == 200
    assert banned["local_banned_at"] is not None
    assert db.count_channels_for_commenting(account_id=7) == 0
    assert db.get_channels_for_commenting(10, account_id=7) == []
    assert db.count_unchecked_link_targets(account_id=7) == 0
    cancelled_slots = db.get_comment_schedule(campaign["id"])
    assert [row["status"] for row in cancelled_slots] == ["cancelled", "cancelled"]
    assert db.get_task(task_id)["status"] == "cancelled"

    db.upsert_channels_batch(
        [
            {
                "channel_id": 100,
                "title": "Source after resync",
                "target_kind": "channel",
                "comment_mode": "channel_post",
                "linked_chat_id": 300,
                "linked_chat_title": "New discussion",
                "link_status": "Связано повторно",
            }
        ],
        account_id=7,
    )
    after_sync = db.get_channel_by_id(100, account_id=7)
    assert after_sync["title"] == "Source after resync"
    assert after_sync["linked_chat_id"] is None
    assert after_sync["local_banned_at"] == banned["local_banned_at"]
    assert (
        db.update_channel_link(100, 300, "New discussion", "Связано", account_id=7)
        is False
    )


@pytest.mark.asyncio
async def test_ambiguous_join_bans_only_target_and_batch_continues(monkeypatch):
    db = MagicMock()
    db.get_setting.return_value = 77
    db.get_channels.return_value = [
        {
            "channel_id": 1,
            "title": "Ambiguous",
            "target_kind": "channel",
            "link_checked_at": None,
            "local_banned_at": None,
        },
        {
            "channel_id": 2,
            "title": "No discussion",
            "target_kind": "channel",
            "link_checked_at": None,
            "local_banned_at": None,
        },
    ]
    db.update_task_checkpoint.return_value = True
    db.ban_channel_locally.return_value = True
    telegram = _TelegramForLinks()
    handlers, cleanup = _link_handlers(monkeypatch, db, telegram)

    await handlers["link_channels"]({"id": 9, "payload": {}})
    await cleanup()

    db.ban_channel_locally.assert_called_once_with(
        1,
        "Результат вступления неизвестен",
        related_peer_id=101,
        account_id=77,
    )
    # The second channel was still inspected and persisted after the failure.
    assert any(
        call.args == (2, None, None, "Нет чата обсуждения")
        for call in db.update_channel_link.call_args_list
    )
    assert telegram.join_calls == [101]
    db.update_task_progress.assert_called_with(9, 100)
    final_payload = db.update_task_checkpoint.call_args_list[-1].args[1]
    assert "_link_checkpoint" not in final_payload


@pytest.mark.asyncio
async def test_resumed_checkpoint_skips_already_banned_channel_without_rpc(
    monkeypatch,
):
    db = MagicMock()
    db.get_setting.return_value = 77
    banned_row = {
        "channel_id": 1,
        "title": "Already banned",
        "target_kind": "channel",
        "link_checked_at": "2026-07-20 12:00:00",
        "local_banned_at": "2026-07-20 12:00:00",
        "local_ban_reason": "Результат вступления неизвестен",
        "linked_chat_id": None,
    }
    db.get_channels.return_value = [banned_row]
    db.get_channel_by_id.return_value = dict(banned_row)
    db.update_task_checkpoint.return_value = True
    telegram = _TelegramForLinks()
    linked_service = _LinkedForLinks()
    handlers, cleanup = _link_handlers(
        monkeypatch, db, telegram, linked_service=linked_service
    )
    checkpoint = {
        "version": 1,
        "account_id": 77,
        "phase": "channels",
        "channel_ids": [1],
        "group_ids": [],
        "channel_index": 0,
        "group_index": 0,
        "join_attempt_count": 0,
        "joined_count": 0,
        "prepared_count": 0,
        "banned_count": 0,
    }

    await handlers["link_channels"](
        {
            "id": 10,
            "defer_count": 1,
            "payload": {"_link_checkpoint": checkpoint},
        }
    )
    await cleanup()

    assert linked_service.calls == []
    assert telegram.join_calls == []
    db.ban_channel_locally.assert_not_called()
    assert not any(
        call.args and call.args[0] == 1
        for call in db.update_channel_link.call_args_list
    )
    db.update_task_progress.assert_called_with(10, 100)
    final_payload = db.update_task_checkpoint.call_args_list[-1].args[1]
    assert "_link_checkpoint" not in final_payload


class _CommentDB:
    def __init__(self, *, banned: bool = False) -> None:
        self.banned = banned
        self.reserved = 0
        self.finalized: list[dict] = []

    def is_channel_locally_banned(self, _channel_id, *, account_id=None) -> bool:
        return self.banned

    def reserve_comment_delivery(self, *_args, **_kwargs) -> bool:
        self.reserved += 1
        return True

    def finalize_comment_delivery(self, data: dict) -> None:
        self.finalized.append(dict(data))


class _CommentTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def send_comment(self, channel_id, post_id, text, **kwargs):
        self.calls.append((channel_id, post_id, text, kwargs))
        return SimpleNamespace(id=444, sender_id=55, date=None)


@pytest.mark.asyncio
async def test_successful_comment_is_written_to_file_log_without_text(caplog):
    db = _CommentDB()
    telegram = _CommentTelegram()
    service = CommentService(telegram, db=db)
    secret_text = "SENSITIVE_COMMENT_TEXT_V27"

    with caplog.at_level(logging.INFO, logger="services.comment_service"):
        result = await service.ensure_and_send_comment(
            channel_id=11,
            linked_chat_id=22,
            post_message_id=33,
            text=secret_text,
            account_id=77,
            campaign_id=88,
            membership_ready=True,
        )

    assert result.id == 444
    assert db.reserved == 1
    assert db.finalized[0]["comment_message_id"] == 444
    assert "Comment sent successfully" in caplog.text
    assert "account_id=77" in caplog.text
    assert "channel_id=11" in caplog.text
    assert "comment_message_id=444" in caplog.text
    assert secret_text not in caplog.text


@pytest.mark.asyncio
async def test_local_ban_blocks_comment_before_reservation_and_telegram_rpc():
    db = _CommentDB(banned=True)
    telegram = _CommentTelegram()
    service = CommentService(telegram, db=db)

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.ensure_and_send_comment(
            channel_id=11,
            linked_chat_id=22,
            post_message_id=33,
            text="hello",
            account_id=77,
            membership_ready=True,
        )

    assert raised.value.code == "channel_locally_banned"
    assert db.reserved == 0
    assert telegram.calls == []


@pytest.mark.asyncio
async def test_interrupt_race_retrieves_finished_future_exception():
    service = object.__new__(TelegramService)
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    calls = 0

    def interruption_requested() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            future.set_exception(RuntimeError("connect failed in shutdown race"))
            return True
        return False

    service._interruption_requested = interruption_requested
    with pytest.raises(asyncio.CancelledError):
        await service._await_interruptible(future, timeout=1.0)

    assert future.done()
    # CPython sets this flag to False once result()/exception() retrieved it.
    assert getattr(future, "_log_traceback", False) is False


def test_telegram_client_disables_background_update_difference_loop(
    tmp_path, monkeypatch
):
    captured: dict = {}

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr("services.telegram_service.PacedTelegramClient", Client)
    monkeypatch.setattr(
        TelegramService, "_prepare_session_file", lambda self, _path: None
    )
    monkeypatch.setattr(
        TelegramService, "_secure_session_file", lambda self, _path: None
    )

    service = TelegramService(
        TelegramSettings(api_id=1, api_hash="hash", session_dir=tmp_path),
        limiter=object(),
    )

    assert service.client is not None
    assert captured["kwargs"]["receive_updates"] is False
    assert captured["kwargs"]["request_retries"] == 0

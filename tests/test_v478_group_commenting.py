from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import utils
from telethon.tl import types

from services.telegram_service import TelegramService
from storage.database import Database
from tests.test_composition_resilience import (
    _Linked,
    _Telegram,
    _comment_database,
    _handlers,
)


class _AsyncRows:
    def __init__(self, rows):
        self._rows = iter(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._rows)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


@pytest.mark.asyncio
async def test_iter_channels_yields_broadcasts_and_groups_with_safe_modes():
    broadcast = types.Channel(
        id=111,
        title="News",
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=True,
        megagroup=False,
        access_hash=1,
        username="news",
    )
    supergroup = types.Channel(
        id=222,
        title="Supergroup",
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=False,
        megagroup=True,
        access_hash=2,
        username="supergroup",
    )
    basic_group = types.Chat(
        id=333,
        title="Basic group",
        photo=types.ChatPhotoEmpty(),
        participants_count=3,
        date=None,
        version=1,
    )
    private_user = SimpleNamespace(id=444, first_name="User")
    dialogs = [
        SimpleNamespace(entity=broadcast, is_channel=True, is_group=False),
        SimpleNamespace(entity=supergroup, is_channel=True, is_group=True),
        SimpleNamespace(entity=basic_group, is_channel=False, is_group=True),
        SimpleNamespace(entity=private_user, is_channel=False, is_group=False),
    ]

    service = object.__new__(TelegramService)
    service.ensure_connected = AsyncMock()
    service.client = SimpleNamespace(iter_dialogs=lambda: _AsyncRows(dialogs))
    service._iter_with_timeout = lambda iterator: iterator

    rows = [row async for row in service.iter_channels()]

    assert rows[0] == {
        "id": 111,
        "title": "News",
        "username": "news",
        "target_kind": "channel",
        "comment_mode": "channel_post",
        "linked_chat_id": None,
        "linked_chat_title": None,
        "link_status": None,
    }
    assert rows[1]["id"] == utils.get_peer_id(supergroup)
    assert rows[1]["target_kind"] == "group"
    assert rows[1]["comment_mode"] == "pending"
    assert rows[1]["linked_chat_id"] == rows[1]["id"]
    assert rows[2]["id"] == utils.get_peer_id(basic_group)
    assert rows[2]["target_kind"] == "group"
    assert len(rows) == 3


def test_group_classification_keeps_only_linked_discussions_as_comment_targets(
    tmp_path,
):
    db = Database(tmp_path / "groups.db")
    linked_group_id = -1000000000020
    direct_group_id = -1000000000030
    db.upsert_channels_batch(
        [
            {
                "channel_id": 10,
                "title": "Broadcast",
                "target_kind": "channel",
                "comment_mode": "channel_post",
            },
            {
                "channel_id": linked_group_id,
                "title": "Discussion",
                "target_kind": "group",
                "comment_mode": "pending",
                "linked_chat_id": linked_group_id,
            },
            {
                "channel_id": direct_group_id,
                "title": "Standalone",
                "target_kind": "group",
                "comment_mode": "pending",
                "linked_chat_id": direct_group_id,
            },
        ]
    )
    db.update_channel_link(10, linked_group_id, "Discussion", "Связано")
    db.update_group_link_classification(
        direct_group_id,
        is_linked=False,
        status="Группа · связь не обнаружена",
    )

    result = db.refresh_group_comment_modes()
    rows = {row["channel_id"]: row for row in db.get_channels()}
    targets = {row["channel_id"] for row in db.get_channels_for_commenting(10)}

    assert result == {"linked_discussion": 1, "direct_group": 0}
    assert rows[linked_group_id]["comment_mode"] == "linked_discussion"
    assert rows[direct_group_id]["comment_mode"] == "pending"
    assert targets == {10}

    # Repeated Telegram synchronization must keep an ordinary group disabled.
    db.upsert_channels_batch(
        [
            {
                "channel_id": direct_group_id,
                "title": "Standalone renamed",
                "target_kind": "group",
                "comment_mode": "pending",
                "linked_chat_id": direct_group_id,
                "link_status": "Группа · ожидает проверки связей",
            }
        ]
    )
    assert db.get_channel_by_id(direct_group_id)["comment_mode"] == "pending"


def test_v15_database_migrates_work_targets_to_v16(tmp_path):
    path = tmp_path / "v15.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE migrations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL UNIQUE,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE channels(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL UNIQUE,
                username TEXT,
                title TEXT,
                linked_chat_id INTEGER,
                linked_chat_title TEXT,
                link_status TEXT,
                last_sync_at DATETIME,
                last_comment_check_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE comment_deliveries(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL DEFAULT 0,
                status TEXT,
                reserved_at DATETIME,
                updated_at DATETIME,
                error TEXT
            );
            CREATE TABLE direct_message_deliveries(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                status TEXT,
                reserved_at DATETIME,
                updated_at DATETIME,
                error TEXT
            );
            CREATE TABLE tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                payload TEXT,
                status TEXT DEFAULT 'pending',
                progress INTEGER DEFAULT 0,
                status_text TEXT,
                error TEXT,
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                not_before DATETIME
            );
            INSERT INTO channels(channel_id, title, linked_chat_id)
            VALUES(10, 'Legacy channel', -1000000000020);
            INSERT INTO migrations(version) VALUES(15);
            PRAGMA user_version=15;
            """
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    row = db.get_channel_by_id(10)

    # The number moves with every migration; what these tests exist to pin
    # is that a migrated database reaches the version the code expects.
    assert db.get_version() == Database.SCHEMA_VERSION
    assert Database.SCHEMA_VERSION >= 30
    assert row["target_kind"] == "channel"
    assert row["comment_mode"] == "channel_post"


@pytest.mark.asyncio
async def test_comment_slot_never_sends_plain_message_to_standalone_group(monkeypatch):
    db = _comment_database()
    group_id = -1000000000030
    db.get_channels_for_commenting.return_value = [
        {
            "channel_id": group_id,
            "linked_chat_id": group_id,
            "title": "Open group",
            "target_kind": "group",
            "comment_mode": "direct_group",
        }
    ]
    telegram = _Telegram()
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 700, "payload": {"campaign_id": 1, "slot_id": 700}}
    )

    assert telegram.latest_calls == 0
    assert telegram.member_calls == []
    assert telegram.join_calls == []
    assert comments.sent == []
    assert db.finish_comment_slot.call_args.kwargs["status"] == "skipped"
    assert db.finish_comment_slot.call_args.kwargs["post_id"] is None
    assert db.finish_comment_slot.call_args.kwargs["sent"] is False
    db.update_group_link_classification.assert_called_once()


@pytest.mark.asyncio
async def test_link_scan_processes_only_broadcasts_then_classifies_groups(monkeypatch):
    db = MagicMock()
    db.get_channels.return_value = [
        {
            "channel_id": 10,
            "title": "Broadcast",
            "target_kind": "channel",
            "linked_chat_id": None,
        },
        {
            "channel_id": -1000000000020,
            "title": "Group",
            "target_kind": "group",
            "comment_mode": "pending",
            "linked_chat_id": -1000000000020,
        },
    ]
    db.refresh_group_comment_modes.return_value = {
        "linked_discussion": 0,
        "direct_group": 1,
    }
    telegram = _Telegram()
    telegram.peer_links[-1000000000020] = 10
    linked = _Linked()
    linked.links[10] = -1000000000020
    linked.access[-1000000000020] = True
    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch, db, telegram, linked=linked
    )

    await handlers["link_channels"]({"id": 701, "payload": {}})

    db.update_channel_link.assert_called_once_with(
        10,
        -1000000000020,
        None,
        "Связано · обсуждение уже в диалогах",
    )
    assert telegram.join_calls == []
    db.update_group_link_classification.assert_called_once_with(
        -1000000000020,
        is_linked=True,
        status="Связанное обсуждение · только комментарии к постам",
    )
    db.refresh_group_comment_modes.assert_called_once_with()
    db.update_task_progress.assert_any_call(701, 100)

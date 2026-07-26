from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.composition import ApplicationContainer
from core.exceptions import NonRetryableTelegramError
from tests.test_composition_resilience import (
    _Telegram,
    _comment_database,
    _handlers,
)


def test_container_shutdown_paths():
    container = object.__new__(ApplicationContainer)
    container.api = MagicMock()
    container.database = MagicMock()
    container.queue_worker = MagicMock()
    container.queue_worker.isRunning.return_value = False
    assert container.shutdown(123) is True
    container.api.prepare_shutdown.assert_called_once()
    container.database.close_thread_connection.assert_called_once()

    container.queue_worker.isRunning.return_value = True
    container.queue_worker.stop.return_value = False
    assert container.shutdown(456) is False
    container.queue_worker.stop.assert_called_with(456)


@pytest.mark.asyncio
async def test_comment_slot_uses_exact_discussion_target_without_relink_lookup(
    monkeypatch,
):
    db = _comment_database()
    telegram = _Telegram()
    telegram.chat_titles[30] = "Actual discussion"
    telegram.latest_post = SimpleNamespace(
        status="ok",
        message=SimpleNamespace(id=901),
        discussion_chat_id=30,
        discussion_message_id=777,
    )
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 100, "payload": {"campaign_id": 1, "slot_id": 100}}
    )

    db.update_channel_link.assert_not_called()
    assert telegram.member_calls == []
    assert telegram.join_calls == []
    assert comments.sent[0]["linked_chat_id"] == 30
    assert comments.sent[0]["reply_to"] == 777
    assert db.finish_comment_slot.call_args.kwargs["status"] == "sent"


@pytest.mark.asyncio
async def test_comment_slot_uses_links_preflight_without_get_permissions(monkeypatch):
    db = _comment_database()
    telegram = _Telegram()
    telegram.member_results = [False]
    telegram.latest_post = SimpleNamespace(
        status="ok",
        message=SimpleNamespace(id=901),
        discussion_chat_id=20,
        discussion_message_id=777,
    )
    handlers, _cleanup, comments, worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 101, "payload": {"campaign_id": 1, "slot_id": 101}}
    )

    assert telegram.member_calls == []
    assert telegram.join_calls == []
    assert worker.sleep_calls == []
    assert len(comments.sent) == 1
    assert db.finish_comment_slot.call_args.kwargs["status"] == "sent"


@pytest.mark.asyncio
async def test_prepared_comment_uses_resolved_root_without_post_join_refresh(
    monkeypatch,
):
    db = _comment_database()
    telegram = _Telegram()
    telegram.member_results = [True]
    telegram.latest_posts = [
        SimpleNamespace(
            status="ok",
            message=SimpleNamespace(id=901),
            discussion_chat_id=20,
            discussion_message_id=700,
        )
    ]
    handlers, _cleanup, comments, worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 103, "payload": {"campaign_id": 1, "slot_id": 103}}
    )

    assert telegram.latest_calls == 1
    assert telegram.join_calls == []
    assert worker.sleep_calls == []
    assert comments.sent[0]["reply_to"] == 700
    assert db.finish_comment_slot.call_args.kwargs["status"] == "sent"


@pytest.mark.asyncio
async def test_definitive_send_rejection_is_not_retried_as_post_join_send(
    monkeypatch,
):
    db = _comment_database()
    telegram = _Telegram()
    telegram.member_results = [True]
    telegram.latest_post = SimpleNamespace(
        status="ok",
        message=SimpleNamespace(id=901),
        discussion_chat_id=20,
        discussion_message_id=777,
    )
    handlers, _cleanup, comments, worker = _handlers(monkeypatch, db, telegram)
    comments.errors = [
        NonRetryableTelegramError(
            "forbidden",
            code="chat_write_forbidden",
            details={"rpc_error": "ChatWriteForbiddenError"},
        )
    ]

    await handlers["auto_comment_slot"](
        {"id": 104, "payload": {"campaign_id": 1, "slot_id": 104}}
    )

    assert telegram.latest_calls == 1
    assert telegram.join_calls == []
    assert worker.sleep_calls == []
    assert comments.sent == []
    assert db.finish_comment_slot.call_args.kwargs["status"] == "skipped"


@pytest.mark.asyncio
async def test_public_discussion_is_sent_without_membership_probe(
    monkeypatch,
):
    db = _comment_database()
    telegram = _Telegram()
    telegram.member_results = [False]
    telegram.latest_post = SimpleNamespace(
        status="ok",
        message=SimpleNamespace(id=901),
        discussion_chat_id=20,
        discussion_message_id=777,
    )
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 105, "payload": {"campaign_id": 1, "slot_id": 105}}
    )

    assert telegram.member_calls == []
    assert telegram.join_calls == []
    assert len(comments.sent) == 1
    assert db.finish_comment_slot.call_args.kwargs["status"] == "sent"

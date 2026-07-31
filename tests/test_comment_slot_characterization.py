from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from workers.comment_slot.handler import create_comment_slot_handler


class _Queue:
    def __init__(self, cancelled: list[bool] | None = None) -> None:
        self.cancelled = list(cancelled or [])
        self.cooldowns: list[tuple[int, int, str]] = []

    def is_scope_cancelled(self, *_scope: object) -> bool:
        return self.cancelled.pop(0) if self.cancelled else False

    def remember_account_rpc_cooldown(
        self, account_id: int, seconds: int, next_allowed_at: str
    ) -> None:
        self.cooldowns.append((account_id, seconds, next_allowed_at))


class _Telegram:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    def register_peer_reference(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def get_latest_post_for_commenting(
        self, _channel_id: int, **_kwargs: object
    ) -> SimpleNamespace:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            status="ok",
            message=SimpleNamespace(id=901, message="post"),
            discussion_chat_id=20,
            discussion_message_id=777,
        )


class _Comments:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.sent: list[dict[str, object]] = []
        self.activity_schedule = None

    async def ensure_and_send_comment(self, **kwargs: object) -> None:
        self.sent.append(dict(kwargs))
        if self.error is not None:
            raise self.error


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _database() -> MagicMock:
    db = MagicMock()
    db.get_account_restriction.return_value = {}
    db.get_comment_campaign.return_value = {
        "id": 1,
        "status": "running",
        "comments": ["hello"],
        "last_comment_text": "",
        "network_failure_count": 0,
        "account_id": 7,
    }
    db.get_campaign_comment_settings.return_value = {}
    db.get_comment_slot_route.return_value = None
    db.get_channels_for_commenting.return_value = [
        {
            "channel_id": 10,
            "linked_chat_id": 20,
            "title": "Channel",
            "comment_mode": "channel_post",
        }
    ]
    db.get_channel_by_id.side_effect = lambda channel_id, **_kwargs: (
        {"channel_id": 10, "linked_chat_id": 20, "title": "Channel", "comment_mode": "channel_post"}
        if int(channel_id) == 10
        else {"channel_id": int(channel_id)}
    )
    db.mark_comment_slot_running.return_value = True
    db.has_commented.return_value = False
    db.bind_comment_slot_target.return_value = True
    db.defer_comment_slot.return_value = True
    db.defer_comment_slot_and_set_network_wait.return_value = True
    db.set_account_rpc_cooldown.return_value = {"next_allowed_at": "later"}
    return db


def _handler(
    db: MagicMock,
    *,
    queue: _Queue | None = None,
    telegram: _Telegram | None = None,
    comments: _Comments | None = None,
):
    return create_comment_slot_handler(
        as_int=_as_int,
        queue_worker=queue or _Queue(),
        config=SimpleNamespace(),
        worker_db=db,
        telegram=telegram or _Telegram(),
        comments=comments or _Comments(),
        set_runtime=lambda *_args, **_kwargs: None,
    )


@pytest.mark.asyncio
async def test_confirmed_send_is_the_only_sent_outcome() -> None:
    db = _database()
    comments = _Comments()

    await _handler(db, comments=comments)(
        {"id": 101, "payload": {"campaign_id": 1, "slot_id": 2, "account_id": 7}}
    )

    assert len(comments.sent) == 1
    assert comments.sent[0]["post_message_id"] == 901
    assert comments.sent[0]["reply_to"] == 777
    assert db.finish_comment_slot.call_args.kwargs["status"] == "sent"
    assert db.finish_comment_slot.call_args.kwargs["sent"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_status", "paused"),
    [
        ("delivery_result_unknown", "uncertain", True),
        ("delivery_persist_failed", "failed", True),
        ("comment_already_reserved", "skipped", False),
        ("chat_write_forbidden", "skipped", False),
    ],
)
async def test_send_failure_table_preserves_uncertain_and_replay_contracts(
    code: str, expected_status: str, paused: bool
) -> None:
    db = _database()
    comments = _Comments(NonRetryableTelegramError(code, code=code))

    await _handler(db, comments=comments)(
        {"id": 102, "payload": {"campaign_id": 1, "slot_id": 2, "account_id": 7}}
    )

    assert db.finish_comment_slot.call_args.kwargs["status"] == expected_status
    assert db.finish_comment_slot.call_args.kwargs["sent"] is False
    assert db.pause_campaign_for_safety.called is paused
    if code in {"delivery_result_unknown", "delivery_persist_failed"}:
        db.mark_channel_comment_checked.assert_called_once_with(10)


@pytest.mark.asyncio
async def test_floodwait_defers_slot_and_installs_account_embargo() -> None:
    db = _database()
    queue = _Queue()
    telegram = _Telegram(
        DeferredTelegramError(
            "Telegram FloodWait", code="flood_wait_deferred", retry_after=120
        )
    )

    await _handler(db, queue=queue, telegram=telegram)(
        {"id": 103, "payload": {"campaign_id": 1, "slot_id": 2, "account_id": 7}}
    )

    assert queue.cooldowns[0][:2] == (7, 120)
    db.set_account_rpc_cooldown.assert_called_once()
    db.defer_comment_slot_and_set_network_wait.assert_called_once()
    db.finish_comment_slot.assert_not_called()
    db.update_task_progress.assert_called_once_with(103, 100)


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_is_uncertain_and_not_replayed() -> None:
    db = _database()
    comments = _Comments(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _handler(db, comments=comments)(
            {"id": 104, "payload": {"campaign_id": 1, "slot_id": 2, "account_id": 7}}
        )

    assert len(comments.sent) == 1
    assert db.finish_comment_slot.call_args.kwargs["status"] == "uncertain"
    assert "результат требует проверки" in db.finish_comment_slot.call_args.kwargs["result"]
    db.pause_campaign_for_safety.assert_called_once()


@pytest.mark.asyncio
async def test_cached_receipt_skips_route_and_send_rpcs() -> None:
    db = _database()
    db.get_comment_slot_route.return_value = {
        "channel_id": 10,
        "post_id": 901,
        "linked_chat_id": 20,
        "discussion_message_id": 777,
    }
    db.has_commented.return_value = True
    telegram = _Telegram()
    comments = _Comments()

    await _handler(db, telegram=telegram, comments=comments)(
        {"id": 105, "payload": {"campaign_id": 1, "slot_id": 2, "account_id": 7}}
    )

    assert telegram.calls == 0
    assert comments.sent == []
    assert "уже комментировали" in db.finish_comment_slot.call_args.kwargs["result"]

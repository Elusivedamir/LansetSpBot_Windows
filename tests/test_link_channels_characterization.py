from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from workers.handlers.link_channels import create_link_channels_handler


class _QueueWorker:
    def __init__(self) -> None:
        self.cancelled = False

    def isInterruptionRequested(self) -> bool:
        return False

    def is_scope_cancelled(self, *_scope: object) -> bool:
        return self.cancelled

    async def safe_sleep(self, _seconds: float) -> bool:
        return True

    def cancel_scopes_and_run(self, _scopes: object, mutation):
        return mutation()


class _Owner:
    def __init__(self) -> None:
        self.queue_worker = _QueueWorker()
        self.config = SimpleNamespace(
            min_join_interval_seconds=45,
            link_join_delay_min_seconds=0,
            link_join_delay_max_seconds=0,
            link_check_delay_min_seconds=0,
            link_check_delay_max_seconds=0,
            max_joins_per_hour=40,
        )

    @staticmethod
    def _as_int(value: object, default: int = 0) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            return int(default)


class _Database:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {int(row["channel_id"]): copy.deepcopy(row) for row in rows}
        self.checkpoints: list[dict[str, Any]] = []
        self.progress: list[int] = []
        self.join_events: list[tuple[int, str, int | None]] = []
        self.bans: list[tuple[int, int | None, str]] = []
        self.guard = {"allowed": True, "wait_seconds": 0, "effective_count": 0}
        self.postponed = False

    def get_setting(self, key: str, default: object = None) -> object:
        return 7 if key == "telegram.account_id" else default

    def get_channels(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(row) for row in self.rows.values()]

    def get_channel_by_id(
        self, channel_id: int, *, account_id: int | None = None
    ) -> dict[str, Any] | None:
        del account_id
        row = self.rows.get(int(channel_id))
        return copy.deepcopy(row) if row is not None else None

    @staticmethod
    def is_channel_locally_banned(
        self, channel_id: int, *, account_id: int | None = None
    ) -> bool:
        del account_id
        row = self.rows.get(int(channel_id))
        return bool(row and row.get("local_banned_at"))

    @staticmethod
    def ban_channel_locally(
        self,
        channel_id: int,
        reason: str,
        *,
        related_peer_id: int | None = None,
        account_id: int | None = None,
    ) -> bool:
        del account_id
        row = self.rows.get(int(channel_id))
        if row is None:
            return False
        row["local_banned_at"] = "now"
        row["link_checked_at"] = "now"
        row["link_status"] = f"Заблокирован · {reason}"
        self.bans.append((int(channel_id), related_peer_id, reason))
        return True

    def update_task_checkpoint(
        self, _task_id: int, payload: dict[str, Any], progress: int
    ) -> bool:
        self.checkpoints.append(copy.deepcopy(payload))
        self.progress.append(int(progress))
        return True

    def update_task_progress(self, _task_id: int, progress: int) -> bool:
        self.progress.append(int(progress))
        return True

    def update_channel_link(
        self,
        channel_id: int,
        linked_id: int | None,
        linked_title: str | None,
        status: str,
    ) -> bool:
        row = self.rows[int(channel_id)]
        row.update(
            linked_chat_id=linked_id,
            linked_chat_title=linked_title,
            link_status=status,
        )
        return True

    def mark_link_checked(
        self, channel_id: int, *, account_id: int | None = None
    ) -> bool:
        del account_id
        row = self.rows.get(int(channel_id))
        if row is not None:
            row["link_checked_at"] = "now"
        return True

    def get_join_guard(self, **_kwargs: object) -> dict[str, Any]:
        return copy.deepcopy(self.guard)

    def postpone_running_task_for_account_cooldown(
        self, _task_id: int, *, retry_at: object, code: str
    ) -> bool:
        del retry_at, code
        self.postponed = True
        return True

    def record_join_event(
        self, peer_id: int, status: str, *, account_id: int | None = None
    ) -> bool:
        self.join_events.append((int(peer_id), str(status), account_id))
        return True

    def update_group_link_classification(
        self, group_id: int, *, is_linked: bool, status: str, **_kwargs: object
    ) -> bool:
        row = self.rows[int(group_id)]
        row["comment_mode"] = "linked_discussion" if is_linked else "direct_group"
        row["link_status"] = status
        return True

    def refresh_group_comment_modes(self) -> bool:
        return True


class _Telegram:
    def __init__(self, outcome: object = True) -> None:
        self.outcome = outcome
        self.join_calls: list[int] = []

    def register_peer_reference(self, _peer_id: int, **_kwargs: object) -> None:
        return None

    async def join_without_confirmation(self, peer_id: int, **_kwargs: object) -> bool:
        self.join_calls.append(int(peer_id))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return bool(self.outcome)


class _Linked:
    def __init__(self, outcomes: dict[int, object]) -> None:
        self.outcomes = outcomes
        self.calls: list[int] = []

    async def get_linked_chat_id(self, channel_id: int, **_kwargs: object) -> int | None:
        self.calls.append(int(channel_id))
        outcome = self.outcomes.get(int(channel_id))
        if isinstance(outcome, BaseException):
            raise outcome
        return None if outcome is None else int(outcome)


def _row(
    channel_id: int,
    *,
    kind: str = "channel",
    comment_mode: str | None = None,
) -> dict[str, Any]:
    return {
        "channel_id": channel_id,
        "title": f"peer-{channel_id}",
        "target_kind": kind,
        "comment_mode": comment_mode,
        "link_checked_at": None,
        "local_banned_at": None,
    }


def _handler(db: _Database, telegram: _Telegram, linked: _Linked):
    return create_link_channels_handler(
        self=_Owner(),
        telegram=telegram,
        worker_db=db,
        linked=linked,
        set_runtime=lambda *_args, **_kwargs: None,
        publish_activity=lambda *_args, **_kwargs: None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("join_outcome", "expected_status", "expected_events"),
    [
        (True, "Связано · вступление выполнено", [(100, "joined", 7)]),
        (False, "Связано · участие уже было", []),
    ],
)
async def test_join_result_preserves_membership_contract(
    join_outcome: bool,
    expected_status: str,
    expected_events: list[tuple[int, str, int | None]],
) -> None:
    db = _Database([_row(1)])
    telegram = _Telegram(join_outcome)
    await _handler(db, telegram, _Linked({1: 100}))(
        {"id": 9, "payload": {"account_id": 7}}
    )

    assert db.rows[1]["link_status"] == expected_status
    assert db.join_events == expected_events
    assert telegram.join_calls == [100]


@pytest.mark.asyncio
async def test_join_request_is_not_counted_as_confirmed_membership() -> None:
    db = _Database([_row(1)])
    telegram = _Telegram(
        NonRetryableTelegramError("request sent", code="join_requested")
    )

    await _handler(db, telegram, _Linked({1: 100}))(
        {"id": 10, "payload": {"account_id": 7}}
    )

    assert db.rows[1]["link_status"] == "Связано · заявка на вступление отправлена"
    assert db.join_events == []
    assert db.rows[1]["link_checked_at"] == "now"


@pytest.mark.asyncio
async def test_unknown_join_result_bans_target_and_blocks_replay() -> None:
    db = _Database([_row(1)])
    telegram = _Telegram(
        NonRetryableTelegramError("unknown", code="join_result_unknown")
    )

    await _handler(db, telegram, _Linked({1: 100}))(
        {"id": 11, "payload": {"account_id": 7}}
    )

    assert db.bans == [(1, 100, "Результат вступления неизвестен")]
    assert db.rows[1]["local_banned_at"] == "now"
    assert db.join_events == []


@pytest.mark.asyncio
async def test_floodwait_target_is_advanced_and_not_replayed_on_resume() -> None:
    db = _Database([_row(1), _row(2), _row(3)])
    linked = _Linked(
        {
            1: None,
            2: DeferredTelegramError(
                "wait", code="flood_wait_deferred", retry_after=60
            ),
            3: None,
        }
    )
    handler = _handler(db, _Telegram(), linked)

    with pytest.raises(DeferredTelegramError):
        await handler({"id": 12, "payload": {"account_id": 7}})

    assert linked.calls == [1, 2]
    checkpoint_payload = copy.deepcopy(db.checkpoints[-1])
    assert checkpoint_payload["_link_checkpoint"]["channel_index"] == 2
    assert db.rows[2]["link_status"] == "Пропущено · Telegram FloodWait"
    assert db.rows[2]["link_checked_at"] == "now"

    linked.calls.clear()
    linked.outcomes[2] = None
    await handler({"id": 12, "payload": checkpoint_payload})

    assert linked.calls == [3]
    assert "_link_checkpoint" not in db.checkpoints[-1]


@pytest.mark.asyncio
async def test_existing_discussion_group_requires_no_join_rpc() -> None:
    db = _Database([_row(1), _row(100, kind="group")])
    telegram = _Telegram()

    await _handler(db, telegram, _Linked({1: 100}))(
        {"id": 13, "payload": {"account_id": 7}}
    )

    assert telegram.join_calls == []
    assert db.rows[1]["link_status"] == "Связано · обсуждение уже в диалогах"
    assert db.rows[100]["comment_mode"] == "linked_discussion"


@pytest.mark.asyncio
async def test_local_join_guard_preserves_cursor_without_join_rpc() -> None:
    db = _Database([_row(1)])
    db.guard = {"allowed": False, "wait_seconds": 40, "effective_count": 1}
    telegram = _Telegram()

    await _handler(db, telegram, _Linked({1: 100}))(
        {"id": 14, "payload": {"account_id": 7}}
    )

    assert telegram.join_calls == []
    assert db.postponed is True
    assert db.checkpoints[-1]["_link_checkpoint"]["channel_index"] == 0

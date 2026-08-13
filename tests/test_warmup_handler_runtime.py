from __future__ import annotations

import asyncio
import types as pytypes
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from telethon import types

from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from workers.handlers.warmup_step import (
    _catchup_delay_seconds,
    _extract_sent_message_id,
    _group_join_parts,
    _recover_existing_message_id,
    _stable_message_random_id,
    _unknown_result,
    create_warmup_step_handler,
)


class _Action(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _Client:
    def action(self, _peer: object, _action: str) -> _Action:
        return _Action()

    async def get_input_entity(self, _peer: object) -> object:
        raise AssertionError("telegram.execute should own get_input_entity calls")

    async def get_messages(self, _peer: object, *, limit: int) -> list[object]:
        raise AssertionError("telegram.execute should own recovery get_messages calls")


class _Telegram:
    def __init__(self) -> None:
        self.client = _Client()
        self.calls: list[tuple[str, object | None]] = []
        self.recent_messages: list[object] = []
        self.group_messages: list[object] = []
        self.group_error: BaseException | None = None
        self.join_error: BaseException | None = None
        self.execute_error: BaseException | None = None
        self.send_result: object = pytypes.SimpleNamespace(
            updates=[type("UpdateShortSentMessage", (), {"id": 777})()]
        )
        self.entity_results: dict[object, object] = {}

    async def execute(self, target, *args, **_kwargs):
        target_name = str(getattr(target, "__name__", "client"))
        request = args[0] if args else None
        request_name = type(request).__name__ if request is not None else ""
        self.calls.append((target_name, request))
        if self.execute_error is not None:
            exc = self.execute_error
            self.execute_error = None
            raise exc
        if target_name == "get_input_entity":
            locator = args[0] if args else None
            override = self.entity_results.get(locator)
            if isinstance(override, BaseException):
                raise override
            if override is not None:
                return override
            if locator in {102, "beta"}:
                return types.InputPeerUser(user_id=102, access_hash=0)
            return types.InputPeerUser(user_id=9001, access_hash=0)
        if target_name == "get_messages":
            return list(self.recent_messages)
        if request_name == "SendMessageRequest":
            return self.send_result
        return None

    async def join_saved_dialog(self, **_kwargs) -> None:
        if self.join_error is not None:
            raise self.join_error

    async def get_messages(self, _chat_ref: str, *, limit: int) -> list[object]:
        if self.group_error is not None:
            raise self.group_error
        return list(self.group_messages[:limit])


class _Queue:
    def __init__(self) -> None:
        self.notifications = 0
        self.sleep_result = True
        self.barriers: list[tuple[tuple[object, ...], ...]] = []
        self.sleeps: list[int] = []

    def create_scope_dispatch_barrier(self, *scopes, pre_dispatch_check):
        assert pre_dispatch_check() is True
        self.barriers.append(tuple(scopes))
        return object()

    async def safe_sleep(self, _seconds: int, *, cancel_scope) -> bool:
        assert cancel_scope[0] == "warmup_pair"
        self.sleeps.append(int(_seconds))
        return self.sleep_result

    def notify_task_available(self) -> None:
        self.notifications += 1


class _DB:
    def __init__(self, step: dict[str, Any] | None) -> None:
        self.step = dict(step) if step is not None else None
        self.accounts = {
            101: {"telegram_account_id": 101, "display_name": "Alpha One", "username": "alpha"},
            102: {"telegram_account_id": 102, "display_name": "Beta Two", "username": "beta"},
        }
        self.pair = {
            "id": 7,
            "status": "running",
            "account_a_id": 101,
            "account_b_id": 102,
            "owner_token_a": "a" * 32,
            "owner_token_b": "b" * 32,
            "reply_min_seconds": 120,
            "reply_max_seconds": 900,
        }
        self.group: dict[str, Any] | None = None
        self.finished: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.deferred: list[dict[str, Any]] = []
        self.rescheduled: list[dict[str, Any]] = []
        self.visits: list[dict[str, Any]] = []
        self.enqueued: list[int] = []
        self.leases: list[tuple[int, str]] = []
        self.released: list[tuple[int, str]] = []
        self.finish_completed = False
        self.previous_message_context: dict[str, Any] | None = None

    def begin_warmup_step(self, _step_id: int, *, account_id: int):
        if self.step is None:
            return None
        assert account_id == int(self.step["actor_account_id"])
        return dict(self.step)

    def acquire_account_activity_lease(
        self, account_id: int, *, owner_token: str, lease_seconds: int, metadata: dict[str, Any]
    ) -> None:
        assert lease_seconds == 30 * 60
        assert int(metadata["pair_id"]) == 7
        self.leases.append((account_id, owner_token))

    def get_warmup_pair(self, _pair_id: int):
        return dict(self.pair)

    def enqueue_warmup_step(self, pair_id: int):
        self.enqueued.append(pair_id)
        return {"pair_id": pair_id}

    def get_previous_warmup_message_context(
        self, *, pair_id: int, week_number: int, before_sequence_no: int
    ):
        assert pair_id == 7
        assert week_number == 1
        assert before_sequence_no == 11
        return (
            dict(self.previous_message_context)
            if self.previous_message_context is not None
            else None
        )

    def get_telegram_account(self, account_id: int):
        value = self.accounts.get(account_id)
        return dict(value) if value else None

    def choose_warmup_group_for_account(self, _account_id: int):
        return dict(self.group) if self.group else None

    def record_warmup_group_visit(self, **kwargs) -> None:
        self.visits.append(dict(kwargs))

    def finish_warmup_step(self, _step_id: int, **kwargs):
        self.finished.append(dict(kwargs))
        return {
            "completed": self.finish_completed,
            "pair": dict(self.pair),
        }

    def release_account_activity_lease(self, account_id: int, *, owner_token: str) -> None:
        self.released.append((account_id, owner_token))

    def defer_warmup_step(self, _step_id: int, *, clear_queue_task: bool) -> bool:
        self.deferred.append({"clear_queue_task": clear_queue_task})
        return True

    def reschedule_warmup_step_after_unknown(self, _step_id: int, **kwargs):
        self.rescheduled.append(dict(kwargs))
        return {"task_id": 88}

    def fail_warmup_step(self, _step_id: int, **kwargs):
        self.failed.append(dict(kwargs))
        return {"status": "failed"}


def _step(action: str, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": 11,
        "pair_id": 7,
        "actor_account_id": 101,
        "target_account_id": 102,
        "owner_token": "a" * 32,
        "week_number": 1,
        "sequence_no": 11,
        "scheduled_at": None,
        "action": action,
        "message_text": "Привет",
        "typing_seconds": 1,
        "reply_to_previous": False,
        "last_sender_account_id": 102,
        "last_message_id": 321,
        "posts_to_read": 2,
        "should_react": True,
    }
    value.update(overrides)
    return value


def _task() -> dict[str, Any]:
    return {
        "id": 44,
        "payload": {"account_id": 101, "pair_id": 7, "step_id": 11},
    }


def _handler(
    db: _DB,
    telegram: _Telegram,
    queue: _Queue,
    activity: list[tuple[str, dict[str, Any]]],
):
    return create_warmup_step_handler(
        queue_worker=queue,
        worker_db=db,
        telegram=telegram,
        set_runtime=lambda *_args, **_kwargs: None,
        publish_activity=lambda message, **kwargs: activity.append((message, dict(kwargs))),
        contact_phone_provider=lambda account_id: "+79990001122" if account_id == 102 else None,
    )


def test_warmup_helper_contracts_cover_retry_identity_and_group_parsing() -> None:
    unknown = NonRetryableTelegramError(
        "lost", code="warmup_message_result_unknown"
    )
    known = NonRetryableTelegramError("bad", code="warmup_partner_missing")
    assert _unknown_result(unknown) is True
    assert _unknown_result(known) is False

    first = _stable_message_random_id(pair_id=7, step_id=11, account_id=101)
    assert first == _stable_message_random_id(pair_id=7, step_id=11, account_id=101)
    assert first != _stable_message_random_id(pair_id=7, step_id=12, account_id=101)
    assert first != 0

    direct = type("UpdateShortSentMessage", (), {"id": 91})()
    nested = pytypes.SimpleNamespace(updates=[pytypes.SimpleNamespace(message=direct)])
    cyclic = pytypes.SimpleNamespace()
    cyclic.update = cyclic
    assert _extract_sent_message_id(direct) == 91
    assert _extract_sent_message_id(nested) == 91
    assert _extract_sent_message_id(cyclic) == 0
    assert _extract_sent_message_id(None) == 0

    assert _group_join_parts("@example") == ("example", None)
    assert _group_join_parts("https://t.me/example/?start=1") == ("example", None)
    assert _group_join_parts("https://t.me/+invite") == (None, "https://t.me/+invite")
    assert _group_join_parts("https://t.me/joinchat/hash") == (
        None,
        "https://t.me/joinchat/hash",
    )
    assert _group_join_parts("") == (None, None)


def test_recover_existing_message_id_filters_direction_text_and_reply() -> None:
    async def run() -> None:
        telegram = _Telegram()
        telegram.recent_messages = [
            pytypes.SimpleNamespace(id=1, out=False, message="Привет", reply_to_msg_id=321),
            pytypes.SimpleNamespace(id=2, out=True, message="Другое", reply_to_msg_id=321),
            pytypes.SimpleNamespace(id=3, out=True, message="Привет", reply_to_msg_id=999),
            pytypes.SimpleNamespace(
                id=4,
                out=True,
                message="Привет",
                reply_to_msg_id=0,
                reply_to=pytypes.SimpleNamespace(reply_to_msg_id=321),
            ),
        ]
        assert (
            await _recover_existing_message_id(
                telegram=telegram,
                peer="beta",
                text="Привет",
                reply_to=321,
                dispatch_barrier=object(),
            )
            == 4
        )
        assert (
            await _recover_existing_message_id(
                telegram=telegram,
                peer="beta",
                text="missing",
                reply_to=None,
                dispatch_barrier=object(),
            )
            == 0
        )

    asyncio.run(run())


def test_warmup_step_stopped_and_already_finished_short_circuit() -> None:
    async def run() -> None:
        queue = _Queue()
        telegram = _Telegram()
        activity: list[tuple[str, dict[str, Any]]] = []

        stopped = _DB(None)
        await _handler(stopped, telegram, queue, activity)(_task())
        assert stopped.leases == []
        assert any("шаг пропущен" in message for message, _meta in activity)

        finished = _DB(_step("message", already_finished=True))
        await _handler(finished, telegram, queue, activity)(_task())
        assert finished.enqueued == [7]
        assert finished.leases == []

    asyncio.run(run())


def test_ensure_contact_success_finishes_and_releases_completed_pair() -> None:
    async def run() -> None:
        db = _DB(_step("ensure_contact"))
        db.finish_completed = True
        telegram = _Telegram()
        queue = _Queue()
        activity: list[tuple[str, dict[str, Any]]] = []

        await _handler(db, telegram, queue, activity)(_task())

        assert db.finished[0]["skipped"] is False
        assert db.finished[0]["telegram_message_id"] is None
        assert db.released == [(101, "a" * 32), (102, "b" * 32)]
        assert queue.notifications == 0
        assert any(
            type(request).__name__ == "ImportContactsRequest"
            for _target, request in telegram.calls
            if request is not None
        )

    asyncio.run(run())


def test_message_success_uses_reply_and_persists_telegram_message_id() -> None:
    async def run() -> None:
        db = _DB(
            _step(
                "message",
                reply_to_previous=True,
                last_sender_account_id=102,
                last_message_id=321,
            )
        )
        db.previous_message_context = {
            "actor_account_id": 102,
            "target_account_id": 101,
            "message_text": "Предыдущее",
            "completed_at": None,
        }
        telegram = _Telegram()
        telegram.recent_messages = [
            pytypes.SimpleNamespace(
                id=812,
                out=False,
                sender_id=102,
                message="Предыдущее",
                date=None,
            )
        ]
        queue = _Queue()
        activity: list[tuple[str, dict[str, Any]]] = []

        await _handler(db, telegram, queue, activity)(_task())

        assert db.finished[0]["telegram_message_id"] == 777
        assert db.finished[0]["skipped"] is False
        assert db.enqueued == [7]
        assert queue.notifications == 1
        requests = [
            request
            for _target, request in telegram.calls
            if type(request).__name__ == "SendMessageRequest"
        ]
        assert len(requests) == 1
        request = requests[0]
        assert request.random_id == _stable_message_random_id(
            pair_id=7, step_id=11, account_id=101
        )
        assert int(request.reply_to.reply_to_msg_id) == 812

    asyncio.run(run())


def test_message_duplicate_recovers_existing_message_without_replaying_text() -> None:
    async def run() -> None:
        db = _DB(_step("message"))
        telegram = _Telegram()
        queue = _Queue()
        activity: list[tuple[str, dict[str, Any]]] = []
        telegram.execute_error = None

        original_execute = telegram.execute
        send_seen = False

        async def execute(target, *args, **kwargs):
            nonlocal send_seen
            request = args[0] if args else None
            if type(request).__name__ == "SendMessageRequest" and not send_seen:
                send_seen = True
                raise NonRetryableTelegramError(
                    "duplicate", code="message_random_id_duplicate"
                )
            return await original_execute(target, *args, **kwargs)

        telegram.execute = execute  # type: ignore[method-assign]
        telegram.recent_messages = [
            pytypes.SimpleNamespace(
                id=432, out=True, message="Привет", reply_to_msg_id=0, reply_to=None
            )
        ]

        await _handler(db, telegram, queue, activity)(_task())
        assert db.finished[0]["telegram_message_id"] == 432
        assert db.failed == []

    asyncio.run(run())


def test_private_reaction_skip_and_success_paths() -> None:
    async def run() -> None:
        queue = _Queue()
        telegram = _Telegram()
        activity: list[tuple[str, dict[str, Any]]] = []

        skipped = _DB(
            _step(
                "private_reaction",
                last_sender_account_id=101,
                last_message_id=321,
            )
        )
        await _handler(skipped, telegram, queue, activity)(_task())
        assert skipped.finished[0]["skipped"] is True

        success = _DB(_step("private_reaction"))
        success.previous_message_context = {
            "actor_account_id": 102,
            "target_account_id": 101,
            "message_text": "Предыдущее",
            "completed_at": None,
        }
        telegram.recent_messages = [
            pytypes.SimpleNamespace(
                id=913,
                out=False,
                sender_id=102,
                message="Предыдущее",
                date=None,
            )
        ]
        await _handler(success, telegram, queue, activity)(_task())
        assert success.finished[0]["skipped"] is False
        assert any(
            type(request).__name__ == "SendReactionRequest"
            for _target, request in telegram.calls
            if request is not None
        )

    asyncio.run(run())


def test_stale_username_identity_mismatch_fails_closed_before_send() -> None:
    async def run() -> None:
        db = _DB(_step("message"))
        telegram = _Telegram()
        telegram.entity_results[102] = ValueError("entity cache miss")
        telegram.entity_results["beta"] = types.InputPeerUser(
            user_id=999,
            access_hash=0,
        )
        queue = _Queue()
        activity: list[tuple[str, dict[str, Any]]] = []

        with pytest.raises(NonRetryableTelegramError) as exc_info:
            await _handler(db, telegram, queue, activity)(_task())

        assert exc_info.value.code == "warmup_partner_identity_mismatch"
        assert db.failed and db.failed[0]["uncertain"] is False
        assert not any(
            type(request).__name__ == "SendMessageRequest"
            for _target, request in telegram.calls
            if request is not None
        )

    asyncio.run(run())


def test_overdue_message_gets_human_scale_catchup_delay() -> None:
    delay = _catchup_delay_seconds(
        step={"scheduled_at": "2000-01-01 00:00:00"},
        pair={"reply_min_seconds": 120, "reply_max_seconds": 120},
        pair_id=7,
        step_id=11,
    )
    assert delay == 120
    assert (
        _catchup_delay_seconds(
            step={"scheduled_at": None},
            pair={"reply_min_seconds": 120, "reply_max_seconds": 900},
            pair_id=7,
            step_id=11,
        )
        == 0
    )


def test_overdue_message_publishes_safe_wait_deadline_before_typing() -> None:
    async def run() -> None:
        db = _DB(_step("message", scheduled_at="2000-01-01 00:00:00"))
        db.pair["reply_min_seconds"] = 120
        db.pair["reply_max_seconds"] = 120
        telegram = _Telegram()
        queue = _Queue()
        activity: list[tuple[str, dict[str, Any]]] = []
        runtime: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        handler = create_warmup_step_handler(
            queue_worker=queue,
            worker_db=db,
            telegram=telegram,
            set_runtime=lambda *args, **kwargs: runtime.append(
                (tuple(args), dict(kwargs))
            ),
            publish_activity=lambda message, **kwargs: activity.append(
                (message, dict(kwargs))
            ),
            contact_phone_provider=lambda _account_id: None,
        )

        await handler(_task())

        safe_wait = [
            (args, kwargs)
            for args, kwargs in runtime
            if "Ожидание безопасного интервала" in str(args[1])
        ]
        assert len(safe_wait) == 1
        assert safe_wait[0][1]["wait_seconds"] == 120
        assert queue.sleeps[:2] == [120, 1]
        assert db.finished and db.finished[0]["skipped"] is False

    asyncio.run(run())


def test_group_visit_without_group_and_pending_membership_are_safe_skips() -> None:
    async def run() -> None:
        queue = _Queue()
        telegram = _Telegram()
        activity: list[tuple[str, dict[str, Any]]] = []

        no_group = _DB(_step("group_visit"))
        await _handler(no_group, telegram, queue, activity)(_task())
        assert no_group.finished[0]["skipped"] is True

        pending = _DB(_step("group_visit"))
        pending.group = {
            "id": 5,
            "chat_ref": "https://t.me/+invite",
            "membership_state": "unknown",
        }
        telegram.join_error = type("InviteRequestSentError", (Exception,), {})("pending")
        await _handler(pending, telegram, queue, activity)(_task())
        assert pending.finished[0]["skipped"] is True
        assert pending.visits[-1]["membership_state"] == "requested"

    asyncio.run(run())


def test_group_visit_reads_and_reacts_after_join() -> None:
    async def run() -> None:
        db = _DB(_step("group_visit", posts_to_read=2, should_react=True))
        db.group = {
            "id": 5,
            "chat_ref": "@publicgroup",
            "membership_state": "unknown",
        }
        telegram = _Telegram()
        telegram.group_messages = [
            pytypes.SimpleNamespace(id=20),
            pytypes.SimpleNamespace(id=19),
        ]
        queue = _Queue()
        activity: list[tuple[str, dict[str, Any]]] = []

        await _handler(db, telegram, queue, activity)(_task())

        assert db.finished[0]["skipped"] is False
        assert db.visits[-1]["membership_state"] == "joined"
        assert db.visits[-1]["last_read_message_id"] == 20
        assert db.visits[-1]["last_reacted_message_id"] == 20
        request_names = {
            type(request).__name__
            for _target, request in telegram.calls
            if request is not None
        }
        assert {"ReadHistoryRequest", "SendReactionRequest"} <= request_names

    asyncio.run(run())


def test_unknown_result_is_rescheduled_but_deferred_error_keeps_same_queue_task() -> None:
    async def run() -> None:
        activity: list[tuple[str, dict[str, Any]]] = []

        db = _DB(_step("ensure_contact"))
        telegram = _Telegram()
        telegram.execute_error = NonRetryableTelegramError(
            "unknown", code="warmup_contact_result_unknown"
        )
        queue = _Queue()
        await _handler(db, telegram, queue, activity)(_task())
        assert db.rescheduled
        assert db.rescheduled[0]["delay_seconds"] == 5 * 60
        assert db.failed == []
        assert queue.notifications == 1

        deferred_db = _DB(_step("ensure_contact"))
        deferred_telegram = _Telegram()
        deferred_telegram.execute_error = DeferredTelegramError(
            "wait", code="flood_wait", retry_after=20
        )
        deferred_queue = _Queue()
        with pytest.raises(DeferredTelegramError):
            await _handler(
                deferred_db, deferred_telegram, deferred_queue, activity
            )(_task())
        assert deferred_db.deferred == [{"clear_queue_task": False}]
        assert deferred_db.failed == []

    asyncio.run(run())


def test_cancelled_typing_defers_without_sending_or_failing_pair() -> None:
    async def run() -> None:
        db = _DB(_step("message"))
        telegram = _Telegram()
        queue = _Queue()
        queue.sleep_result = False
        activity: list[tuple[str, dict[str, Any]]] = []

        await _handler(db, telegram, queue, activity)(_task())

        assert db.deferred == [{"clear_queue_task": True}]
        assert db.finished == []
        assert db.failed == []
        assert not any(
            type(request).__name__ == "SendMessageRequest"
            for _target, request in telegram.calls
            if request is not None
        )

    asyncio.run(run())


def test_invalid_action_pauses_pair_and_re_raises_non_retryable_error() -> None:
    async def run() -> None:
        db = _DB(_step("does_not_exist"))
        telegram = _Telegram()
        queue = _Queue()
        activity: list[tuple[str, dict[str, Any]]] = []

        with pytest.raises(NonRetryableTelegramError) as exc_info:
            await _handler(db, telegram, queue, activity)(_task())

        assert exc_info.value.code == "warmup_action_invalid"
        assert db.failed and db.failed[0]["uncertain"] is False
        assert any(meta.get("level") == "ERROR" for _message, meta in activity)

    asyncio.run(run())


def test_lease_failure_is_persisted_instead_of_leaving_step_running() -> None:
    class LeaseFailDB(_DB):
        def acquire_account_activity_lease(
            self,
            account_id: int,
            *,
            owner_token: str,
            lease_seconds: int,
            metadata: dict[str, Any],
        ) -> None:
            raise RuntimeError("lease acquisition failed")

    async def run() -> None:
        db = LeaseFailDB(_step("message"))
        telegram = _Telegram()
        queue = _Queue()
        activity: list[tuple[str, dict[str, Any]]] = []

        with pytest.raises(RuntimeError, match="lease acquisition failed"):
            await _handler(db, telegram, queue, activity)(_task())

        assert db.failed
        assert db.failed[0]["uncertain"] is False
        assert "lease acquisition failed" in str(db.failed[0]["message"])

    asyncio.run(run())

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.account_runtime_manager import create_multiaccount_handlers
from services.api_parts.task_queue import TaskQueueAPIMixin
from services.telegram.audience import TelegramAudienceMixin
from storage.database import Database
from workers.handlers.parse_audience import create_audience_parser_handler


class _RouterQueue:
    def __init__(self, database):
        self.database = database

    def get_db(self):
        return self.database

    def is_scope_cancelled(self, *_scope):
        return False


async def _run_router_scenario():
    worker_db = MagicMock()
    worker_db.get_telegram_account.return_value = {
        "authorized": True,
        "stopped": False,
        "runtime_state": "connected",
    }
    worker_db.get_account_restriction.return_value = None
    container = SimpleNamespace(queue_worker=_RouterQueue(worker_db))
    calls = []

    async def parse_audience(task):
        calls.append(task)
        return {"account_id": task["account_id"]}

    async def runtime_cleanup():
        return None

    def create_handlers(context, **_factories):
        assert context is container
        return {"parse_audience": parse_audience}, runtime_cleanup

    handlers, cleanup = create_multiaccount_handlers(
        container,
        create_worker_handlers=create_handlers,
        TelegramService=object,
        ImportService=object,
        LinkedChatService=object,
        CommentService=object,
    )
    try:
        result = await handlers["parse_audience"](
            {
                "id": 7,
                "account_id": 101,
                "type": "parse_audience",
                "payload": {"account_id": 101},
            }
        )
    finally:
        await cleanup()
    return result, calls


def test_production_router_exposes_audience_parser() -> None:
    result, calls = asyncio.run(_run_router_scenario())
    assert result == {"account_id": 101}
    assert len(calls) == 1
    assert calls[0]["payload"]["account_id"] == 101


class _AudienceResolver(TelegramAudienceMixin):
    def __init__(self):
        self.client = SimpleNamespace(get_entity=lambda _peer: None)
        self.calls = []

    async def ensure_connected(self):
        return None

    async def execute(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        return SimpleNamespace(
            title="Test Group",
            broadcast=False,
            megagroup=True,
            left=False,
            deactivated=False,
        )


def test_group_resolution_forwards_task_dispatch_barrier() -> None:
    resolver = _AudienceResolver()
    barrier = object()

    entity = asyncio.run(
        resolver.resolve_audience_group(
            {"link": "@test_group"},
            dispatch_barrier=barrier,
        )
    )

    assert entity.title == "Test Group"
    assert len(resolver.calls) == 1
    assert resolver.calls[0][2]["dispatch_barrier"] is barrier


class _CancellationDatabase:
    def __init__(self):
        self.cancel_calls = 0

    def get_task(self, task_id):
        assert int(task_id) == 7
        return {
            "id": 7,
            "type": "parse_audience",
            "status": "running",
        }

    def cancel_task(self, _task_id):
        self.cancel_calls += 1
        return False


class _CancellationWorker:
    def __init__(self):
        self.scopes = ()

    def cancel_scopes_and_run(self, scopes, mutation):
        self.scopes = tuple(scopes)
        return mutation()


class _CancellationAPI(TaskQueueAPIMixin):
    pass


def test_running_parser_stop_is_accepted_and_task_scoped() -> None:
    api = _CancellationAPI()
    api.database = _CancellationDatabase()
    api.queue_worker = _CancellationWorker()

    assert api.cancel_task(7) is True
    assert api.queue_worker.scopes == (("task", 7),)
    assert api.database.cancel_calls == 0


class _ParserQueue:
    def __init__(
        self,
        task_id: int,
        *,
        cancel_after_scope_checks: int | None = None,
        interrupt_after_scope_checks: int | None = None,
    ) -> None:
        self.task_id = int(task_id)
        self.cancel_after_scope_checks = cancel_after_scope_checks
        self.interrupt_after_scope_checks = interrupt_after_scope_checks
        self.scope_checks = 0

    def is_scope_cancelled(self, scope_type, scope_id):
        assert (scope_type, scope_id) == ("task", self.task_id)
        self.scope_checks += 1
        return bool(
            self.cancel_after_scope_checks is not None
            and self.scope_checks >= self.cancel_after_scope_checks
        )

    def isInterruptionRequested(self):
        return bool(
            self.interrupt_after_scope_checks is not None
            and self.scope_checks >= self.interrupt_after_scope_checks
        )

    def create_scope_dispatch_barrier(self, *scopes):
        assert scopes == (("task", self.task_id),)
        return None


class _ParserTelegram:
    async def resolve_audience_group(self, source, *, dispatch_barrier=None):
        assert source == {"link": "@test_group"}
        assert dispatch_barrier is None
        return SimpleNamespace(title="Test Group")

    async def iter_audience_members(self, _entity, *, dispatch_barrier=None):
        assert dispatch_barrier is None
        for index in range(3):
            yield SimpleNamespace(
                username=f"User{index}",
                deleted=False,
                bot=False,
            )


def _parser_payload(output_path):
    return {
        "account_id": 101,
        "source": {"link": "@test_group"},
        "source_title": "Test Group",
        "output_path": str(output_path),
    }


def _parser_task(task_id: int, output_path):
    return {
        "id": int(task_id),
        "account_id": 101,
        "payload": _parser_payload(output_path),
    }


def test_task_local_stop_persists_cancelled_and_removes_partial_file(tmp_path) -> None:
    database = Database(tmp_path / "cancel.db")
    output = tmp_path / "audience.txt"
    task_id = database.insert_task("parse_audience", _parser_payload(output))
    assert database.set_processing(task_id)

    handler = create_audience_parser_handler(
        queue_worker=_ParserQueue(task_id, cancel_after_scope_checks=3),
        worker_db=database,
        telegram=_ParserTelegram(),
        set_runtime=lambda *args, **kwargs: None,
        publish_activity=lambda *args, **kwargs: None,
    )
    asyncio.run(handler(_parser_task(task_id, output)))

    assert database.get_task(task_id)["status"] == "cancelled"
    assert not output.exists()
    assert list(tmp_path.glob("*.part")) == []


def test_shutdown_does_not_persist_user_cancellation(tmp_path) -> None:
    database = Database(tmp_path / "shutdown.db")
    output = tmp_path / "audience.txt"
    task_id = database.insert_task("parse_audience", _parser_payload(output))
    assert database.set_processing(task_id)

    handler = create_audience_parser_handler(
        queue_worker=_ParserQueue(task_id, interrupt_after_scope_checks=2),
        worker_db=database,
        telegram=_ParserTelegram(),
        set_runtime=lambda *args, **kwargs: None,
        publish_activity=lambda *args, **kwargs: None,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(handler(_parser_task(task_id, output)))

    assert database.get_task(task_id)["status"] == "running"
    assert not output.exists()
    assert list(tmp_path.glob("*.part")) == []


def test_crash_recovery_requeues_audience_parser(tmp_path) -> None:
    database = Database(tmp_path / "recovery.db")
    output = tmp_path / "audience.txt"
    task_id = database.insert_task("parse_audience", _parser_payload(output))
    assert database.set_processing(task_id)

    assert database.reset_running_tasks() == 1
    recovered = database.get_task(task_id)
    assert recovered["status"] == "pending"
    assert recovered["error"] == "Recovered after unclean shutdown"

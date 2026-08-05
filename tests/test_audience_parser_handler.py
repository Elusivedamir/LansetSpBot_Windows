from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from workers.handlers.parse_audience import create_audience_parser_handler


class _FakeDatabase:
    def __init__(self):
        self.progress = []
        self.status = "running"
        self.cancelled = []

    def update_task_progress(self, task_id, value):
        self.progress.append((int(task_id), int(value)))

    def cancel_running_audience_task(self, task_id, reason):
        self.cancelled.append((int(task_id), str(reason)))
        self.status = "cancelled"
        return True

    def get_task(self, task_id):
        assert int(task_id) == 7
        return {"id": 7, "status": self.status}


class _FakeBarrier:
    def __init__(self):
        self.dispatch_calls = 0

    @contextmanager
    def dispatch(self, _request=None):
        self.dispatch_calls += 1
        yield


class _FakeQueue:
    def __init__(self, *, cancel_after_checks=None):
        self.cancel_after_checks = cancel_after_checks
        self.checks = 0
        self.dispatch_barrier = _FakeBarrier()

    def isInterruptionRequested(self):
        return False

    def is_scope_cancelled(self, scope_type, scope_id):
        assert scope_type == "task"
        assert scope_id == 7
        self.checks += 1
        return (
            self.cancel_after_checks is not None
            and self.checks >= self.cancel_after_checks
        )

    def create_scope_dispatch_barrier(self, *scopes):
        assert scopes == (("task", 7),)
        return self.dispatch_barrier


class _FakeTelegram:
    def __init__(self, users):
        self.users = list(users)
        self.dispatch_barrier = None

    async def resolve_audience_group(self, source, *, dispatch_barrier=None):
        assert source
        assert dispatch_barrier is not None
        self.dispatch_barrier = dispatch_barrier
        return SimpleNamespace(title="Test Group")

    async def iter_audience_members(self, _entity, *, dispatch_barrier=None):
        assert dispatch_barrier is self.dispatch_barrier
        for user in self.users:
            yield user


def _task(output_path):
    return {
        "id": 7,
        "account_id": 101,
        "payload": {
            "account_id": 101,
            "source": {"link": "@test_group"},
            "source_title": "Test Group",
            "output_path": str(output_path),
        },
    }


def test_handler_exports_only_unique_live_human_usernames(tmp_path):
    output = tmp_path / "audience.txt"
    users = [
        SimpleNamespace(username="Alice", deleted=False, bot=False),
        SimpleNamespace(username="alice", deleted=False, bot=False),
        SimpleNamespace(username=None, deleted=False, bot=False),
        SimpleNamespace(username="Deleted", deleted=True, bot=False),
        SimpleNamespace(username="Robot", deleted=False, bot=True),
        SimpleNamespace(username="Bob", deleted=False, bot=False),
    ]
    database = _FakeDatabase()
    statuses = []
    queue = _FakeQueue()
    telegram = _FakeTelegram(users)
    handler = create_audience_parser_handler(
        queue_worker=queue,
        worker_db=database,
        telegram=telegram,
        set_runtime=lambda *args, **kwargs: statuses.append((args, kwargs)),
        publish_activity=lambda *args, **kwargs: None,
    )

    asyncio.run(handler(_task(output)))

    assert output.read_text(encoding="utf-8") == "@Alice\n@Bob\n"
    assert database.progress[-1] == (7, 100)
    assert queue.dispatch_barrier.dispatch_calls == 1
    assert telegram.dispatch_barrier is queue.dispatch_barrier
    assert "удалённых: 1" in statuses[-1][0][1]
    assert "ботов: 1" in statuses[-1][0][1]
    assert "дубликатов: 1" in statuses[-1][0][1]


def test_handler_cancellation_removes_partial_file(tmp_path):
    output = tmp_path / "audience.txt"
    users = [
        SimpleNamespace(username=f"User{index}", deleted=False, bot=False)
        for index in range(10)
    ]
    database = _FakeDatabase()
    handler = create_audience_parser_handler(
        queue_worker=_FakeQueue(cancel_after_checks=3),
        worker_db=database,
        telegram=_FakeTelegram(users),
        set_runtime=lambda *args, **kwargs: None,
        publish_activity=lambda *args, **kwargs: None,
    )

    asyncio.run(handler(_task(output)))

    assert not output.exists()
    assert list(tmp_path.glob("*.part")) == []
    assert database.status == "cancelled"
    assert database.cancelled == [(7, "Остановлено пользователем")]

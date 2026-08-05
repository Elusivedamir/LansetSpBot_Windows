from __future__ import annotations

import asyncio
from types import SimpleNamespace

from workers.handlers.parse_audience import create_audience_parser_handler


class _FakeDatabase:
    def __init__(self):
        self.progress = []

    def update_task_progress(self, task_id, value):
        self.progress.append((int(task_id), int(value)))


class _FakeQueue:
    def __init__(self, *, cancel_after_checks=None):
        self.cancel_after_checks = cancel_after_checks
        self.checks = 0

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
        return None


class _FakeTelegram:
    def __init__(self, users):
        self.users = list(users)

    async def resolve_audience_group(self, source):
        assert source
        return SimpleNamespace(title="Test Group")

    async def iter_audience_members(self, _entity, *, dispatch_barrier=None):
        assert dispatch_barrier is None
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
    handler = create_audience_parser_handler(
        queue_worker=_FakeQueue(),
        worker_db=database,
        telegram=_FakeTelegram(users),
        set_runtime=lambda *args, **kwargs: statuses.append((args, kwargs)),
        publish_activity=lambda *args, **kwargs: None,
    )

    asyncio.run(handler(_task(output)))

    assert output.read_text(encoding="utf-8") == "@Alice\n@Bob\n"
    assert database.progress[-1] == (7, 100)
    assert "удалённых: 1" in statuses[-1][0][1]
    assert "ботов: 1" in statuses[-1][0][1]
    assert "дубликатов: 1" in statuses[-1][0][1]


def test_handler_cancellation_removes_partial_file(tmp_path):
    output = tmp_path / "audience.txt"
    users = [
        SimpleNamespace(username=f"User{index}", deleted=False, bot=False)
        for index in range(10)
    ]
    handler = create_audience_parser_handler(
        queue_worker=_FakeQueue(cancel_after_checks=3),
        worker_db=_FakeDatabase(),
        telegram=_FakeTelegram(users),
        set_runtime=lambda *args, **kwargs: None,
        publish_activity=lambda *args, **kwargs: None,
    )

    asyncio.run(handler(_task(output)))

    assert not output.exists()
    assert list(tmp_path.glob("*.part")) == []

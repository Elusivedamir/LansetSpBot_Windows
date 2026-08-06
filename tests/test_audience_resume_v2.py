from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from workers.handlers.parse_audience import create_audience_parser_handler


class _Database:
    def __init__(self):
        self.progress = []
        self.cancelled = []

    def update_task_progress(self, task_id, value):
        self.progress.append((int(task_id), int(value)))

    def cancel_running_audience_task(self, task_id, reason):
        self.cancelled.append((int(task_id), str(reason)))
        return True

    def get_task(self, task_id):
        return {"id": int(task_id), "status": "running"}


class _Barrier:
    def __init__(self):
        self.dispatch_calls = 0

    @contextmanager
    def dispatch(self, _request=None):
        self.dispatch_calls += 1
        yield


class _Queue:
    def __init__(self, *, cancel_after=None):
        self.cancel_after = cancel_after
        self.checks = 0
        self.barrier = _Barrier()

    def isInterruptionRequested(self):
        return False

    def is_scope_cancelled(self, scope_type, scope_id):
        assert (scope_type, scope_id) == ("task", 7)
        self.checks += 1
        return self.cancel_after is not None and self.checks >= self.cancel_after

    def create_scope_dispatch_barrier(self, *scopes):
        assert scopes == (("task", 7),)
        return self.barrier


class _PagedTelegram:
    def __init__(self, pages):
        self.pages = pages
        self.offsets = []

    async def resolve_audience_group(self, source, *, dispatch_barrier=None):
        assert source == {"link": "@group"}
        return SimpleNamespace(title="Group")

    async def iter_audience_member_pages(
        self, _entity, *, offset, page_size, dispatch_barrier=None
    ):
        assert page_size == 200
        self.offsets.append(offset)
        for next_offset, page in self.pages:
            yield next_offset, page


def _task(output, temp):
    return {
        "id": 7,
        "account_id": 101,
        "payload": {
            "account_id": 101,
            "source": {"link": "@group"},
            "source_title": "Group",
            "output_path": str(output),
            "filters": {
                "exclude_admins": False,
                "exclude_scam_fake": False,
                "activity_days": 0,
            },
            "_audience_checkpoint": {
                "version": 2,
                "task_id": 7,
                "account_id": 101,
                "source": {"link": "@group"},
                "source_title": "Group",
                "output_path": str(output.resolve()),
                "temp_path": str(temp.resolve()),
                "filters": {
                    "exclude_admins": False,
                    "exclude_scam_fake": False,
                    "activity_days": 0,
                },
                "offset": 200,
                "file_size": temp.stat().st_size,
                "counters": {
                    "scanned": 200,
                    "saved": 1,
                    "missing_username": 0,
                    "deleted": 0,
                    "bot": 0,
                    "duplicate": 0,
                    "administrator": 0,
                    "scam_fake": 0,
                    "inactive": 0,
                },
                "awaiting_user_choice": False,
                "resume_approved": True,
            },
        },
    }


def test_resume_requests_first_new_page_at_saved_offset(tmp_path):
    output = tmp_path / "audience.txt"
    temp = tmp_path / ".audience.txt.7.part"
    temp.write_text("@Alice\n", encoding="utf-8")
    telegram = _PagedTelegram(
        [
            (
                202,
                [
                    (SimpleNamespace(username="Bob", deleted=False, bot=False), False),
                    (SimpleNamespace(username="Carol", deleted=False, bot=False), False),
                ],
            )
        ]
    )
    database = _Database()
    handler = create_audience_parser_handler(
        queue_worker=_Queue(),
        worker_db=database,
        telegram=telegram,
        set_runtime=lambda *args, **kwargs: None,
        publish_activity=lambda *args, **kwargs: None,
    )

    asyncio.run(handler(_task(output, temp)))

    assert telegram.offsets == [200]
    assert output.read_text(encoding="utf-8") == "@Alice\n@Bob\n@Carol\n"
    assert database.progress[-1] == (7, 100)


def test_explicit_cancellation_closes_then_removes_partial_file(tmp_path):
    output = tmp_path / "audience.txt"
    temp = tmp_path / ".audience.txt.7.part"
    temp.write_text("@Alice\n", encoding="utf-8")
    telegram = _PagedTelegram(
        [
            (201, [(SimpleNamespace(username="Bob", deleted=False, bot=False), False)]),
            (202, [(SimpleNamespace(username="Carol", deleted=False, bot=False), False)]),
        ]
    )
    database = _Database()
    handler = create_audience_parser_handler(
        queue_worker=_Queue(cancel_after=4),
        worker_db=database,
        telegram=telegram,
        set_runtime=lambda *args, **kwargs: None,
        publish_activity=lambda *args, **kwargs: None,
    )

    asyncio.run(handler(_task(output, temp)))

    assert not output.exists()
    assert not temp.exists()
    assert database.cancelled == [(7, "Остановлено пользователем")]

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QObject

from core.campaign_schedule import utc_now
from core.logging_setup import _BoundedFormatter
from core.redaction import sanitize_text
from gui.background import BackgroundCall, connect_lifecycle_safe
from services.api import ServiceAPI
from services.comment_service import CommentService
from storage.database import Database
from workers.queue_worker import QueueWorker
from tests.conftest import open_project_database


@pytest.mark.asyncio
async def test_forward_wall_clock_jump_does_not_bypass_monotonic_cooldown(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "clock.db")
    db.set_setting("telegram.account_id", 101)
    task_id = db.insert_task("comment", {"account_id": 101})
    task = db.claim_next_pending_task()
    assert task and task["id"] == task_id
    db.set_account_rpc_cooldown(
        account_id=101,
        retry_at=utc_now() + timedelta(hours=1),
        source_task_id=task_id,
    )

    called: list[int] = []

    async def handler(current):
        called.append(int(current["id"]))

    worker = QueueWorker(lambda: {})
    worker._db = db
    worker._handlers = {"comment": handler}

    # First observation establishes the process-local monotonic deadline.
    await worker._process_task_impl(task)
    assert called == []
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET status='running', not_before=NULL WHERE id=?", (task_id,)
        )
    task = dict(task)
    task["status"] = "running"

    import workers.queue_worker as queue_module

    real_wall = utc_now()
    monkeypatch.setattr(queue_module, "utc_now", lambda: real_wall + timedelta(hours=2))
    await worker._process_task_impl(task)

    assert called == []
    stored = db.get_task(task_id)
    assert stored["status"] == "pending"
    assert db.get_account_rpc_cooldown(account_id=101)["active"] == 1


def test_secret_redaction_covers_sqlite_journal_and_file_formatter(tmp_path):
    secret = "P@ss:SuperSecret-987"
    raw = (
        f"ProxyError: password={secret}; api_hash='0123456789abcdef'; "
        "phone=+49123456789; verification_code=12345"
    )
    db = Database(tmp_path / "secret.db")
    db.set_setting("telegram.account_id", 102)
    task_id = db.insert_task("comment", {"account_id": 102})
    task = db.claim_next_pending_task()

    async def handler(_task):
        raise RuntimeError(raw)

    worker = QueueWorker(lambda: {})
    worker._db = db
    worker._handlers = {"comment": handler}
    asyncio.run(worker._process_task_impl(task))

    error = str(db.get_task(task_id)["error"])
    assert secret not in error
    assert "0123456789abcdef" not in error
    assert "+49123456789" not in error
    assert "12345" not in error
    assert "<redacted>" in error

    db.insert_log("ERROR", raw)
    journal = str(db.get_logs(limit=1)[0]["message"])
    assert secret not in journal
    assert "<redacted>" in journal

    record = logging.LogRecord("probe", logging.ERROR, __file__, 1, raw, (), None)
    formatted = _BoundedFormatter("%(message)s").format(record)
    assert secret not in formatted
    assert "<redacted>" in formatted

    unlabelled = sanitize_text(
        f"Proxy authentication failed for credential {secret}",
        secrets=(secret,),
    )
    assert secret not in unlabelled
    assert "<redacted>" in unlabelled


def test_v25_disables_legacy_direct_group_work_before_worker_start(tmp_path):
    path = tmp_path / "direct-group.db"
    db = Database(path)
    db.set_setting("telegram.account_id", 201)
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO channels(
                   account_id, channel_id, title, target_kind, comment_mode,
                   linked_chat_id, link_status)
               VALUES(201, -1009001, 'ordinary', 'group', 'direct_group',
                      -1009001, 'Группа · прямая отправка')"""
        )
    task_id = db.insert_task(
        "direct_message", {"account_id": 201, "chat_id": -1009001, "text": "x"}
    )
    db.close_thread_connection()


    with open_project_database(path) as conn:
        conn.execute("DELETE FROM migrations WHERE version=25")
        conn.execute("PRAGMA user_version=24")

    migrated = Database(path)
    assert migrated.get_version() == Database.SCHEMA_VERSION
    channel = migrated.get_channel_by_id(-1009001, account_id=201)
    assert channel["comment_mode"] == "pending"
    assert channel["linked_chat_id"] is None
    task = migrated.get_task(task_id)
    assert task["status"] == "cancelled"
    assert migrated.get_channels_for_commenting(10, account_id=201) == []
    assert migrated.refresh_group_comment_modes(account_id=201)["direct_group"] == 0


@pytest.mark.asyncio
async def test_direct_group_path_is_campaign_only_and_persists_receipt(tmp_path):
    db = Database(tmp_path / "direct-group-enabled.db")
    db.set_setting("telegram.account_id", 202)
    api = ServiceAPI(db)
    assert api.wait_for_secret_migration(5_000)
    with pytest.raises(ValueError, match="Unsupported task type"):
        api.create_task("direct_message", {"chat_id": -1001, "text": "x"})
    api.prepare_shutdown()

    task_id = db.insert_task(
        "direct_message", {"account_id": 202, "chat_id": -1001, "text": "x"}
    )

    class Telegram:
        calls = 0

        async def send_message(self, *_args, **_kwargs):
            self.calls += 1
            return SimpleNamespace(id=777)

    telegram = Telegram()
    service = CommentService(telegram, db=db)
    result = await service.send_direct_message(
        -1001, "x", task_id=task_id, account_id=202, campaign_id=5
    )

    assert result.id == 777
    assert telegram.calls == 1
    delivery = db.get_direct_message_delivery(task_id)
    assert delivery["status"] == "sent"
    assert delivery["message_id"] == 777


def test_lifecycle_safe_background_callbacks_ignore_deleted_owner():
    owner = QObject()
    child = QObject(owner)
    child.setObjectName("alive")
    job = BackgroundCall(lambda: None)
    calls: list[str] = []

    def succeeded(widget: QObject, _value) -> None:
        assert widget is owner
        child.setObjectName("updated")
        calls.append("success")

    def failed(widget: QObject, _message: str) -> None:
        assert widget is owner
        child.setObjectName("failed")
        calls.append("failed")

    def finished(widget: QObject) -> None:
        assert widget is owner
        calls.append("finished")

    connect_lifecycle_safe(
        job,
        owner,
        succeeded=succeeded,
        failed=failed,
        finished=finished,
        orphaned_finished=lambda: calls.append("orphaned"),
    )
    shiboken6.delete(owner)
    assert not shiboken6.isValid(owner)

    job.signals.succeeded.emit({})
    job.signals.failed.emit("boom")
    job.signals.finished.emit()

    assert calls == ["orphaned"]


def test_same_day_legacy_maintenance_claim_is_recovered(tmp_path, monkeypatch):
    db = Database(tmp_path / "maintenance.db")
    with db.get_connection() as conn:
        today = str(conn.execute("SELECT date('now')").fetchone()[0])
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?)",
            ("maintenance.prune_claim_date", today),
        )

    calls: list[int] = []

    def prune():
        calls.append(1)
        return {"logs": 1}

    monkeypatch.setattr(db, "prune_old_data", prune)
    assert db.run_daily_maintenance() == {"logs": 1}
    assert calls == [1]
    assert db.get_setting("maintenance.last_prune_date") == today
    assert db.get_setting("maintenance.prune_claim_date") is None

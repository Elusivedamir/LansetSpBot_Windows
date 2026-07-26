from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

from PySide6.QtWidgets import QApplication

from storage.database import Database
from workers.queue_worker import QueueWorker


def _app():
    return QApplication.instance() or QApplication([])


def test_old_database_schema_is_repaired(tmp_path):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, status TEXT)")
    connection.execute("INSERT INTO tasks(status) VALUES('processing')")
    connection.commit()
    connection.close()

    database = Database(path)
    assert database.get_version() == Database.SCHEMA_VERSION
    with database.get_connection() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        channel_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(channels)")
        }
        row = connection.execute("SELECT * FROM tasks").fetchone()
    assert {
        "type",
        "payload",
        "retry_count",
        "max_retries",
        "created_at",
        "updated_at",
    } <= columns
    assert "last_comment_check_at" in channel_columns
    assert row["status"] == "running"


def test_atomic_claim_has_no_duplicates(tmp_path):
    path = tmp_path / "queue.db"
    database = Database(path)
    for index in range(80):
        database.insert_task("noop", {"index": index})

    claimed: list[int] = []
    lock = threading.Lock()

    def consume():
        local = Database(path)
        try:
            while True:
                task = local.claim_next_pending_task()
                if task is None:
                    return
                with lock:
                    claimed.append(task["id"])
                local.set_done(task["id"])
        finally:
            local.close_thread_connection()

    threads = [threading.Thread(target=consume) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(claimed) == 80
    assert len(set(claimed)) == 80
    assert all(task["status"] == "completed" for task in database.get_tasks(limit=100))


def test_stop_after_side_effect_does_not_requeue(tmp_path):
    app = _app()
    path = tmp_path / "effect.db"
    database = Database(path)
    effects: list[int] = []
    worker = None

    def factory():
        async def handler(task):
            await asyncio.sleep(0.05)
            effects.append(task["id"])
            worker.requestInterruption()

        return {"effect": handler}, None

    worker = QueueWorker(factory, database_path=path)
    task_id = database.insert_task("effect", {})
    worker.start()
    deadline = time.time() + 5
    while worker.isRunning() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)

    task = database.get_tasks(limit=1)[0]
    assert effects == [task_id]
    assert task["status"] == "completed"
    assert not worker.isRunning()


def test_newer_database_is_rejected(tmp_path):
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.commit()
    connection.close()
    import pytest
    from storage.database import DatabaseError

    with pytest.raises(DatabaseError, match="newer than supported"):
        Database(path)


def test_startup_recovery_does_not_duplicate_side_effect_tasks(tmp_path):
    path = tmp_path / "recovery.db"
    database = Database(path)
    safe_id = database.insert_task("sync_channels", {})
    send_id = database.insert_task("direct_message", {"chat_id": 1, "text": "x"})
    assert database.set_processing(safe_id)
    assert database.set_processing(send_id)
    assert database.reset_running_tasks() == 2
    rows = {task["id"]: task for task in database.get_tasks(limit=10)}
    assert rows[safe_id]["status"] == "pending"
    assert rows[send_id]["status"] == "failed"
    assert "uncertain external result" in rows[send_id]["error"]


def test_completion_write_failure_is_never_retried():
    app = _app()

    class FakeDB:
        def __init__(self):
            self.failed_calls = []
            self.requeued = False

        def set_done(self, task_id):
            raise RuntimeError("disk unavailable")

        def set_failed(self, task_id, message, retry=False):
            self.failed_calls.append((task_id, message, retry))
            return True

        def requeue_task(self, *args, **kwargs):
            self.requeued = True

    async def handler(task):
        return None

    worker = QueueWorker(lambda: {"effect": handler})
    fake = FakeDB()
    worker._db = fake
    worker._handlers = {"effect": handler}
    asyncio.run(
        worker._process_task(
            {
                "id": 9,
                "type": "effect",
                "payload": {},
                "retry_count": 0,
                "max_retries": 3,
            }
        )
    )
    app.processEvents()
    assert fake.requeued is False
    assert fake.failed_calls
    assert fake.failed_calls[0][2] is False
    assert "completion_state_uncertain" in fake.failed_calls[0][1]


def test_channel_upsert_preserves_internal_id(tmp_path):
    database = Database(tmp_path / "upsert.db")
    database.insert_channel({"channel_id": 77, "title": "old", "username": "a"})
    first = database.get_channel_by_id(77)
    database.insert_channel({"channel_id": 77, "title": "new", "username": "b"})
    second = database.get_channel_by_id(77)
    assert second["id"] == first["id"]
    assert second["title"] == "new"
    assert len(database.get_channels()) == 1


def test_task_input_validation_prevents_poisoned_queue_rows(tmp_path):
    import pytest
    from storage.database import DatabaseError

    database = Database(tmp_path / "validation.db")
    with pytest.raises(DatabaseError, match="JSON object"):
        database.insert_task("noop", [])
    with pytest.raises(DatabaseError, match="non-empty"):
        database.insert_task("", {})
    with pytest.raises(DatabaseError, match="max_retries"):
        database.insert_task("noop", {}, max_retries="broken")


def test_worker_reports_database_startup_failure(monkeypatch, tmp_path):
    app = _app()
    worker = QueueWorker(lambda: {})
    errors = []
    worker.worker_error.connect(errors.append)

    class BrokenDatabase:
        def __init__(self, _path, **_kwargs):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr("workers.queue_worker.Database", BrokenDatabase)
    worker.start()
    assert worker.wait(3000)
    app.processEvents()
    assert errors == ["database unavailable"]


def test_runtime_status_text_is_visible_and_cleared_on_completion(tmp_path):
    database = Database(tmp_path / "status.db")
    task_id = database.insert_task("noop", {})
    assert database.set_processing(task_id)
    assert database.update_task_status_text(task_id, "Ограничение Telegram: 5 сек")
    assert database.get_task(task_id)["status_text"] == "Ограничение Telegram: 5 сек"
    assert database.set_done(task_id)
    task = database.get_task(task_id)
    assert task["status_text"] is None
    assert task["error"] is None


def test_commenting_batch_rotates_and_excludes_unlinked_channels(tmp_path):
    database = Database(tmp_path / "rotation.db")
    for channel_id, title, linked_chat_id in (
        (1, "Alpha", 101),
        (2, "Bravo", 102),
        (3, "Charlie", 103),
        (4, "Delta", 104),
        (5, "No discussion", None),
    ):
        database.insert_channel(
            {
                "channel_id": channel_id,
                "title": title,
                "linked_chat_id": linked_chat_id,
            }
        )

    first = database.get_channels_for_commenting(2)
    assert [row["channel_id"] for row in first] == [1, 2]
    for row in first:
        assert database.mark_channel_comment_checked(row["channel_id"])

    second = database.get_channels_for_commenting(2)
    assert [row["channel_id"] for row in second] == [3, 4]
    assert all(row["linked_chat_id"] is not None for row in second)


def test_insert_or_ignore_reports_whether_row_was_inserted(tmp_path):
    database = Database(tmp_path / "duplicates.db")
    database.insert_channel({"channel_id": 10, "linked_chat_id": 11, "title": "A"})
    message = {
        "channel_id": 10,
        "message_id": 20,
        "text": "post",
        "date": None,
        "author_id": None,
    }
    assert database.insert_message(message) is True
    assert database.insert_message(message) is False

    comment = {
        "channel_id": 10,
        "linked_chat_id": 11,
        "post_message_id": 20,
        "comment_message_id": 30,
        "reply_to": 20,
        "author_id": 40,
        "text": "hello",
        "date": None,
    }
    assert database.insert_comment(comment) is True
    assert database.insert_comment(comment) is False

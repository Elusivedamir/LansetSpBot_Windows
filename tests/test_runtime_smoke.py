from __future__ import annotations

import time
import threading
import uuid
import asyncio
import pytest

from PySide6.QtWidgets import QApplication

from core.config import Config
from core.single_instance import SingleInstance
from core.composition import ApplicationContainer
from gui.app import MarlenApp
from services.api import ServiceAPI
from storage.database import Database
from workers.queue_worker import QueueWorker


def _app():
    return QApplication.instance() or QApplication([])


def test_invalid_environment_uses_safe_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("MARLEN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_ID", "not-an-int")
    monkeypatch.setenv("WORKERS", "broken")
    monkeypatch.setenv("MAX_RETRIES", "broken")
    monkeypatch.setenv("RATE_LIMIT", "broken")
    config = Config()
    assert config.telegram.api_id == 0
    assert config.queue.workers == 1
    assert config.queue.max_retries == 3
    assert config.rate_limit == 1.0


def test_non_finite_rate_limit_uses_safe_default(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT", "nan")
    assert Config().rate_limit == 1.0


def test_gui_container_and_noop_queue_smoke(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "custom.db"))
    app = _app()
    config = Config()
    container = ApplicationContainer(config)
    window = MarlenApp(container.adapter, container.queue_worker, config)
    assert container.database.path == container.queue_worker.database_path

    completed_ids: set[int] = set()
    all_completed = threading.Event()
    created_ids: set[int] = set()

    def record_completion(task_id: int) -> None:
        completed_ids.add(int(task_id))
        if created_ids and created_ids <= completed_ids:
            all_completed.set()

    container.queue_worker.task_completed.connect(record_completion)
    try:
        for index in range(4):
            task = container.api.create_task("noop", {"index": index})
            created_ids.add(int(task["id"]))
        assert container.api.start_queue()
        completion_deadline = time.monotonic() + 30
        while not all_completed.is_set() and time.monotonic() < completion_deadline:
            app.processEvents()
            all_completed.wait(0.01)
        assert created_ids <= completed_ids
        tasks = container.api.get_tasks(limit=10)
        assert all(task["status"] == "completed" for task in tasks)
    finally:
        container.api.stop_queue()
        stop_deadline = time.monotonic() + 15
        while container.queue_worker.isRunning() and time.monotonic() < stop_deadline:
            app.processEvents()
            time.sleep(0.01)
        window._tray.hide()
        container.shutdown(timeout_ms=15_000)
    assert not container.queue_worker.isRunning()


@pytest.mark.skip(
    reason=(
        "requires process-isolated packaging IPC proof; in-process Qt socket "
        "teardown can abort the shared pytest interpreter on Windows"
    )
)
def test_second_instance_activates_first():
    app = QApplication.instance() or QApplication([])
    name = "marlen.test." + uuid.uuid4().hex
    first = SingleInstance(name)
    second = SingleInstance(name)
    seen = []
    first.activation_requested.connect(lambda: seen.append(True))
    assert first.acquire() is True
    assert second.acquire() is False
    deadline = time.time() + 1
    while not seen and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    first.close()
    second.close()
    assert seen == [True]


def test_queue_restarts_when_task_arrives_during_cleanup(tmp_path):
    app = _app()
    path = tmp_path / "restart-race.db"
    database = Database(path)
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    async def noop(_task):
        await asyncio.sleep(0)

    async def cleanup():
        cleanup_started.set()
        while not release_cleanup.is_set():
            await asyncio.sleep(0.01)

    def factory():
        return {"noop": noop}, cleanup

    worker = QueueWorker(factory, database_path=path)
    # Этот тест проверяет перезапуск QueueWorker, а не миграцию секретов.
    # Production ApplicationContainer завершает миграцию до создания очереди.
    api = ServiceAPI(
        database,
        worker,
        secret_migration_verified=True,
    )
    second_completed = threading.Event()
    second_id = 0

    def record_completion(task_id: int) -> None:
        if second_id and int(task_id) == second_id:
            second_completed.set()

    worker.task_completed.connect(record_completion)
    try:
        first = api.create_task("noop", {})
        assert api.start_queue() is True
        assert cleanup_started.wait(timeout=30), "worker never entered cleanup"
        assert api.get_task(first["id"])["status"] == "completed"

        second = api.create_task("noop", {})
        second_id = int(second["id"])
        # This call happens while QThread.isRunning() is still true. It must arrange
        # an automatic restart instead of leaving the task pending forever.
        assert api.start_queue() is True
        release_cleanup.set()
        completion_deadline = time.monotonic() + 30
        while not second_completed.is_set() and time.monotonic() < completion_deadline:
            app.processEvents()
            second_completed.wait(0.01)
        assert second_completed.is_set()
        assert api.get_task(second["id"])["status"] == "completed"
    finally:
        release_cleanup.set()
        api.prepare_shutdown()
        worker.wait(15_000)

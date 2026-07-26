from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from PySide6.QtWidgets import QMessageBox

from gui.app import MarlenApp
from gui.gui_service_adapter import GUIServiceAdapter
from services.api import ServiceAPI
from storage.database import Database


def test_factory_reset_button_does_not_use_account_change_guard() -> None:
    source = Path("gui/views/account_view.py").read_text(encoding="utf-8")
    reset_block = source.split("    def reset_database(self) -> None:", 1)[1].split(
        "    def logout_account", 1
    )[0]

    assert "_ensure_account_change_allowed" not in reset_block
    assert "set_factory_reset_pending(True)" in reset_block
    assert "автоматически остановит кампании" in reset_block


def test_prepare_factory_reset_stops_campaign_and_blocks_new_queue_work(
    tmp_path,
) -> None:
    database = Database(tmp_path / "factory-reset-campaign.db")
    campaign = database.create_comment_campaign(
        ["comment"],
        daily_limit=1,
        slot_count=1,
        continuous=False,
    )
    worker = MagicMock()
    worker.isRunning.return_value = True
    api = ServiceAPI(database, queue_worker=worker)
    api._secret_migration_thread.join(timeout=5)

    result = api.prepare_factory_reset()

    assert result["comment_campaign_stopped"] is True
    assert database.get_comment_campaign(campaign["id"])["status"] == "stopped"
    assert api._shutdown_requested is True
    assert api.get_queue_unavailable_reason() == "shutdown_in_progress"
    worker.request_scope_cancellation.assert_called_with(
        "comment_campaign", campaign["id"]
    )
    worker.requestInterruption.assert_called()

    api.cancel_shutdown()
    api._campaign_timer.stop()
    api._delivery_recovery_timer.stop()


def test_queue_reports_shutdown_reason_and_recovers_after_abort(tmp_path) -> None:
    worker = MagicMock()
    worker.isRunning.return_value = False
    api = ServiceAPI(Database(tmp_path / "queue-recovery.db"), queue_worker=worker)
    api._secret_migration_thread.join(timeout=5)

    api.prepare_shutdown()
    assert api.start_queue() is False
    assert api.get_queue_unavailable_reason() == "shutdown_in_progress"

    api.cancel_shutdown()
    api._campaign_timer.stop()
    api._delivery_recovery_timer.stop()
    assert api.get_queue_unavailable_reason() is None
    assert api.start_queue() is True
    worker.start.assert_called_once_with()
    api.prepare_shutdown()


def test_queue_unavailable_message_explains_shutdown() -> None:
    adapter = GUIServiceAdapter(
        SimpleNamespace(get_queue_unavailable_reason=lambda: "shutdown_in_progress")
    )

    message = adapter.get_queue_unavailable_message()

    assert "завершает работу" in message
    assert "заводской сброс" in message


def test_tray_cannot_reopen_interactive_window_during_shutdown() -> None:
    tray = SimpleNamespace(
        isVisible=lambda: True,
        messages=[],
        showMessage=lambda *args: tray.messages.append(args),
    )
    fake = SimpleNamespace(_quitting=True, _tray=tray)

    MarlenApp.show_from_tray(fake)

    assert tray.messages
    assert "безопасное завершение" in tray.messages[0][1]


def test_aborted_factory_reset_restores_queue_and_reset_button(monkeypatch) -> None:
    events: list[object] = []
    adapter = SimpleNamespace(cancel_shutdown=lambda: events.append("cancel_shutdown"))
    account_view = SimpleNamespace(
        set_factory_reset_pending=lambda value: events.append(("reset_pending", value))
    )
    timer = SimpleNamespace(start=lambda: events.append("keep_alive"))
    tray = SimpleNamespace(
        isSystemTrayAvailable=lambda: False,
        show=lambda: events.append("tray_show"),
    )
    fake = SimpleNamespace(
        _factory_reset_pending=True,
        _quitting=True,
        adapter=adapter,
        account_view=account_view,
        _keep_alive_timer=timer,
        _tray=tray,
        setEnabled=lambda value: events.append(("enabled", value)),
        show_from_tray=lambda: events.append("show_from_tray"),
    )
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: events.append("dialog"))

    MarlenApp._abort_shutdown(fake, "failed", blockers=["background_calls:1"])

    assert fake._factory_reset_pending is False
    assert fake._quitting is False
    assert "cancel_shutdown" in events
    assert ("reset_pending", False) in events
    assert ("enabled", True) in events
    assert "show_from_tray" in events


def test_factory_reset_rolls_back_all_artifacts_after_late_delete_failure(
    tmp_path, monkeypatch
) -> None:
    from core import factory_reset
    from core.factory_reset import FactoryResetError, reset_local_state
    from core.paths import AppPaths

    root = tmp_path / "Marlen"
    paths = AppPaths(
        root=root,
        database=root / "marlen.db",
        logs=root / "logs",
        sessions=root / "sessions",
        backups=root / "backups",
    )
    for directory in (root, paths.logs, paths.sessions, paths.backups):
        directory.mkdir(parents=True, exist_ok=True)
    paths.database.write_text("database-before-reset", encoding="utf-8")
    secret_path = root / ".secrets.json"
    secret_path.write_text('{"telegram.api_hash":"secret"}', encoding="utf-8")
    (paths.sessions / "main.session").write_text("session", encoding="utf-8")
    (paths.backups / "main.bak").write_text("backup", encoding="utf-8")
    (paths.logs / "marlen.log").write_text("log", encoding="utf-8")

    original_clear = factory_reset._clear_directory

    def fail_after_file_deletions(directory: Path):
        if directory == paths.sessions:
            raise PermissionError("session directory is locked")
        return original_clear(directory)

    monkeypatch.setattr(factory_reset, "_clear_directory", fail_after_file_deletions)

    with pytest.raises(FactoryResetError, match="восстановлены из rollback-снимка"):
        reset_local_state(
            database_path=paths.database,
            paths=paths,
            secret_path=secret_path,
        )

    assert paths.database.read_text(encoding="utf-8") == "database-before-reset"
    assert secret_path.read_text(encoding="utf-8") == '{"telegram.api_hash":"secret"}'
    assert (paths.sessions / "main.session").read_text(encoding="utf-8") == "session"
    assert (paths.backups / "main.bak").read_text(encoding="utf-8") == "backup"
    assert (paths.logs / "marlen.log").read_text(encoding="utf-8") == "log"
    assert not list(root.glob(".factory-reset-rollback-*.tar"))
    assert not list(root.glob(".factory-reset-restore-*"))


def test_factory_reset_execution_failure_aborts_quit_and_restores_services(
    monkeypatch,
) -> None:
    events: list[object] = []

    def fail_reset():
        raise PermissionError("cannot delete sessions")

    fake = SimpleNamespace(
        _factory_reset_pending=True,
        _factory_reset_executor=fail_reset,
        _abort_shutdown=lambda message, blockers: events.append(
            ("abort", message, blockers)
        ),
        _finalize_quit=lambda: events.append("quit"),
    )
    monkeypatch.setattr("gui.app.log.exception", lambda *_args, **_kwargs: None)

    MarlenApp._complete_shutdown(fake)

    assert "quit" not in events
    assert events and events[0][0] == "abort"
    assert "продолжает работу" in events[0][1]
    assert "не возобновлены автоматически" in events[0][1]
    assert events[0][2] == ["factory_reset_failed"]


def test_forbidden_credential_backends_are_absent_from_tree() -> None:
    root = Path(__file__).resolve().parents[1]
    terms = {
        "".join(parts).lower()
        for parts in (
            ("key", "chain"),
            ("key", "ring"),
            ("Security", ".", "framework"),
            ("find", "-generic-password"),
            ("add", "-generic-password"),
            ("delete", "-generic-password"),
            ("security", " ", "find"),
            ("security", " ", "add"),
            ("security", " ", "delete"),
            ("Sec", "Key", "chain"),
            ("Sec", "ItemAdd"),
            ("Sec", "ItemCopyMatching"),
            ("Sec", "ItemDelete"),
        )
    }
    matches: list[tuple[str, str]] = []
    ignored_directory_names = {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "venv",
        "env",
        "site-packages",
    }
    for path in root.rglob("*"):
        if any(
            part in ignored_directory_names or part.startswith(".venv")
            for part in path.parts
        ):
            continue
        relative = str(path.relative_to(root))
        lowered_name = relative.lower()
        for term in terms:
            if term in lowered_name:
                matches.append((relative, term))
        if not path.is_file():
            continue
        try:
            lowered_text = path.read_text(encoding="utf-8").lower()
        except (OSError, UnicodeDecodeError):
            continue
        for term in terms:
            if term in lowered_text:
                matches.append((relative, term))

    assert matches == []


def test_aborted_shutdown_restarts_real_worker_and_processes_new_task(
    tmp_path,
) -> None:
    import asyncio
    import threading
    import time

    from PySide6.QtWidgets import QApplication

    from workers.queue_worker import QueueWorker

    app = QApplication.instance() or QApplication([])
    database = Database(tmp_path / "real-worker-recovery.db")
    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[int] = []

    async def handler(task):
        task_id = int(task["id"])
        calls.append(task_id)
        if len(calls) == 1:
            first_started.set()
            while not release_first.is_set():
                await asyncio.sleep(0.01)

    worker = QueueWorker(lambda: ({"noop": handler}, None), database_path=database.path)
    api = ServiceAPI(database, queue_worker=worker)
    api._secret_migration_thread.join(timeout=5)
    first = api.create_task("noop", {})
    assert api.start_queue() is True

    deadline = time.monotonic() + 5
    while not first_started.is_set() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert first_started.is_set()

    api.prepare_shutdown()
    api.cancel_shutdown()
    second = api.create_task("noop", {})
    assert api.start_queue() is True
    release_first.set()

    deadline = time.monotonic() + 8
    second_state = None
    while time.monotonic() < deadline:
        app.processEvents()
        second_state = api.get_task(second["id"])
        if second_state and second_state["status"] == "completed":
            break
        time.sleep(0.01)

    assert api.get_task(first["id"])["status"] == "completed"
    assert second_state is not None and second_state["status"] == "completed"
    assert calls == [first["id"], second["id"]]

    api.prepare_shutdown()
    if worker.isRunning():
        assert worker.stop(5_000)
    api._campaign_timer.stop()
    api._delivery_recovery_timer.stop()
    database.close_thread_connection()


def test_incomplete_reset_rollback_closes_instead_of_exposing_unsafe_gui(
    monkeypatch,
) -> None:
    from core.factory_reset import FactoryResetError

    events: list[object] = []

    def fail_reset():
        raise FactoryResetError("rollback failed", profile_restored=False)

    fake = SimpleNamespace(
        _factory_reset_pending=True,
        _factory_reset_executor=fail_reset,
        account_view=SimpleNamespace(
            set_factory_reset_pending=lambda value: events.append(
                ("reset_pending", value)
            )
        ),
        _abort_shutdown=lambda *_args, **_kwargs: events.append("abort"),
        _finalize_quit=lambda: events.append("quit"),
    )
    monkeypatch.setattr("gui.app.log.critical", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: events.append("dialog"))

    MarlenApp._complete_shutdown(fake)

    assert fake._factory_reset_pending is False
    assert ("reset_pending", False) in events
    assert "dialog" in events
    assert "quit" in events
    assert "abort" not in events


def test_worker_startup_failure_finishes_due_gui_task_and_allows_retry(
    tmp_path, monkeypatch
) -> None:
    import asyncio
    import time

    from PySide6.QtWidgets import QApplication

    import workers.queue_worker as queue_module
    from workers.queue_worker import QueueWorker

    app = QApplication.instance() or QApplication([])
    path = tmp_path / "worker-startup-failure.db"
    database = Database(path)

    async def noop(_task):
        await asyncio.sleep(0)

    worker = QueueWorker(lambda: ({"noop": noop}, None), database_path=path)
    api = ServiceAPI(database, queue_worker=worker)
    api._secret_migration_thread.join(timeout=5)
    first = api.create_task("noop", {})
    real_database_class = queue_module.Database

    class BrokenWorkerDatabase:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("worker SQLite unavailable")

    monkeypatch.setattr(queue_module, "Database", BrokenWorkerDatabase)
    assert api.start_queue() is True
    assert worker.wait(5_000)
    app.processEvents()

    failed = api.get_task(first["id"])
    assert failed is not None and failed["status"] == "failed"
    assert failed["error"] == "queue_worker_failed: worker SQLite unavailable"

    monkeypatch.setattr(queue_module, "Database", real_database_class)
    second = api.create_task("noop", {})
    assert api.start_queue() is True
    deadline = time.monotonic() + 5
    completed = None
    while time.monotonic() < deadline:
        app.processEvents()
        completed = api.get_task(second["id"])
        if completed and completed["status"] == "completed":
            break
        time.sleep(0.01)

    assert completed is not None and completed["status"] == "completed"
    api.prepare_shutdown()
    if worker.isRunning():
        assert worker.stop(5_000)
    api._campaign_timer.stop()
    api._delivery_recovery_timer.stop()
    database.close_thread_connection()

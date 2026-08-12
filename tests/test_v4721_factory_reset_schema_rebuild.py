from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from core.composition import ApplicationContainer
from core.factory_reset import FactoryResetError, reset_local_state
from core.paths import AppPaths
from gui.app import MarlenApp
from gui.main_window import MainWindow
from gui.views.commenting_view import CommentingView
from storage.database import Database


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        database=root / "marlen.db",
        logs=root / "logs",
        sessions=root / "sessions",
        backups=root / "backups",
    )


def _empty_profile_initializer(container) -> None:
    ApplicationContainer._initialize_empty_local_profile(container)


def test_factory_reset_recreates_complete_empty_schema_and_runtime_dirs(tmp_path):
    paths = _paths(tmp_path / "Marlen")
    paths.ensure()
    database = Database(paths.database)
    database.set_setting("telegram.account_id", 123)
    database.insert_log("INFO", "old log")
    campaign = database.create_comment_campaign(
        ["old comment"], daily_limit=1, slot_count=1, continuous=False
    )
    assert campaign
    database.close_thread_connection()
    (paths.sessions / "main.session").write_text("telegram", encoding="utf-8")
    (paths.backups / "old.bak").write_text("backup", encoding="utf-8")
    (paths.logs / "marlen.log").write_text("old file log", encoding="utf-8")
    secret = paths.root / ".secrets.json"
    secret.write_text('{"telegram.api_hash":"secret"}', encoding="utf-8")

    container = SimpleNamespace(
        config=SimpleNamespace(paths=paths, database_path=paths.database)
    )
    result = reset_local_state(
        database_path=paths.database,
        paths=paths,
        secret_path=secret,
        post_reset_initializer=lambda: _empty_profile_initializer(container),
    )

    assert result.removed_files >= 4
    assert paths.database.is_file()
    assert paths.logs.is_dir()
    assert paths.sessions.is_dir()
    assert paths.backups.is_dir()
    assert list(paths.sessions.iterdir()) == []
    assert list(paths.backups.iterdir()) == []
    assert not secret.exists()

    fresh = Database(paths.database)
    try:
        with fresh.get_connection() as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert ApplicationContainer._REQUIRED_PROFILE_TABLES <= tables
            for table in (
                "settings",
                "logs",
                "comment_campaigns",
                "join_campaigns",
                "comment_history",
                "tasks",
            ):
                count = int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                assert count == 0
            assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == (
                fresh.SCHEMA_VERSION
            )
            assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        fresh.close_thread_connection()


def test_post_reset_initializer_failure_restores_original_profile(tmp_path):
    paths = _paths(tmp_path / "Marlen")
    paths.ensure()
    original = Database(paths.database)
    original.set_setting("sentinel", "before-reset")
    original.close_thread_connection()
    (paths.sessions / "main.session").write_text("session", encoding="utf-8")
    (paths.logs / "marlen.log").write_text("before-reset", encoding="utf-8")

    def fail_after_partial_rebuild() -> None:
        paths.ensure()
        partial = Database(paths.database)
        partial.set_setting("partial", "new")
        partial.close_thread_connection()
        (paths.sessions / "partial.session").write_text("partial", encoding="utf-8")
        raise RuntimeError("schema verification failed")

    with pytest.raises(FactoryResetError, match="восстановлены из rollback-снимка"):
        reset_local_state(
            database_path=paths.database,
            paths=paths,
            post_reset_initializer=fail_after_partial_rebuild,
        )

    restored = Database(paths.database)
    try:
        assert restored.get_setting("sentinel", "") == "before-reset"
        assert restored.get_setting("partial", "") == ""
    finally:
        restored.close_thread_connection()
    assert (paths.sessions / "main.session").read_text(encoding="utf-8") == "session"
    assert not (paths.sessions / "partial.session").exists()
    assert (paths.logs / "marlen.log").read_text(encoding="utf-8") == "before-reset"
    assert not list(paths.root.glob(".factory-reset-rollback-*.tar"))


class _FakeTimer:
    def __init__(self, active: bool):
        self.active = active
        self.stops = 0
        self.starts = 0

    def isActive(self) -> bool:  # noqa: N802 - Qt-compatible test double
        return self.active

    def stop(self) -> None:
        self.active = False
        self.stops += 1

    def start(self) -> None:
        self.active = True
        self.starts += 1


def test_runtime_sqlite_timers_suspend_and_resume_without_losing_inactive_state():
    active = [_FakeTimer(True) for _ in range(10)]
    inactive = _FakeTimer(False)
    fake = SimpleNamespace(
        activity_panel=SimpleNamespace(
            timer=active[0], countdown_timer=active[1]
        ),
        warmup_view=SimpleNamespace(
            refresh_timer=active[2], journal_timer=active[3]
        ),
        channels_view=SimpleNamespace(
            timer=active[4], watcher=SimpleNamespace(timer=active[5])
        ),
        links_view=SimpleNamespace(watcher=SimpleNamespace(timer=active[6])),
        commenting_view=SimpleNamespace(
            refresh_timer=active[7],
            countdown_timer=active[8],
            limit_save_timer=inactive,
        ),
        audience_parser_view=SimpleNamespace(
            watcher=SimpleNamespace(timer=active[9])
        ),
        _suspended_runtime_timers=[],
    )
    fake._runtime_refresh_timers = lambda: MainWindow._runtime_refresh_timers(fake)

    MainWindow.suspend_runtime_updates(fake)

    assert all(not timer.active and timer.stops == 1 for timer in active)
    assert inactive.stops == 0

    MainWindow.resume_runtime_updates(fake)

    assert all(timer.active and timer.starts == 1 for timer in active)
    assert inactive.active is False and inactive.starts == 0


def test_factory_reset_stops_gui_refresh_before_deleting_profile(monkeypatch):
    events: list[str] = []
    fake = SimpleNamespace(
        _factory_reset_pending=True,
        _factory_reset_executor=lambda: (
            events.append("executor")
            or SimpleNamespace(removed_files=1, removed_directories=1)
        ),
        suspend_runtime_updates=lambda: events.append("suspend"),
        _finalize_quit=lambda: events.append("quit"),
    )
    monkeypatch.setattr(
        QMessageBox, "information", lambda *_args, **_kwargs: events.append("dialog")
    )

    MarlenApp._complete_shutdown(fake)

    assert events == ["suspend", "executor", "dialog", "quit"]


def test_comment_refresh_timer_contains_database_error(monkeypatch):
    labels: list[tuple[str, str]] = []

    def fail_refresh() -> None:
        raise RuntimeError("no such table: comment_campaigns")

    fake = SimpleNamespace(
        _refresh_campaign=fail_refresh,
        _last_refresh_error="",
        status=SimpleNamespace(setText=lambda value: labels.append(("status", value))),
        next_label=SimpleNamespace(
            setText=lambda value: labels.append(("next", value))
        ),
        # _handle_refresh_error only rewrites the countdown label while no next
        # check time is known.
        _next_check_at=None,
    )
    fake._handle_refresh_error = lambda message: CommentingView._handle_refresh_error(
        fake, message
    )
    warnings: list[object] = []
    monkeypatch.setattr(
        "gui.views.commenting_parts.campaign.log.warning",
        lambda *args, **kwargs: warnings.append((args, kwargs)),
    )

    CommentingView.refresh_campaign(fake)
    CommentingView.refresh_campaign(fake)

    assert fake._last_refresh_error.startswith("RuntimeError:")
    assert len(warnings) == 1
    assert ("status", "Не удалось обновить статус кампании") in labels


def test_exception_hook_shows_each_error_once_and_suppresses_shutdown_dialogs(
    monkeypatch,
):
    from main import _install_exception_hook

    app = QApplication.instance() or QApplication([])
    app.setProperty("marlen_shutdown_in_progress", False)
    dialogs: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, _title, message: dialogs.append(str(message)),
    )
    monkeypatch.setattr(logging.getLogger("main"), "critical", lambda *_a, **_k: None)
    previous = sys.excepthook
    try:
        _install_exception_hook()
        error = RuntimeError("same periodic failure")
        sys.excepthook(RuntimeError, error, None)
        sys.excepthook(RuntimeError, error, None)
        app.setProperty("marlen_shutdown_in_progress", True)
        sys.excepthook(ValueError, ValueError("shutdown failure"), None)
    finally:
        sys.excepthook = previous
        app.setProperty("marlen_shutdown_in_progress", False)

    assert len(dialogs) == 1
    assert "same periodic failure" in dialogs[0]


def test_factory_reset_handoff_never_deletes_profile_in_live_gui_process(
    monkeypatch,
):
    import main as main_module

    events: list[str] = []
    container = SimpleNamespace(
        queue_worker=SimpleNamespace(isRunning=lambda: False),
        api=SimpleNamespace(is_secret_migration_running=lambda: False),
    )

    monkeypatch.setattr(
        main_module,
        "launch_detached_factory_reset",
        lambda *, parent_pid: (
            events.append(f"launch:{parent_pid}")
            or SimpleNamespace(scheduled=True, helper_pid=42)
        ),
    )
    monkeypatch.setattr(main_module.os, "getpid", lambda: 1234)

    main_module._prepare_factory_reset_execution(container)
    result = main_module._execute_factory_reset(container)

    assert result.scheduled is True
    assert result.helper_pid == 42
    assert events == ["launch:1234"]


def test_factory_reset_complete_shutdown_schedules_destructive_phase_async():
    events: list[str] = []
    fake = SimpleNamespace(
        _factory_reset_pending=True,
        _factory_reset_executor=lambda: events.append("executor"),
        _factory_reset_preparer=lambda: events.append("prepare"),
        suspend_runtime_updates=lambda: events.append("suspend"),
        _set_shutdown_progress_text=lambda _value: events.append("progress"),
        _start_factory_reset_async=lambda: events.append("async"),
    )

    MarlenApp._complete_shutdown(fake)

    assert events == ["suspend", "progress", "prepare", "async"]
    assert "executor" not in events


def test_setup_logging_replaces_all_stale_matching_handlers(monkeypatch, tmp_path):
    from logging.handlers import RotatingFileHandler

    from core import logging_setup

    paths = _paths(tmp_path / "Marlen")
    monkeypatch.setattr(logging_setup, "APP_PATHS", paths)
    paths.ensure()
    log_file = paths.logs / "marlen.log"
    root = logging.getLogger()
    stale_handlers = [
        RotatingFileHandler(log_file, maxBytes=10, backupCount=9, encoding="utf-8")
        for _ in range(2)
    ]
    for handler in stale_handlers:
        root.addHandler(handler)

    try:
        logging_setup.setup_logging()
        matches = [
            handler
            for handler in root.handlers
            if isinstance(handler, RotatingFileHandler)
            and getattr(handler, "baseFilename", "") == str(log_file)
        ]
        assert len(matches) == 1
        assert matches[0] not in stale_handlers
        assert matches[0].maxBytes == logging_setup.FILE_LOG_SEGMENT_BYTES
        assert matches[0].backupCount == logging_setup.FILE_LOG_BACKUP_COUNT
    finally:
        for handler in list(root.handlers):
            if isinstance(handler, RotatingFileHandler) and getattr(
                handler, "baseFilename", ""
            ) == str(log_file):
                root.removeHandler(handler)
                handler.close()

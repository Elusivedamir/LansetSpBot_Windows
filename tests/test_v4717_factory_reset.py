from __future__ import annotations

from pathlib import Path

import pytest

from core.factory_reset import reset_local_state
from core.paths import AppPaths


def make_paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        database=root / "marlen.db",
        logs=root / "logs",
        sessions=root / "sessions",
        backups=root / "backups",
    )


def test_factory_reset_removes_database_history_sessions_secrets_and_logs(tmp_path):
    root = tmp_path / "Marlen"
    paths = make_paths(root)
    for directory in (root, paths.logs, paths.sessions, paths.backups):
        directory.mkdir(parents=True, exist_ok=True)

    database = paths.database
    for target in (
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        Path(f"{database}-journal"),
    ):
        target.write_text("state", encoding="utf-8")
    (paths.sessions / "main.session").write_text("telegram", encoding="utf-8")
    (paths.sessions / "main.session.corrupt.old").write_text("old", encoding="utf-8")
    (paths.backups / "main.session.backups").mkdir()
    (paths.backups / "main.session.backups" / "one.bak").write_text(
        "backup", encoding="utf-8"
    )
    (paths.logs / "marlen.log").write_text("log", encoding="utf-8")
    (root / ".instance-test.lock").write_text("lock", encoding="utf-8")
    secret_path = root / ".secrets.json"
    secret_path.write_text('{"telegram.api_hash":"secret"}', encoding="utf-8")

    result = reset_local_state(
        database_path=database,
        paths=paths,
        secret_path=secret_path,
    )

    assert not database.exists()
    assert not Path(f"{database}-wal").exists()
    assert not paths.sessions.exists()
    assert not paths.backups.exists()
    assert not paths.logs.exists()
    assert not secret_path.exists()
    # The active single-instance lock remains until the caller finishes reset.
    assert (root / ".instance-test.lock").exists()
    assert root.exists()
    assert result.removed_files >= 7
    assert result.removed_directories >= 3


def test_factory_reset_preserves_unknown_files_in_overridden_root(tmp_path):
    root = tmp_path / "custom-user-selected-directory"
    paths = make_paths(root)
    for directory in (root, paths.logs, paths.sessions, paths.backups):
        directory.mkdir(parents=True, exist_ok=True)
    unknown = root / "do-not-delete.txt"
    unknown.write_text("personal file", encoding="utf-8")
    paths.database.write_text("db", encoding="utf-8")

    reset_local_state(database_path=paths.database, paths=paths)

    assert unknown.read_text(encoding="utf-8") == "personal file"
    assert root.exists()
    assert not paths.database.exists()


def test_factory_reset_uses_local_paths_without_secret_store_calls(tmp_path):
    root = tmp_path / "Marlen"
    paths = make_paths(root)
    root.mkdir(parents=True)
    paths.database.write_text("db", encoding="utf-8")
    secret_path = root / ".secrets.json"
    secret_path.write_text("{}", encoding="utf-8")

    result = reset_local_state(
        database_path=paths.database,
        paths=paths,
        secret_path=secret_path,
    )

    assert result.removed_files == 2
    assert not paths.database.exists()
    assert not secret_path.exists()


def test_account_view_declares_red_factory_reset_button_and_signal():
    source = "\n".join(
        (
            Path("gui/views/account_view.py").read_text(encoding="utf-8"),
            Path("gui/views/account_parts/settings.py").read_text(encoding="utf-8"),
        )
    )
    theme = Path("gui/theme.py").read_text(encoding="utf-8")

    assert "factory_reset_requested = Signal()" in source
    assert 'QPushButton("Сбросить базу данных")' in source
    assert 'setObjectName("dangerButton")' in source
    assert "24-часовые ограничения" in source
    assert "только локальные файлы профиля" in source
    assert 'confirmation.strip().upper() != "СБРОСИТЬ"' in source
    assert "QPushButton#dangerButton" in theme


def test_app_routes_factory_reset_through_graceful_shutdown():
    source = Path("gui/app.py").read_text(encoding="utf-8")
    main_source = Path("main.py").read_text(encoding="utf-8")

    assert "self.account_view.factory_reset_requested.connect(" in source
    assert "self._factory_reset_pending = True" in source
    assert "self.quit_application()" in source
    assert "factory_reset_executor=lambda:" in main_source
    assert "launch_detached_factory_reset(parent_pid=os.getpid())" in main_source
    assert "--factory-reset-helper" in Path("core/factory_reset_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "container.shutdown(timeout_ms=60_000)" in main_source
    assert "self._complete_shutdown()" in source


def test_container_factory_reset_delegates_after_shutdown(monkeypatch, tmp_path):
    from core import composition
    from core.composition import ApplicationContainer
    from core.factory_reset import FactoryResetResult

    expected = FactoryResetResult(removed_files=7, removed_directories=3)
    captured = {}

    def fake_reset_local_state(**kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(composition, "reset_local_state", fake_reset_local_state)
    database = type(
        "DatabaseStub",
        (),
        {
            "closed": False,
            "close_thread_connection": lambda self: setattr(self, "closed", True),
        },
    )()
    queue_worker = type("QueueStub", (), {"isRunning": lambda self: False})()
    paths = make_paths(tmp_path / "Marlen")
    config = type("ConfigStub", (), {"database_path": paths.database, "paths": paths})()
    local_secret_path = paths.root / ".secrets.json"
    secret_store = type("SecretStoreStub", (), {"fallback_path": local_secret_path})()
    container = type(
        "ContainerStub",
        (),
        {
            "queue_worker": queue_worker,
            "database": database,
            "config": config,
            "secret_store": secret_store,
        },
    )()

    result = ApplicationContainer.factory_reset_local_data(container)

    assert result == expected
    assert database.closed is True
    initializer = captured.pop("post_reset_initializer")
    assert callable(initializer)
    assert captured == {
        "database_path": paths.database,
        "paths": paths,
        "secret_path": local_secret_path,
    }


def test_container_factory_reset_rejects_running_queue():
    from core.composition import ApplicationContainer

    container = type(
        "ContainerStub",
        (),
        {"queue_worker": type("QueueStub", (), {"isRunning": lambda self: True})()},
    )()
    with pytest.raises(RuntimeError, match="работает очередь"):
        ApplicationContainer.factory_reset_local_data(container)


def test_container_factory_reset_does_not_query_migration_state(monkeypatch, tmp_path):
    from core import composition
    from core.composition import ApplicationContainer
    from core.factory_reset import FactoryResetResult

    monkeypatch.setattr(
        composition,
        "reset_local_state",
        lambda **_kwargs: FactoryResetResult(removed_files=0, removed_directories=0),
    )
    paths = make_paths(tmp_path / "Marlen")
    container = type(
        "ContainerStub",
        (),
        {
            "queue_worker": type("QueueStub", (), {"isRunning": lambda self: False})(),
            "api": type(
                "ApiStub",
                (),
                {
                    "is_secret_migration_running": lambda self: (_ for _ in ()).throw(
                        AssertionError("must not be called")
                    )
                },
            )(),
            "database": type(
                "DatabaseStub", (), {"close_thread_connection": lambda self: None}
            )(),
            "config": type(
                "ConfigStub", (), {"database_path": paths.database, "paths": paths}
            )(),
            "secret_store": type(
                "SecretStoreStub",
                (),
                {"fallback_path": paths.root / ".secrets.json"},
            )(),
        },
    )()

    ApplicationContainer.factory_reset_local_data(container)


def test_aborted_shutdown_clears_pending_factory_reset():
    source = Path("gui/app.py").read_text(encoding="utf-8")

    assert "reset_was_pending = self._factory_reset_pending" in source
    assert "self._factory_reset_pending = False" in source
    assert "self.account_view.set_factory_reset_pending(False)" in source

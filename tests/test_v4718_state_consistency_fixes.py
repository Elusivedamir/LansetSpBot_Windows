from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QMessageBox

from core.factory_reset import FactoryResetError, reset_local_state
from core.paths import AppPaths
from core.secret_store import SecretStore
from core.config import Config
from core.composition import ApplicationContainer
from gui.app import MarlenApp
from services.api import ServiceAPI
from storage.database import Database


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        database=root / "marlen.db",
        logs=root / "logs",
        sessions=root / "sessions",
        backups=root / "backups",
    )


def test_factory_reset_refuses_external_database_before_local_deletion(tmp_path):
    root = tmp_path / "Marlen"
    paths = _paths(root)
    root.mkdir()
    external = tmp_path / "important.db"
    external.write_bytes(b"do-not-delete")
    secret_path = root / ".secrets.json"
    secret_path.write_text("{}", encoding="utf-8")

    with pytest.raises(FactoryResetError, match="Внешние базы никогда не удаляются"):
        reset_local_state(
            database_path=external,
            paths=paths,
            secret_path=secret_path,
        )

    assert external.read_bytes() == b"do-not-delete"
    assert secret_path.exists()


def test_factory_reset_rejects_symlink_escape(tmp_path):
    root = tmp_path / "Marlen"
    root.mkdir()
    paths = _paths(root)
    external = tmp_path / "external.db"
    external.write_bytes(b"important")
    link = root / "marlen.db"
    try:
        link.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(FactoryResetError, match="Внешние базы никогда не удаляются"):
        reset_local_state(database_path=link, paths=paths)

    assert external.read_bytes() == b"important"
    assert link.is_symlink()


def test_factory_reset_rejects_external_secret_file_before_deletion(tmp_path):
    root = tmp_path / "Marlen"
    paths = _paths(root)
    root.mkdir()
    paths.database.write_bytes(b"db")
    external_secret = tmp_path / "secrets.json"
    external_secret.write_text("{}", encoding="utf-8")

    with pytest.raises(FactoryResetError, match="вне каталога данных приложения"):
        reset_local_state(
            database_path=paths.database,
            paths=paths,
            secret_path=external_secret,
        )

    assert paths.database.exists()
    assert external_secret.exists()


def test_save_settings_restores_local_secret_file_when_sqlite_write_fails(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "settings.db")
    store = SecretStore(tmp_path / "secrets.json")
    store.set("telegram.api_hash", "old-hash")
    store.set("telegram.proxy_password", "old-proxy")
    database.set_settings({"telegram.api_id": "111", "telegram.phone": "+100"})
    api = ServiceAPI(database, queue_worker=None, secret_store=store)
    api._secret_migration_thread.join(timeout=5)

    original = database.set_settings

    def failing_set_settings(values):
        if "telegram.api_id" in values:
            raise RuntimeError("disk full")
        return original(values)

    monkeypatch.setattr(database, "set_settings", failing_set_settings)

    with pytest.raises(RuntimeError, match="disk full"):
        api.save_settings(
            {
                "telegram.api_id": "222",
                "telegram.phone": "+200",
                "telegram.api_hash": "new-hash",
                "telegram.proxy_password": "new-proxy",
            }
        )

    assert store.get_strict_optional("telegram.api_hash") == "old-hash"
    assert store.get_strict_optional("telegram.proxy_password") == "old-proxy"
    assert database.get_setting("telegram.api_id") == "111"
    # telegram.phone is one of SECRET_SETTING_KEYS, so the startup migration
    # moved it out of SQLite into the encrypted store; the rollback must have
    # preserved the previous value there.
    assert store.get_strict_optional("telegram.phone") == "+100"
    assert database.get_setting("telegram.phone") is None
    api.prepare_shutdown()
    database.close_thread_connection()


def test_account_actions_remain_locked_until_identity_is_persisted(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "account-lock.db"))
    app = _app()
    config = Config()
    container = ApplicationContainer(config)
    window = MarlenApp(container.adapter, container.queue_worker, config)
    view = window.account_view
    QThreadPool.globalInstance().waitForDone(5_000)
    app.processEvents()

    started = threading.Event()
    release = threading.Event()
    original_save = container.adapter.save_settings

    def delayed_save(values):
        started.set()
        if not release.wait(timeout=5):
            raise RuntimeError("test save timeout")
        return original_save(values)

    monkeypatch.setattr(container.adapter, "save_settings", delayed_save)
    warnings: list[tuple] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))

    view._adding_account = True
    view._pending_session_name = "pending_0123456789abcdef0123456789abcdef"
    view._auth_settings_snapshot = {
        "telegram.api_id": "12345",
        "telegram.api_hash": "test-api-hash",
        "telegram.phone": "+10000000000",
        "telegram.proxy_enabled": False,
    }

    view._authorized({"id": 222, "name": "New", "username": "new"})
    assert started.wait(timeout=5)
    assert view._account_blocking_jobs
    assert view.connect_button.isEnabled() is False
    assert view.logout_button.isEnabled() is False
    assert view.reset_database_button.isEnabled() is False
    assert view._ensure_account_change_allowed() is False
    assert "сохранения состояния" in str(warnings[-1][2]).lower()

    release.set()
    assert QThreadPool.globalInstance().waitForDone(5_000)
    deadline = time.monotonic() + 2
    while view._account_blocking_jobs and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert not view._account_blocking_jobs
    assert container.adapter.get_settings("telegram.")["telegram.account_id"] == "222"
    assert view.connect_button.isEnabled() is True
    assert view.logout_button.isEnabled() is True

    window._tray.hide()
    window.deleteLater()
    app.processEvents()
    container.shutdown()

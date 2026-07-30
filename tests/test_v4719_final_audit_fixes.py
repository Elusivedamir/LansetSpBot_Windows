from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from core.account_state import (
    AccountStateError,
    has_pending_account_state,
    persist_account_state,
    reconcile_pending_account_state,
)
from core.composition import ApplicationContainer
from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from core.factory_reset import FactoryResetError, reset_local_state
from core.paths import AppPaths
from core.rate_limiter import RateLimiter
from gui.views.account_view import AccountView
from services.api import ServiceAPI
from services.import_service import ImportService, ImportValidationError
from services.telegram_service import TelegramService
from storage.database import Database
from tests.test_composition_resilience import (
    _Telegram,
    _Worker,
    _comment_database,
    _handlers,
)


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


class _Secrets:
    def __init__(self, root: Path) -> None:
        self.fallback_path = root / ".secrets.json"
        self.values = {
            "telegram.api_hash": "hash",
            "telegram.proxy_password": "proxy",
        }

    def get_strict_optional(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str | None) -> None:
        if value in (None, ""):
            self.values.pop(key, None)
        else:
            self.values[key] = str(value)

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def test_account_transition_journal_recovers_after_sqlite_failure(
    tmp_path, monkeypatch
):
    path = tmp_path / "marlen.db"
    original = Database.set_settings

    def fail(_self, _values):
        raise RuntimeError("disk full")

    monkeypatch.setattr(Database, "set_settings", fail)
    values = {
        "telegram.account_id": "222",
        "telegram.account_name": "New Account",
        "telegram.account_username": "new",
        "telegram.authorized": "1",
    }
    with pytest.raises(AccountStateError, match="журнал восстановления"):
        persist_account_state(path, values)
    assert has_pending_account_state(path) is True

    monkeypatch.setattr(Database, "set_settings", original)
    database = Database(path)
    assert reconcile_pending_account_state(database) is True
    assert database.get_setting("telegram.account_id") == "222"
    assert has_pending_account_state(path) is False
    database.close_thread_connection()


@pytest.mark.asyncio
async def test_telegram_connect_rejects_session_account_mismatch():
    class Client:
        connected = False

        def is_connected(self):
            return self.connected

        async def connect(self):
            self.connected = True

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=222)

        async def disconnect(self):
            self.connected = False

    service = object.__new__(TelegramService)
    service.settings = SimpleNamespace(configured=True, expected_account_id=111)
    service.client = Client()
    service._connected = False
    service._last_authorization_check = 0.0
    service.backup_session = MagicMock()

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.connect()
    assert raised.value.code == "account_state_mismatch"
    assert service.client.is_connected() is False
    service.backup_session.assert_not_called()


def test_legacy_migration_aborts_on_strict_secret_read_failure(tmp_path):
    database = Database(tmp_path / "legacy.db")
    database.set_setting("telegram.api_hash", "OLD")

    class LockedStore:
        def get_strict_optional(self, _key):
            raise RuntimeError("local secret store unavailable")

        def set(self, *_args):
            raise AssertionError("must not overwrite")

    ServiceAPI._migrate_legacy_secrets(
        database,
        LockedStore(),
        {"telegram.api_hash"},
        __import__("threading").RLock(),
    )
    assert database.get_setting("telegram.api_hash") == "OLD"
    database.close_thread_connection()


def test_factory_reset_reports_file_phase_failure_without_storage_callbacks(
    tmp_path, monkeypatch
):
    from core import factory_reset

    root = tmp_path / "Marlen"
    paths = _paths(root)
    root.mkdir()
    paths.database.write_text("db", encoding="utf-8")
    secret_path = root / ".secrets.json"
    secret_path.write_text("{}", encoding="utf-8")
    original_unlink = factory_reset._unlink

    def fail_database(path: Path) -> bool:
        if path == paths.database:
            raise PermissionError("locked")
        return original_unlink(path)

    monkeypatch.setattr(factory_reset, "_unlink", fail_database)
    with pytest.raises(FactoryResetError, match="Сброс выполнен не полностью"):
        reset_local_state(
            database_path=paths.database,
            paths=paths,
            secret_path=secret_path,
        )
    assert paths.database.exists()


@pytest.mark.asyncio
async def test_permanently_inaccessible_channel_is_consumed_for_cooldown(monkeypatch):
    database = _comment_database()
    telegram = _Telegram()
    telegram.latest_error = NonRetryableTelegramError("private", code="channel_private")
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, database, telegram)

    await handlers["auto_comment_slot"](
        {"id": 901, "payload": {"campaign_id": 1, "slot_id": 901}}
    )

    database.mark_channel_comment_checked.assert_called_once_with(10)
    assert database.finish_comment_slot.call_args.kwargs["status"] == "skipped"


@pytest.mark.asyncio
async def test_process_rate_floor_applies_to_faster_client(monkeypatch):
    RateLimiter._reset_for_tests()
    monkeypatch.setattr(RateLimiter, "MIN_INTERVAL_SECONDS", 0.01)
    RateLimiter._process_interval_floor = 0.01
    RateLimiter.configure_process_interval(0.08)
    slow = RateLimiter(0.08)
    fast = RateLimiter(0.01)

    starts: list[float] = []
    async with slow.request_slot():
        starts.append(time.monotonic())
    async with fast.request_slot():
        starts.append(time.monotonic())

    assert starts[1] - starts[0] >= 0.07
    RateLimiter._reset_for_tests()


def test_due_task_type_excludes_future_not_before(tmp_path):
    database = Database(tmp_path / "tasks.db")
    task_id = database.insert_task(
        "auto_comment_slot", {"campaign_id": 1, "slot_id": 1}
    )
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET not_before=datetime('now', '+1 hour') WHERE id=?",
            (task_id,),
        )
    assert database.has_pending_task_type("auto_comment_slot") is True
    assert database.has_due_pending_task_type("auto_comment_slot") is False
    database.close_thread_connection()


def test_disconnected_ui_restores_offline_indicator(tmp_path):
    app = _app()
    adapter = MagicMock()
    adapter.get_settings.return_value = {}
    adapter.close_thread_connection.return_value = None
    adapter.set_auth_in_progress.return_value = None
    config = SimpleNamespace(
        telegram=SimpleNamespace(session_dir=tmp_path),
        database_path=tmp_path / "marlen.db",
    )
    view = AccountView(adapter, config)
    QThreadPool.globalInstance().waitForDone(5_000)
    app.processEvents()

    view._set_status_dot(True)
    assert view.status_dot.objectName() == "statusDotOnline"
    view._apply_disconnected_account()
    assert view.status_dot.objectName() == "statusDotOffline"
    view.deleteLater()
    app.processEvents()


def test_comment_import_requires_stable_message_id():
    with pytest.raises(ImportValidationError, match="comment_message_id"):
        ImportService.validate_rows(
            "comments",
            [
                {
                    "channel_id": 1,
                    "linked_chat_id": 2,
                    "post_message_id": 3,
                }
            ],
        )


def test_pending_account_transition_blocks_queue_start(tmp_path):
    database = Database(tmp_path / "queue.db")
    marker = database.path.parent / ".account-state-pending.json"
    marker.write_text("{}", encoding="utf-8")
    worker = MagicMock()
    worker.isRunning.return_value = False
    _app()
    api = ServiceAPI(database, queue_worker=worker, secret_store=_Secrets(tmp_path))
    api._secret_migration_thread.join(timeout=5)
    assert api.start_queue() is False
    worker.start.assert_not_called()
    api.prepare_shutdown()
    database.close_thread_connection()


@pytest.mark.asyncio
async def test_worker_defers_telegram_tasks_when_secret_store_is_unavailable(tmp_path):
    database = MagicMock()
    database.get_settings.return_value = {
        "telegram.api_id": "123",
        "telegram.account_id": "222",
    }

    class LockedSecretStore:
        def get_strict_optional(self, _key):
            raise RuntimeError("local secret store unavailable")

    container = object.__new__(ApplicationContainer)
    container.config = SimpleNamespace(
        telegram=SimpleNamespace(
            api_id=0,
            api_hash="",
            phone=None,
            session_dir=tmp_path,
        ),
        rate_limit=1.0,
    )
    container.queue_worker = _Worker(database)
    container.secret_store = LockedSecretStore()

    handlers, cleanup = container._create_worker_handlers()
    assert callable(cleanup)
    try:
        await handlers["noop"]({"id": 1})
        with pytest.raises(DeferredTelegramError) as raised:
            await handlers["sync_channels"](
                {"id": 2, "payload": {"account_id": 222}}
            )
        assert raised.value.code == "secret_store_unavailable"
        assert raised.value.retry_after == 120
    finally:
        cleanup()


def test_get_settings_does_not_mask_locked_secret_store(tmp_path):
    database = Database(tmp_path / "settings.db")

    class LockedSecretStore:
        def get_strict_optional(self, _key):
            raise RuntimeError("local secret store unavailable")

    _app()
    api = ServiceAPI(database, queue_worker=None, secret_store=LockedSecretStore())
    api._secret_migration_thread.join(timeout=5)
    with pytest.raises(RuntimeError, match="local secret store unavailable"):
        api.get_settings("telegram.")
    api.prepare_shutdown()
    database.close_thread_connection()

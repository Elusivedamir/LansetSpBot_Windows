from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication
from telethon import TelegramClient
from telethon.errors import AuthKeyUnregisteredError
from telethon.tl.functions.messages import SendMessageRequest
from telethon.tl.types import InputPeerSelf

from core.exceptions import (
    DeferredTelegramError,
    NonRetryableTelegramError,
    TelegramOperationError,
)
from core.campaign_schedule import from_db_time
from core.paths import AppPaths
import core.logging_setup as logging_setup
from gui.app import MarlenApp
from gui.main_window import MainWindow
from services.api_parts.task_queue import TaskQueueAPIMixin
from services.api import ServiceAPI
from services.comment_service import CommentService
from services.paced_telegram_client import PacedTelegramClient
from services.telegram_service import TelegramService
from storage.database import Database
from tests.test_composition_resilience import _Telegram, _comment_database, _handlers


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_native_qt_quit_is_routed_through_coordinated_shutdown(monkeypatch) -> None:
    app = _qt_app()
    window = MarlenApp.__new__(MarlenApp)
    window._allow_qt_quit = False
    window.quit_application = MagicMock()

    assert window.eventFilter(app, QEvent(QEvent.Type.Quit)) is True
    window.quit_application.assert_called_once_with()

    window._allow_qt_quit = True
    monkeypatch.setattr(
        MainWindow, "eventFilter", lambda _self, _watched, _event: False
    )
    assert window.eventFilter(app, QEvent(QEvent.Type.Quit)) is False


class _Limiter:
    @asynccontextmanager
    async def request_slot(self):
        yield

    async def acquire(self) -> None:
        return None


def _transport_service() -> TelegramService:
    service = object.__new__(TelegramService)
    service.client = SimpleNamespace(_marlen_request_pacing=False)
    service.limiter = _Limiter()
    service._connected = True
    service._status_callback = None
    service._interruption_requested = lambda: False
    return service


@pytest.mark.asyncio
async def test_mutating_preflight_failure_never_becomes_unknown_delivery() -> None:
    service = _transport_service()
    calls = 0

    async def fail_preflight() -> None:
        raise TelegramOperationError("connect failed")

    async def operation() -> None:
        nonlocal calls
        calls += 1

    service.ensure_connected = fail_preflight
    service.disconnect = lambda: asyncio.sleep(0)
    service.safe_sleep = lambda _seconds: asyncio.sleep(0, result=True)

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.execute(operation, retry_network=False)

    assert raised.value.code == "network_unavailable"
    assert calls == 0


@pytest.mark.asyncio
async def test_mutating_post_dispatch_network_loss_stays_uncertain() -> None:
    service = _transport_service()
    service.ensure_connected = lambda: asyncio.sleep(0)
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise ConnectionError("response lost")

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.execute(operation, retry_network=False)

    assert raised.value.code == "delivery_result_unknown"
    assert calls == 1


@pytest.mark.asyncio
async def test_interrupt_before_mtproto_dispatch_is_a_safe_deferral() -> None:
    class PacedClient:
        _marlen_request_pacing = True

        @contextmanager
        def observe_requests(self, observer):
            self.observer = observer
            try:
                yield
            finally:
                self.observer = None

    service = _transport_service()
    service.client = PacedClient()
    service.ensure_connected = lambda: asyncio.sleep(0)
    checks = 0
    method_started = False

    def interrupted() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    service._interruption_requested = interrupted

    async def operation() -> None:
        nonlocal method_started
        method_started = True
        await asyncio.sleep(10)

    with pytest.raises(DeferredTelegramError) as raised:
        await service.execute(operation, retry_network=False)

    assert raised.value.code == "shutdown_before_dispatch"
    assert method_started is True


@pytest.mark.asyncio
async def test_paced_client_reports_the_real_mtproto_dispatch_boundary(
    monkeypatch,
) -> None:
    client = object.__new__(PacedTelegramClient)
    client._marlen_request_limiter = _Limiter()
    client._marlen_request_timeout = 1.0
    observed = []

    async def fake_call(_self, request, **_kwargs):
        return request

    monkeypatch.setattr(TelegramClient, "__call__", fake_call)
    request = SendMessageRequest(peer=InputPeerSelf(), message="hello", random_id=1)

    with client.observe_requests(observed.append):
        assert (
            await client._call_one(request, ordered=False, flood_sleep_threshold=0)
            is request
        )

    assert observed == [request]


@pytest.mark.asyncio
async def test_revoked_session_rpc_is_mapped_to_authorization_required() -> None:
    service = _transport_service()
    service.ensure_connected = lambda: asyncio.sleep(0)
    disconnected = False

    async def disconnect() -> None:
        nonlocal disconnected
        disconnected = True

    service.disconnect = disconnect

    async def operation() -> None:
        raise AuthKeyUnregisteredError(request=None)

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.execute(operation)

    assert raised.value.code == "authorization_required"
    assert disconnected is True


@pytest.mark.asyncio
async def test_periodic_auth_check_uses_fresh_identity_rpc() -> None:
    class Client:
        def __init__(self) -> None:
            self.connected = True
            self.get_me_calls = 0

        def is_connected(self) -> bool:
            return self.connected

        async def get_me(self):
            self.get_me_calls += 1
            return None

        async def disconnect(self) -> None:
            self.connected = False

    service = _transport_service()
    service.client = Client()
    service.settings = SimpleNamespace(expected_account_id=123)
    # Make the probe deterministically due even in a freshly started CI
    # container whose monotonic clock is still below the recheck interval.
    service._last_authorization_check = (
        time.monotonic() - service.AUTHORIZATION_RECHECK_SECONDS - 1.0
    )

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.ensure_connected()

    assert raised.value.code == "authorization_required"
    assert service.client.get_me_calls == 1
    assert service.client.connected is False


@pytest.mark.asyncio
async def test_pre_dispatch_network_failure_releases_direct_delivery(tmp_path) -> None:
    database = Database(tmp_path / "pre-dispatch.db")
    task_id = database.insert_task("direct_message", {"chat_id": 99, "text": "hello"})

    class Telegram:
        async def send_message(self, *_args, **_kwargs):
            raise NonRetryableTelegramError("offline", code="network_unavailable")

    comments = CommentService(Telegram(), db=database)
    with pytest.raises(NonRetryableTelegramError):
        await comments.send_direct_message(99, "hello", task_id=task_id)

    assert database.get_direct_message_delivery(task_id) is None


@pytest.mark.asyncio
async def test_post_dispatch_comment_cancellation_pauses_campaign(monkeypatch) -> None:
    database = _comment_database()
    handlers, _cleanup, comments, _worker = _handlers(
        monkeypatch, database, _Telegram()
    )
    comments.error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await handlers["auto_comment_slot"](
            {"id": 61, "payload": {"campaign_id": 1, "slot_id": 61}}
        )

    database.pause_campaign_for_safety.assert_called_once()
    assert database.finish_comment_slot.call_args.kwargs["status"] == "uncertain"


def test_direct_group_crash_recovery_blocks_a_second_delivery(tmp_path) -> None:
    database = Database(tmp_path / "direct-crash.db")
    group_id = -100000007001
    database.insert_channel(
        {
            "channel_id": group_id,
            "linked_chat_id": group_id,
            "title": "Direct group",
            "target_kind": "group",
            "comment_mode": "direct_group",
        }
    )
    start = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    campaign = database.create_comment_campaign(
        ["hello"],
        daily_limit=2,
        slot_count=2,
        continuous=False,
        start_at=start,
        rng=random.Random(1),
    )
    campaign_id = int(campaign["id"])
    slot = database.get_comment_schedule(campaign_id, limit=10)[0]
    queued = database.queue_due_comment_slot(
        now=from_db_time(slot["scheduled_at"]) + timedelta(seconds=1)
    )
    assert queued is not None
    task_id = int(queued["task_id"])
    assert database.set_processing(task_id)
    assert database.mark_comment_slot_running(slot["id"], task_id)
    assert database.bind_comment_slot_target(
        slot["id"], task_id, channel_id=group_id, post_id=None
    )
    assert database.reserve_direct_message_delivery(task_id, group_id, "hello")

    assert database.reset_running_tasks() == 1
    assert database.reconcile_comment_schedule() == 1

    recovered_slot = database.get_comment_schedule(campaign_id, limit=10)[0]
    recovered_campaign = database.get_comment_campaign(campaign_id)
    recovered_channel = database.get_channel_by_id(group_id)
    recovered_delivery = database.get_direct_message_delivery(task_id)
    assert recovered_slot["status"] == "uncertain"
    assert recovered_campaign["status"] == "paused"
    assert recovered_channel["last_comment_check_at"] is not None
    assert recovered_delivery["status"] == "uncertain"

    second_task = database.insert_task(
        "direct_message", {"chat_id": group_id, "text": "hello"}
    )
    assert (
        database.reserve_direct_message_delivery(second_task, group_id, "hello")
        is False
    )


def test_join_crash_recovery_counts_unknown_result_against_daily_limit(
    tmp_path,
) -> None:
    database = Database(tmp_path / "join-crash.db")
    account_id = 9
    dialog_ids = []
    for index in range(2):
        dialog_id = database.upsert_saved_dialog(
            {
                "peer_id": 8000 + index,
                "username": f"join_target_{index}",
                "title": f"Join target {index}",
                "kind": "channel",
            },
            account_id=account_id,
        )
        database.set_saved_dialog_membership(dialog_id, account_id, "left")
        dialog_ids.append(dialog_id)

    with database.get_connection() as connection:
        connection.executemany(
            """INSERT INTO join_events(
                   linked_chat_id, joined_at, result, account_id)
               VALUES(?, datetime('now','-10 minutes'), 'joined', ?)""",
            ((50_000 + index, account_id) for index in range(39)),
        )

    campaign = database.create_join_campaign(
        account_id,
        max_per_hour=40,
        start_at=datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc),
        rng=random.Random(2),
    )
    campaign_id = int(campaign["id"])
    slot = database.get_join_schedule(campaign_id, limit=10)[0]
    queued = database.queue_due_join_slot(
        now=from_db_time(slot["scheduled_at"]) + timedelta(seconds=1)
    )
    assert queued is not None
    task_id = int(queued["task_id"])
    assert database.set_processing(task_id)
    assert database.mark_join_slot_running(slot["id"], task_id)

    assert database.reset_running_tasks() == 1
    assert database.reconcile_join_schedule() == 1

    recovered_slot = database.get_join_schedule(campaign_id, limit=10)[0]
    recovered_campaign = database.get_join_campaign(campaign_id)
    dialogs = {row["id"]: row for row in database.get_saved_dialogs(account_id)}
    assert recovered_slot["status"] == "uncertain"
    assert recovered_campaign["status"] == "paused"
    assert dialogs[slot["saved_dialog_id"]]["membership_status"] == "uncertain"

    guard = database.get_join_guard(
        max_joins=40,
        min_interval_seconds=0,
        account_id=account_id,
    )
    assert guard["joined_count"] == 39
    assert guard["uncertain_count"] == 1
    assert guard["effective_count"] == 40
    assert guard["allowed"] is False

    target = dialogs[slot["saved_dialog_id"]]
    database.upsert_saved_dialog(
        {
            "peer_id": target["peer_id"],
            "username": target["username"],
            "title": target["title"],
            "kind": target["kind"],
        },
        account_id=account_id,
    )
    resolved_guard = database.get_join_guard(
        max_joins=40,
        min_interval_seconds=0,
        account_id=account_id,
    )
    assert resolved_guard["joined_count"] == 40
    assert resolved_guard["uncertain_count"] == 0
    assert resolved_guard["effective_count"] == 40
    assert resolved_guard["allowed"] is False


def test_full_dialog_sync_resolves_absent_uncertain_membership_as_left(
    tmp_path,
) -> None:
    database = Database(tmp_path / "join-sync.db")
    account_id = 11
    dialog_id = database.upsert_saved_dialog(
        {
            "peer_id": 9001,
            "username": "missing_after_sync",
            "title": "Missing after sync",
            "kind": "channel",
        },
        account_id=account_id,
    )
    database.set_saved_dialog_membership(
        dialog_id, account_id, "uncertain", "unknown join result"
    )

    assert (
        database.mark_unseen_saved_dialogs_left(
            account_id=account_id, seen_dialog_ids=[]
        )
        == 1
    )
    dialog = database.get_saved_dialogs(account_id)[0]
    assert dialog["membership_status"] == "left"
    assert dialog["membership_error"] is None


def test_resuming_last_inflight_comment_completion_closes_campaign(tmp_path) -> None:
    database = Database(tmp_path / "pause-last-slot.db")
    channel_id = 12001
    database.insert_channel(
        {
            "channel_id": channel_id,
            "linked_chat_id": 22001,
            "title": "Channel",
            "username": "channel_12001",
        }
    )
    campaign = database.create_comment_campaign(
        ["hello"],
        daily_limit=1,
        slot_count=1,
        continuous=False,
        start_at=datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc),
        rng=random.Random(3),
    )
    campaign_id = int(campaign["id"])
    slot = database.get_comment_schedule(campaign_id, limit=10)[0]
    queued = database.queue_due_comment_slot(
        now=from_db_time(slot["scheduled_at"]) + timedelta(seconds=1)
    )
    assert queued is not None
    task_id = int(queued["task_id"])
    assert database.set_processing(task_id)
    assert database.mark_comment_slot_running(slot["id"], task_id)
    assert database.pause_comment_campaign(campaign_id)
    assert database.finalize_comment_slot_outcome(
        task_id,
        slot["id"],
        status="sent",
        result="Sent",
        channel_id=channel_id,
        post_id=301,
        selected_text="hello",
        sent=True,
        consume_channel=True,
    )
    assert all(
        row["status"] not in {"pending", "queued", "running"}
        for row in database.get_comment_schedule(campaign_id, limit=10)
    )
    assert database.get_comment_campaign(campaign_id)["status"] == "paused"

    assert database.resume_comment_campaign(campaign_id)
    assert database.get_comment_campaign(campaign_id)["status"] == "completed"


def test_generic_cancel_rejects_campaign_tasks(tmp_path) -> None:
    database = Database(tmp_path / "cancel-guard.db")
    api = SimpleNamespace(database=database)

    for task_type in ("auto_comment_slot", "join_saved_slot"):
        task_id = database.insert_task(task_type, {"campaign_id": 1, "slot_id": 1})
        assert TaskQueueAPIMixin.cancel_task(api, task_id) is False
        assert database.get_task(task_id)["status"] == "pending"


def test_legacy_cancelled_comment_slot_remains_resumable(tmp_path) -> None:
    database = Database(tmp_path / "cancel-recovery.db")
    campaign = database.create_comment_campaign(
        ["hello"],
        daily_limit=1,
        slot_count=1,
        continuous=False,
        start_at=datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc),
        rng=random.Random(4),
    )
    campaign_id = int(campaign["id"])
    slot = database.get_comment_schedule(campaign_id, limit=10)[0]
    queued = database.queue_due_comment_slot(
        now=from_db_time(slot["scheduled_at"]) + timedelta(seconds=1)
    )
    assert queued is not None
    assert database.cancel_task(queued["task_id"])

    assert database.reconcile_comment_schedule() == 1
    recovered = database.get_comment_schedule(campaign_id, limit=10)[0]
    recovered_campaign = database.get_comment_campaign(campaign_id)
    assert recovered["status"] == "pending"
    assert recovered["task_id"] is None
    assert recovered["executed_at"] is None
    assert recovered_campaign["attempted_count"] == 0
    assert recovered_campaign["status"] == "paused"


def test_legacy_cancelled_join_slot_remains_resumable(tmp_path) -> None:
    database = Database(tmp_path / "cancel-join-recovery.db")
    account_id = 17
    dialog_id = database.upsert_saved_dialog(
        {
            "peer_id": 17001,
            "username": "cancel_join_target",
            "title": "Cancel join target",
            "kind": "channel",
        },
        account_id=account_id,
    )
    database.set_saved_dialog_membership(dialog_id, account_id, "left")
    campaign = database.create_join_campaign(
        account_id,
        start_at=datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc),
    )
    campaign_id = int(campaign["id"])
    slot = database.get_join_schedule(campaign_id, limit=10)[0]
    queued = database.queue_due_join_slot(
        now=from_db_time(slot["scheduled_at"]) + timedelta(seconds=1)
    )
    assert queued is not None
    assert database.cancel_task(queued["task_id"])

    assert database.reconcile_join_schedule() == 1
    recovered = database.get_join_schedule(campaign_id, limit=10)[0]
    recovered_campaign = database.get_join_campaign(campaign_id)
    assert recovered["status"] == "pending"
    assert recovered["task_id"] is None
    assert recovered["executed_at"] is None
    assert recovered_campaign["attempted_count"] == 0
    assert recovered_campaign["status"] == "paused"


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, asyncio.CancelledError])
def test_database_rolls_back_base_exceptions(tmp_path, error_type) -> None:
    path = tmp_path / f"rollback-{error_type.__name__}.db"
    database = Database(path)
    database.set_setting("audit.rollback", "old")

    with pytest.raises(error_type):
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE settings SET value='new' WHERE key='audit.rollback'"
            )
            raise error_type()

    database.close_thread_connection()
    reopened = Database(path)
    assert reopened.get_setting("audit.rollback") == "old"


def test_failed_secret_migration_blocks_scheduler_until_retry_succeeds(
    tmp_path,
    monkeypatch,
) -> None:
    _qt_app()
    database = Database(tmp_path / "secret-migration.db")
    database.set_setting("telegram.api_hash", "legacy-secret")

    # The production scheduler is now multi-account. Verify the public
    # orchestration seam rather than the obsolete root _campaign_tick_once method.
    scheduler_tick = MagicMock()
    monkeypatch.setattr(
        "services.multiaccount_scheduler.run_multiaccount_campaign_tick",
        scheduler_tick,
    )

    class LockedThenAvailableStore:
        def __init__(self) -> None:
            self.locked = True
            self.values = {}

        def get_strict_optional(self, key):
            if self.locked:
                raise RuntimeError("local secret store unavailable")
            return self.values.get(key)

        def get(self, key, default=""):
            return self.values.get(key, default)

        def set(self, key, value) -> None:
            if self.locked:
                raise RuntimeError("local secret store unavailable")
            self.values[key] = value

        def delete(self, key) -> None:
            self.values.pop(key, None)

    store = LockedThenAvailableStore()
    worker = MagicMock()
    worker.isRunning.return_value = False
    api = ServiceAPI(database, queue_worker=worker, secret_store=store)
    api._secret_migration_thread.join(timeout=5)
    assert api._secret_migration_required.is_set()
    assert database.get_setting("telegram.api_hash") == "legacy-secret"

    api._secret_migration_retry_at = float("inf")
    api._campaign_tick()
    scheduler_tick.assert_not_called()
    assert api.start_queue() is False
    worker.start.assert_not_called()

    store.locked = False
    api._secret_migration_retry_at = 0.0
    api._campaign_tick()
    api._secret_migration_thread.join(timeout=5)
    assert api._secret_migration_required.is_set() is False
    assert database.get_setting("telegram.api_hash", "") == ""
    assert store.values["telegram.api_hash"] == "legacy-secret"

    api._campaign_tick()
    scheduler_tick.assert_called_once_with(api)
    api.prepare_shutdown()


def test_unavailable_secret_store_without_legacy_plaintext_does_not_block_scheduler(
    tmp_path,
) -> None:
    _qt_app()
    database = Database(tmp_path / "no-legacy-secret.db")

    class LockedStore:
        def get_strict_optional(self, _key):
            raise RuntimeError("local secret store unavailable")

        def get(self, _key, default=""):
            return default

    api = ServiceAPI(database, queue_worker=None, secret_store=LockedStore())
    api._secret_migration_thread.join(timeout=5)
    assert api._secret_migration_required.is_set() is False
    api.prepare_shutdown()


def test_secret_settings_restore_local_file_on_base_exception(
    tmp_path, monkeypatch
) -> None:
    _qt_app()
    database = Database(tmp_path / "secret-compensation.db")

    class Store:
        def __init__(self) -> None:
            self.values = {"telegram.api_hash": "old-secret"}

        def get_strict_optional(self, key):
            return self.values.get(key)

        def get(self, key, default=""):
            return self.values.get(key, default)

        def set(self, key, value) -> None:
            self.values[key] = value

        def delete(self, key) -> None:
            self.values.pop(key, None)

    store = Store()
    api = ServiceAPI(database, queue_worker=None, secret_store=store)
    api._secret_migration_thread.join(timeout=5)

    def interrupt(_values) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(database, "set_settings", interrupt)
    with pytest.raises(KeyboardInterrupt):
        api.save_settings({"telegram.api_hash": "new-secret", "telegram.api_id": "123"})

    assert store.values["telegram.api_hash"] == "old-secret"
    assert database.get_setting("telegram.api_id", "") == ""
    api.prepare_shutdown()


def test_live_oversized_log_records_respect_two_mib_budget(
    tmp_path, monkeypatch
) -> None:
    paths = AppPaths(
        root=tmp_path,
        database=tmp_path / "marlen.db",
        logs=tmp_path / "logs",
        sessions=tmp_path / "sessions",
        backups=tmp_path / "backups",
    )
    monkeypatch.setattr(logging_setup, "APP_PATHS", paths)
    root = logging.getLogger()
    handler = None
    try:
        logger = logging_setup.setup_logging()
        handler = next(
            item
            for item in root.handlers
            if getattr(item, "baseFilename", "") == str(paths.logs / "marlen.log")
        )
        logger.error("X" * (3 * 1024 * 1024))
        for _index in range(40):
            logger.error("Ж" * 40_000)
        handler.flush()

        files = list(paths.logs.glob("marlen.log*"))
        assert files
        assert all(
            item.stat().st_size <= logging_setup.FILE_LOG_SEGMENT_BYTES
            for item in files
        )
        assert sum(item.stat().st_size for item in files) <= (
            logging_setup.FILE_LOG_TOTAL_BYTES
        )
        rendered = "".join(item.read_text(encoding="utf-8") for item in files)
        assert "log record truncated" in rendered
    finally:
        if handler is not None:
            root.removeHandler(handler)
            handler.close()


def test_account_state_journal_fsyncs_parent_after_atomic_replace(
    tmp_path, monkeypatch
) -> None:
    import core.account_state as account_state

    fsync_parent = MagicMock()
    monkeypatch.setattr(account_state, "_fsync_parent", fsync_parent)
    path = tmp_path / account_state.ACCOUNT_STATE_FILENAME
    account_state._write_pending(
        path,
        {
            "telegram.account_id": "1",
            "telegram.account_name": "User",
            "telegram.account_username": "user",
            "telegram.authorized": "1",
        },
    )

    assert path.is_file()
    fsync_parent.assert_called_once_with(path)

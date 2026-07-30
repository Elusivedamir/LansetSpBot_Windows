from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from telethon import functions, utils
from telethon.tl.types import PeerChannel

from core.exceptions import TelegramOperationError
from core.version import APP_NAME, __version__
from services.telegram_service import TelegramService
from tests.conftest import open_project_database


@pytest.mark.asyncio
async def test_raw_linked_channel_id_is_marked_before_telethon_use() -> None:
    get_entity_method = object()
    service = object.__new__(TelegramService)
    service.client = SimpleNamespace(get_entity=get_entity_method)
    entity = SimpleNamespace(id=100)
    full = SimpleNamespace(full_chat=SimpleNamespace(linked_chat_id=200))

    executed_args = []

    async def execute(method, *args, **kwargs):
        del kwargs
        executed_args.append((method, args))
        return entity if method is get_entity_method else full

    service.execute = execute

    linked_id = await service.get_linked_chat(100)

    assert linked_id == utils.get_peer_id(PeerChannel(200))
    assert linked_id < 0
    assert len(executed_args) == 1
    request = executed_args[0][1][0]
    assert isinstance(request, functions.channels.GetFullChannelRequest)
    assert request.channel == utils.get_peer_id(PeerChannel(100))


def test_legacy_positive_channel_id_is_normalized() -> None:
    assert TelegramService._channel_peer_reference(123) == utils.get_peer_id(
        PeerChannel(123)
    )
    marked = utils.get_peer_id(PeerChannel(456))
    assert TelegramService._channel_peer_reference(marked) == marked
    assert (
        TelegramService._channel_peer_reference("public_username") == "public_username"
    )


def test_corrupt_session_without_backup_is_quarantined(tmp_path: Path) -> None:
    source = tmp_path / "main.session"
    source.write_bytes(b"not a sqlite database")

    TelegramService._prepare_session_file(source)

    assert not source.exists()
    quarantined = list(tmp_path.glob("main.session.corrupt.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"not a sqlite database"


@pytest.mark.asyncio
async def test_failed_connect_closes_partial_telethon_transport() -> None:
    class Client:
        def __init__(self) -> None:
            self.connected = False
            self.disconnect_calls = 0

        def is_connected(self) -> bool:
            return self.connected

        async def connect(self) -> None:
            self.connected = True

        async def is_user_authorized(self) -> bool:
            raise asyncio.TimeoutError

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            self.connected = False

    service = object.__new__(TelegramService)
    service.settings = SimpleNamespace(configured=True)
    service.client = Client()
    service._connected = False
    service.backup_session = lambda: None

    with pytest.raises(TelegramOperationError, match="timed out"):
        await service.connect()

    assert service.client.disconnect_calls == 1
    assert service.client.is_connected() is False
    assert service._connected is False


def test_public_name_is_short_and_versioned() -> None:
    assert __version__ == "4.8.0"
    assert APP_NAME == "LansetSpBot"
    assert " " not in APP_NAME


@pytest.mark.asyncio
async def test_auth_worker_repairs_session_before_client_construction(
    tmp_path: Path, monkeypatch
) -> None:
    import gui.auth_worker as auth_module
    from gui.auth_worker import TelegramAuthWorker

    order: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            order.append("client")
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        def is_connected(self) -> bool:
            return self.connected

        async def is_user_authorized(self) -> bool:
            return True

        async def get_me(self):
            return SimpleNamespace(
                id=1, first_name="Test", last_name="", username="test", phone="1"
            )

        async def disconnect(self) -> None:
            self.connected = False

    def prepare(path: Path) -> None:
        assert path == tmp_path / "main.session"
        order.append("prepare")

    monkeypatch.setattr(auth_module, "TelegramClient", Client)
    monkeypatch.setattr(TelegramService, "_prepare_session_file", staticmethod(prepare))
    worker = TelegramAuthWorker(
        mode="request_code",
        settings={
            "telegram.api_id": "123",
            "telegram.api_hash": "hash",
            "telegram.phone": "+10000000000",
        },
        session_dir=tmp_path,
    )

    await worker._run()

    assert order == ["prepare", "client"]


def test_corrupt_session_quarantines_sqlite_sidecars(tmp_path):
    source = tmp_path / "main.session"
    source.write_bytes(b"not sqlite")
    wal = Path(f"{source}-wal")
    shm = Path(f"{source}-shm")
    wal.write_bytes(b"stale wal")
    shm.write_bytes(b"stale shm")

    TelegramService._prepare_session_file(source)

    assert not source.exists()
    assert not wal.exists()
    assert not shm.exists()
    assert list(tmp_path.glob("main.session.corrupt.*"))
    assert list(tmp_path.glob("main.session-wal.corrupt.*"))
    assert list(tmp_path.glob("main.session-shm.corrupt.*"))


def test_legacy_secret_migration_closes_its_thread_connection():
    from services.api import ServiceAPI

    database = MagicMock()
    database.get_setting.return_value = "legacy-secret"
    secret_store = MagicMock()
    secret_store.get.return_value = ""

    ServiceAPI._migrate_legacy_secrets(
        database,
        secret_store,
        {"telegram.api_hash"},
        threading.RLock(),
    )

    secret_store.set.assert_called_once_with("account.1.telegram.api_hash", "legacy-secret")
    database.delete_setting.assert_called_once_with("telegram.api_hash")
    database.close_thread_connection.assert_called_once_with()


def test_logout_purge_removes_live_session_and_recovery_backups(tmp_path):
    source = tmp_path / "main.session"
    source.write_bytes(b"revoked session")
    Path(f"{source}-wal").write_bytes(b"wal")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "main.session.20260101T000000Z.bak").write_bytes(b"auth backup")
    (tmp_path / "main.session.corrupt.old").write_bytes(b"diagnostic")

    TelegramService.purge_session_artifacts(source)

    assert not source.exists()
    assert not Path(f"{source}-wal").exists()
    assert not backup_dir.exists()
    assert not list(tmp_path.glob("backups.revoked.*"))
    assert not list(tmp_path.glob("main.session.corrupt.*"))


def test_background_call_closes_worker_thread_resources_on_success() -> None:
    from gui.background import BackgroundCall

    events: list[object] = []
    job = BackgroundCall(
        lambda: "ok",
        cleanup=lambda: events.append("cleanup"),
    )
    job.signals.succeeded.connect(lambda value: events.append(("success", value)))
    job.signals.finished.connect(lambda: events.append("finished"))

    job.run()

    assert events == ["cleanup", ("success", "ok"), "finished"]


def test_background_call_closes_worker_thread_resources_on_failure() -> None:
    from gui.background import BackgroundCall

    events: list[object] = []

    def fail() -> None:
        raise RuntimeError("boom")

    job = BackgroundCall(fail, cleanup=lambda: events.append("cleanup"))
    job.signals.failed.connect(lambda value: events.append(("failure", value)))
    job.signals.finished.connect(lambda: events.append("finished"))

    job.run()

    assert events == [
        "cleanup",
        ("failure", "RuntimeError: boom"),
        "finished",
    ]


@pytest.mark.asyncio
async def test_logout_does_not_require_phone_number(
    tmp_path: Path, monkeypatch
) -> None:
    import gui.auth_worker as auth_module
    from gui.auth_worker import TelegramAuthWorker

    events: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            events.append("client")
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def is_user_authorized(self) -> bool:
            return False

        def is_connected(self) -> bool:
            return self.connected

        async def disconnect(self) -> None:
            self.connected = False

    monkeypatch.setattr(auth_module, "TelegramClient", Client)
    monkeypatch.setattr(
        TelegramService,
        "_prepare_session_file",
        staticmethod(lambda _path: events.append("prepare")),
    )
    monkeypatch.setattr(
        TelegramService,
        "purge_session_artifacts",
        staticmethod(lambda _path: events.append("purge")),
    )
    worker = TelegramAuthWorker(
        mode="logout",
        settings={
            "telegram.api_id": "123",
            "telegram.api_hash": "hash",
            "telegram.phone": "",
        },
        session_dir=tmp_path,
    )

    await worker._run()

    assert events == ["prepare", "client", "purge"]


def test_shutdown_waits_for_marlen_background_calls_only() -> None:
    from gui.app import MarlenApp
    from gui.background import BackgroundCall

    window = MarlenApp.__new__(MarlenApp)
    window.queue_worker = None
    window.account_view = SimpleNamespace(is_authentication_running=lambda: False)
    window.adapter = SimpleNamespace(is_secret_migration_running=lambda: False)

    job = BackgroundCall(lambda: None)
    assert window._background_threads_running() is True

    job.run()
    assert window._background_threads_running() is False


def test_finalized_delivery_cannot_be_promoted_from_a_non_reserved_state(tmp_path):
    """A sent receipt may only be written from an active 'sending' reservation.

    Regression for an audit finding: the ``-> sent`` ledger transition matched on
    (account, channel, post, campaign) without a status predicate, while both
    sibling transitions required one.  A row already resolved as failed or
    uncertain could therefore be rewritten as a confirmed success.
    """


    from storage.database import Database
    from storage.db_common import DatabaseError

    database = Database(tmp_path / "ledger.db")
    connection = open_project_database(str(tmp_path / "ledger.db"))
    connection.execute(
        "INSERT INTO channels(account_id, channel_id, title) VALUES(1, 100, 'ch')"
    )
    connection.execute(
        """INSERT INTO comment_campaigns(
               account_id, status, started_at, ends_at, daily_limit,
               cadence_seconds, continuous, comments_json)
           VALUES(1, 'running', datetime('now'), datetime('now', '+1 day'),
                  10, 2160.0, 0, '[]')"""
    )
    connection.commit()

    payload = {
        "account_id": 1,
        "campaign_id": 1,
        "channel_id": 100,
        "post_message_id": 5,
        "comment_message_id": 777,
        "text": "hello",
        "linked_chat_id": 200,
        "action_type": "campaign_comment",
    }

    assert database.reserve_comment_delivery(
        100, 5, linked_chat_id=200, text="hello", account_id=1, campaign_id=1,
        action_type="campaign_comment",
    )
    for resolved in ("uncertain", "failed", "sent"):
        connection.execute(
            "UPDATE comment_deliveries SET status=? WHERE account_id=1 AND channel_id=100 AND post_id=5",
            (resolved,),
        )
        connection.commit()
        with pytest.raises(DatabaseError):
            database.finalize_comment_delivery(dict(payload))
        assert (
            connection.execute(
                "SELECT status FROM comment_deliveries WHERE account_id=1 AND channel_id=100 AND post_id=5"
            ).fetchone()[0]
            == resolved
        )

    connection.execute(
        "UPDATE comment_deliveries SET status='sending' WHERE account_id=1 AND channel_id=100 AND post_id=5"
    )
    connection.commit()
    assert database.finalize_comment_delivery(dict(payload)) is True
    assert (
        connection.execute(
            "SELECT status FROM comment_deliveries WHERE account_id=1 AND channel_id=100 AND post_id=5"
        ).fetchone()[0]
        == "sent"
    )
    connection.close()
    database.close_thread_connection()

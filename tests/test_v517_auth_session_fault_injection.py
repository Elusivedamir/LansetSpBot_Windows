from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from telethon.errors import RPCError, UnauthorizedError

from core.exceptions import NonRetryableTelegramError, TelegramOperationError
from core.crypto_vault import VaultIntegrityError
from services.encrypted_telethon_session import (
    EncryptedSQLiteSession,
    TelegramSessionEncryptionError,
)
from services.telegram_service import TelegramService
from storage.database import Database


ACCOUNT_ID = 77
OTHER_ACCOUNT_ID = 88


class _ConnectClient:
    def __init__(
        self,
        *,
        get_me_result=None,
        get_me_error: BaseException | None = None,
        forbid_cached_auth_flag: bool = False,
    ) -> None:
        self.connected = False
        self.get_me_result = get_me_result
        self.get_me_error = get_me_error
        self.forbid_cached_auth_flag = forbid_cached_auth_flag
        self.get_me_calls = 0
        self.authorization_flag_calls = 0
        self.connect_calls = 0
        self.disconnect_calls = 0

    def is_connected(self) -> bool:
        return bool(self.connected)

    async def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    async def is_user_authorized(self) -> bool:
        self.authorization_flag_calls += 1
        if self.forbid_cached_auth_flag:
            raise AssertionError(
                "runtime connect must not use Telethon's cached auth flag"
            )
        return False

    async def get_me(self):
        self.get_me_calls += 1
        if self.get_me_error is not None:
            raise self.get_me_error
        return self.get_me_result


class _UnauthorizedProbe(UnauthorizedError):
    def __init__(self) -> None:
        Exception.__init__(self, "unauthorized")


def _prepare_database(tmp_path) -> Database:
    db = Database(tmp_path / "auth-fault.db")
    db.register_telegram_account(
        telegram_account_id=ACCOUNT_ID,
        session_name=f"account_{ACCOUNT_ID}",
        display_name="Auth Test",
        username="auth_test",
        authorized=True,
    )
    db.select_telegram_account(ACCOUNT_ID)
    row = db.get_telegram_account(ACCOUNT_ID)
    assert row is not None
    assert row["authorized"] is True
    assert row["stopped"] is False
    return db


def _make_transport(
    db: Database,
    client: _ConnectClient,
) -> tuple[TelegramService, list[tuple[str, str]]]:
    service = object.__new__(TelegramService)
    service.client = client
    service.limiter = SimpleNamespace()
    service.settings = SimpleNamespace(
        configured=True,
        expected_account_id=ACCOUNT_ID,
        account_id=ACCOUNT_ID,
        proxy_password="",
        proxy_secret="",
        api_hash="",
        phone="",
    )
    service.account_id = ACCOUNT_ID
    service._connected = False
    service._last_authorization_check = 0.0
    service._authorized_user = None
    service._status_callback = None
    service._peer_references = {}

    terminal_events: list[tuple[str, str]] = []

    def terminal_account_error(code: str, message: str) -> None:
        terminal_events.append((str(code), str(message)))
        db.mark_account_authorization_required(
            ACCOUNT_ID,
            error=f"{code}: {message}",
        )

    service._terminal_account_error_callback = terminal_account_error
    service._interruption_requested = lambda: False
    return service, terminal_events


def _account(db: Database) -> dict:
    row = db.get_telegram_account(ACCOUNT_ID)
    assert row is not None
    return row


def _assert_still_authorized(db: Database) -> None:
    row = _account(db)
    assert row["authorized"] is True
    assert row["stopped"] is False
    assert row["runtime_state"] != "authorization_required"


def _assert_authorization_revoked(db: Database) -> None:
    row = _account(db)
    assert row["authorized"] is False
    assert row["stopped"] is True
    assert row["runtime_state"] == "authorization_required"
    assert str(db.get_setting("telegram.authorized", "")) == "0"


@pytest.mark.asyncio
async def test_runtime_connect_uses_get_me_not_cached_is_user_authorized(tmp_path):
    db = _prepare_database(tmp_path)
    client = _ConnectClient(
        get_me_result=SimpleNamespace(id=ACCOUNT_ID),
        forbid_cached_auth_flag=True,
    )
    service, terminal_events = _make_transport(db, client)

    await service.connect()

    assert service._connected is True
    assert client.get_me_calls == 1
    assert client.authorization_flag_calls == 0
    assert terminal_events == []
    _assert_still_authorized(db)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("temporary Telegram identity probe failure"),
        asyncio.TimeoutError("temporary timeout"),
        ConnectionError("temporary proxy/network failure"),
        OSError("temporary socket failure"),
    ],
    ids=["runtime", "timeout", "connection", "oserror"],
)
@pytest.mark.asyncio
async def test_temporary_identity_probe_failures_never_revoke_account(
    tmp_path,
    error,
):
    db = _prepare_database(tmp_path)
    client = _ConnectClient(get_me_error=error)
    service, terminal_events = _make_transport(db, client)

    with pytest.raises(TelegramOperationError):
        await service.connect()

    assert terminal_events == []
    assert service._connected is False
    assert client.connected is False
    _assert_still_authorized(db)


@pytest.mark.asyncio
async def test_wrong_session_identity_blocks_runtime_without_false_logout(tmp_path):
    db = _prepare_database(tmp_path)
    client = _ConnectClient(get_me_result=SimpleNamespace(id=OTHER_ACCOUNT_ID))
    service, terminal_events = _make_transport(db, client)

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.connect()

    assert raised.value.code == "account_state_mismatch"
    assert terminal_events == []
    assert client.connected is False
    _assert_still_authorized(db)


@pytest.mark.asyncio
async def test_get_me_none_is_terminal_and_marks_account_for_reauthorization(tmp_path):
    db = _prepare_database(tmp_path)
    client = _ConnectClient(get_me_result=None)
    service, terminal_events = _make_transport(db, client)

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.connect()

    assert raised.value.code == "authorization_required"
    assert len(terminal_events) == 1
    assert terminal_events[0][0] == "authorization_required"
    _assert_authorization_revoked(db)


@pytest.mark.asyncio
async def test_unauthorized_rpc_is_terminal_and_marks_account_for_reauthorization(
    tmp_path,
):
    db = _prepare_database(tmp_path)
    client = _ConnectClient(get_me_error=_UnauthorizedProbe())
    service, terminal_events = _make_transport(db, client)

    with pytest.raises(NonRetryableTelegramError) as raised:
        await service.connect()

    assert raised.value.code == "authorization_required"
    assert len(terminal_events) == 1
    assert terminal_events[0][0] == "authorization_required"
    _assert_authorization_revoked(db)


def _fake_rpc_error(class_name: str, *, code: int, message: str) -> RPCError:
    cls = type(class_name, (RPCError,), {})
    exc = cls.__new__(cls)
    Exception.__init__(exc, message)
    exc.code = int(code)
    return exc


@pytest.mark.parametrize(
    ("rpc_name", "rpc_text", "expected_code"),
    [
        ("AuthKeyDuplicatedError", "AUTH_KEY_DUPLICATED", "auth_key_duplicated"),
        ("AuthKeyInvalidError", "AUTH_KEY_INVALID", "auth_key_invalid"),
        (
            "AuthKeyUnregisteredError",
            "AUTH_KEY_UNREGISTERED",
            "authorization_required",
        ),
        ("SessionExpiredError", "SESSION_EXPIRED", "authorization_required"),
        ("SessionRevokedError", "SESSION_REVOKED", "authorization_required"),
        ("UserDeactivatedError", "USER_DEACTIVATED", "account_deactivated"),
    ],
)
def test_terminal_server_auth_errors_revoke_only_the_affected_account(
    tmp_path,
    rpc_name,
    rpc_text,
    expected_code,
):
    db = _prepare_database(tmp_path)
    db.register_telegram_account(
        telegram_account_id=OTHER_ACCOUNT_ID,
        session_name=f"account_{OTHER_ACCOUNT_ID}",
        display_name="Other",
        authorized=True,
    )
    client = _ConnectClient(get_me_result=SimpleNamespace(id=ACCOUNT_ID))
    service, terminal_events = _make_transport(db, client)
    exc = _fake_rpc_error(rpc_name, code=401, message=rpc_text)

    with pytest.raises(NonRetryableTelegramError) as raised:
        service._raise_rpc_error(
            exc,
            retry_network=False,
            request_dispatched=True,
            unknown_result_code="delivery_result_unknown",
        )

    assert raised.value.code == expected_code
    assert len(terminal_events) == 1
    assert terminal_events[0][0] == expected_code
    _assert_authorization_revoked(db)

    other = db.get_telegram_account(OTHER_ACCOUNT_ID)
    assert other is not None
    assert other["authorized"] is True
    assert other["stopped"] is False


def test_user_restricted_is_not_misclassified_as_authorization_loss(tmp_path):
    db = _prepare_database(tmp_path)
    client = _ConnectClient(get_me_result=SimpleNamespace(id=ACCOUNT_ID))
    service, terminal_events = _make_transport(db, client)
    exc = _fake_rpc_error(
        "UserRestrictedError",
        code=403,
        message="USER_RESTRICTED",
    )

    with pytest.raises(NonRetryableTelegramError) as raised:
        service._raise_rpc_error(
            exc,
            retry_network=False,
            request_dispatched=True,
            unknown_result_code="delivery_result_unknown",
        )

    assert raised.value.code == "user_restricted"
    assert terminal_events == []
    _assert_still_authorized(db)


def test_generic_server_failure_is_not_misclassified_as_authorization_loss(tmp_path):
    db = _prepare_database(tmp_path)
    client = _ConnectClient(get_me_result=SimpleNamespace(id=ACCOUNT_ID))
    service, terminal_events = _make_transport(db, client)
    exc = _fake_rpc_error(
        "ServerError",
        code=500,
        message="INTERNAL_SERVER_ERROR",
    )

    with pytest.raises(NonRetryableTelegramError) as raised:
        service._raise_rpc_error(
            exc,
            retry_network=False,
            request_dispatched=True,
            unknown_result_code="delivery_result_unknown",
        )

    assert raised.value.code == "delivery_result_unknown"
    assert terminal_events == []
    _assert_still_authorized(db)


def test_manual_stop_does_not_destroy_authorization(tmp_path):
    db = _prepare_database(tmp_path)

    db.set_account_runtime_state(ACCOUNT_ID, "stopped")

    row = _account(db)
    assert row["authorized"] is True
    assert row["stopped"] is True
    assert row["runtime_state"] == "stopped"


def test_registry_reauthorization_restores_authorized_state_and_clears_error(tmp_path):
    db = _prepare_database(tmp_path)
    db.mark_account_authorization_required(
        ACCOUNT_ID,
        error="authorization_required: session revoked",
    )
    _assert_authorization_revoked(db)

    row, created = db.register_telegram_account(
        telegram_account_id=ACCOUNT_ID,
        session_name=f"account_{ACCOUNT_ID}",
        display_name="Auth Test",
        username="auth_test",
        authorized=True,
    )

    assert created is False
    assert bool(row["authorized"]) is True
    assert bool(row["stopped"]) is True
    # Existing stopped state is intentionally preserved until the higher-level
    # reauthorization flow calls resume_account_work().
    assert row["runtime_state"] == "stopped"
    assert row["last_error"] is None


def test_structurally_corrupt_session_is_quarantined_instead_of_reused(tmp_path):
    session_file = tmp_path / "account_77.session"
    session_file.write_bytes(b"this is not a sqlite session")

    TelegramService._prepare_session_file(session_file)

    assert not session_file.exists()
    quarantined = list(tmp_path.glob("account_77.session.corrupt.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"this is not a sqlite session"


def test_encrypted_auth_key_integrity_failure_is_explicit_not_silently_accepted():
    class _BrokenCodec:
        @staticmethod
        def is_encrypted(_payload: bytes) -> bool:
            return True

        @staticmethod
        def decrypt(_payload: bytes, *, purpose: str) -> bytes:
            raise VaultIntegrityError("wrong OS profile or corrupted key")

    session = object.__new__(EncryptedSQLiteSession)
    session._session_codec = _BrokenCodec()

    with pytest.raises(TelegramSessionEncryptionError) as raised:
        session._decode_key(
            b"encrypted-session-key",
            purpose=EncryptedSQLiteSession.AUTH_PURPOSE,
        )

    assert "corrupted or belongs to another OS profile" in str(raised.value)

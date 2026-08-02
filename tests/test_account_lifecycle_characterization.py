from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from services.account_context import SECRET_SETTING_KEYS, account_secret_key
from services.account_sessions import lifecycle_journal_key
from services.api_parts.accounts import AccountsAPIMixin
from storage.database import Database


class _MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str, default: object = ""):
        return self.values.get(str(key), default)

    def get_strict_optional(self, key: str) -> str | None:
        return self.values.get(str(key))

    def set(self, key: str, value: object) -> None:
        name = str(key)
        if value in (None, ""):
            self.values.pop(name, None)
        else:
            self.values[name] = str(value)

    def delete(self, key: str) -> None:
        self.values.pop(str(key), None)


class _AccountAPI(AccountsAPIMixin):
    def __init__(
        self,
        database: Database,
        secret_store: _MemorySecretStore,
        session_dir: Path,
        *,
        queue_worker=None,
    ) -> None:
        self.database = database
        self.secret_store = secret_store
        self.config = SimpleNamespace(
            telegram=SimpleNamespace(session_dir=session_dir)
        )
        self.queue_worker = queue_worker
        self._secret_lock = threading.RLock()


class _TimeoutFuture:
    def result(self, timeout: float | None = None):
        del timeout
        raise FutureTimeoutError


class _TimeoutWorker:
    def __init__(self) -> None:
        self.cancelled_scopes: list[tuple[str, int]] = []
        self.utilities: list[tuple[str, dict[str, int]]] = []

    def isRunning(self) -> bool:
        return True

    def cancel_scopes_and_run(self, scopes, mutation):
        self.cancelled_scopes.extend(scopes)
        return mutation()

    def request_scope_cancellation(self, scope_type: str, scope_id: int) -> None:
        self.cancelled_scopes.append((scope_type, int(scope_id)))

    def submit_utility(self, name: str, payload: dict[str, int]):
        self.utilities.append((name, dict(payload)))
        return _TimeoutFuture()


@pytest.fixture(autouse=True)
def _avoid_platform_acl_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise real family moves without depending on the runner ACL backend."""

    monkeypatch.setattr(
        "services.telegram_service.TelegramService._secure_session_file",
        staticmethod(lambda _path: None),
    )


def _api(tmp_path: Path, *, queue_worker=None) -> _AccountAPI:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    return _AccountAPI(
        Database(tmp_path / "accounts.db"),
        _MemorySecretStore(),
        session_dir,
        queue_worker=queue_worker,
    )


def _session_path(api: _AccountAPI, name: str) -> Path:
    return Path(api.config.telegram.session_dir) / f"{name}.session"


def _write_session(api: _AccountAPI, name: str, payload: bytes) -> Path:
    path = _session_path(api, name)
    path.write_bytes(payload)
    return path


def _register_existing(
    api: _AccountAPI,
    account_id: int,
    *,
    stopped: bool = False,
    display_name: str = "Existing Account",
) -> None:
    api.database.register_telegram_account(
        telegram_account_id=account_id,
        session_name=f"account_{account_id}",
        display_name=display_name,
        username=f"existing_{account_id}",
        phone="+491234567890",
        authorized=True,
    )
    if stopped:
        api.database.begin_account_stop(account_id)
        api.database.finish_account_stop(account_id)


def _assert_no_journal(api: _AccountAPI, account_id: int) -> None:
    assert api.secret_store.get(lifecycle_journal_key(account_id), None) is None


def test_register_authorized_account_moves_session_and_partitions_settings(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    pending = "pending_0123456789abcdef"
    _write_session(api, pending, b"new-session")

    result = api.register_authorized_account(
        {
            "id": 101,
            "name": "Primary Account",
            "username": "primary",
            "phone": "+491111111111",
        },
        {
            "automation.enabled": True,
            "commenting.daily_limit": 40,
            "telegram.api_hash": "hash-101",
            "openai.api_key": "openai-101",
            "telegram.account_id": "999",
            "telegram.session_name": "unsafe-override",
        },
        pending_session_name=pending,
    )

    assert result["created"] is True
    assert result["telegram_account_id"] == 101
    assert not _session_path(api, pending).exists()
    assert _session_path(api, "account_101").read_bytes() == b"new-session"
    assert api.database.get_account_settings(101) == {
        "automation.enabled": "True",
        "commenting.daily_limit": "40",
    }
    assert (
        api.secret_store.get(account_secret_key(101, "telegram.api_hash"))
        == "hash-101"
    )
    assert (
        api.secret_store.get(account_secret_key(101, "openai.api_key"))
        == "openai-101"
    )
    assert api.database.get_account_setting(101, "telegram.account_id", None) is None
    _assert_no_journal(api, 101)


def test_new_registration_rolls_back_row_secrets_and_session_after_late_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(tmp_path)
    pending = "pending_1111111111111111"
    _write_session(api, pending, b"pending-session")

    def fail_selection(_account_id: int):
        raise RuntimeError("selection failed")

    monkeypatch.setattr(api.database, "select_telegram_account", fail_selection)

    with pytest.raises(RuntimeError, match="selection failed"):
        api.register_authorized_account(
            {"id": 102, "name": "Rollback Account"},
            {
                "automation.enabled": True,
                "telegram.api_hash": "temporary-hash",
            },
            pending_session_name=pending,
        )

    assert api.database.get_telegram_account(102) is None
    assert not _session_path(api, pending).exists()
    assert not _session_path(api, "account_102").exists()
    assert (
        api.secret_store.get(
            account_secret_key(102, "telegram.api_hash"), None
        )
        is None
    )
    _assert_no_journal(api, 102)


def test_save_account_settings_partitions_public_secret_and_identity_values(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    _register_existing(api, 103)
    api.database.select_telegram_account(103)

    api.save_account_settings(
        {
            "automation.enabled": False,
            "commenting.daily_limit": 55,
            "telegram.api_hash": "saved-hash",
            "telegram.account_name": "must-not-overwrite",
            "telegram.runtime_state": "restricted",
        },
        account_id=103,
    )

    assert api.database.get_account_settings(103) == {
        "automation.enabled": "False",
        "commenting.daily_limit": "55",
    }
    assert api.database.get_setting("automation.enabled") == "False"
    assert (
        api.secret_store.get(account_secret_key(103, "telegram.api_hash"))
        == "saved-hash"
    )
    account = api.database.get_telegram_account(103)
    assert account is not None
    assert account["display_name"] == "Existing Account"
    assert account["runtime_state"] == "connected"


def test_save_account_settings_restores_secrets_when_public_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(tmp_path)
    _register_existing(api, 104)
    api.database.select_telegram_account(104)
    api._set_account_secret(104, "telegram.api_hash", "old-hash")
    api._set_account_secret(104, "openai.api_key", "old-openai")

    def fail_public_write(_account_id: int, _values: dict[str, object]) -> None:
        raise RuntimeError("database write failed")

    monkeypatch.setattr(api.database, "set_account_settings", fail_public_write)

    with pytest.raises(RuntimeError, match="database write failed"):
        api.save_account_settings(
            {
                "automation.enabled": True,
                "telegram.api_hash": "new-hash",
                "openai.api_key": "new-openai",
            },
            account_id=104,
        )

    assert (
        api.secret_store.get(account_secret_key(104, "telegram.api_hash"))
        == "old-hash"
    )
    assert (
        api.secret_store.get(account_secret_key(104, "openai.api_key"))
        == "old-openai"
    )
    assert api.database.get_account_setting(104, "automation.enabled", None) is None


def test_reauthorize_existing_account_replaces_session_and_resumes_work(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    _register_existing(api, 105, stopped=True)
    api.database.set_account_settings(105, {"automation.enabled": False})
    api._set_account_secret(105, "telegram.api_hash", "old-hash")
    api._set_account_secret(105, "openai.api_key", "keep-openai")
    _write_session(api, "account_105", b"old-session")
    pending = "pending_2222222222222222"
    _write_session(api, pending, b"new-session")

    result = api.register_authorized_account(
        {"id": 105, "name": "Reauthorized", "username": "reauthorized"},
        {
            "automation.enabled": True,
            "commenting.daily_limit": 60,
            "telegram.api_hash": "new-hash",
        },
        pending_session_name=pending,
    )

    assert result["created"] is False
    assert result["duplicate"] is True
    assert result["reauthorized"] is True
    assert _session_path(api, "account_105").read_bytes() == b"new-session"
    assert not _session_path(api, pending).exists()
    assert list(Path(api.config.telegram.session_dir).glob(".swap_*.session")) == []
    account = api.database.get_telegram_account(105)
    assert account is not None
    assert account["display_name"] == "Reauthorized"
    assert account["stopped"] is False
    assert account["runtime_state"] == "connected"
    assert api.database.get_account_settings(105) == {
        "automation.enabled": "True",
        "commenting.daily_limit": "60",
    }
    assert (
        api.secret_store.get(account_secret_key(105, "telegram.api_hash"))
        == "new-hash"
    )
    assert (
        api.secret_store.get(account_secret_key(105, "openai.api_key"))
        == "keep-openai"
    )
    _assert_no_journal(api, 105)


def test_reauthorization_failure_restores_old_session_settings_and_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(tmp_path)
    _register_existing(api, 106, stopped=True, display_name="Original")
    api.database.set_account_settings(
        106,
        {"automation.enabled": False, "commenting.daily_limit": 25},
    )
    api._set_account_secret(106, "telegram.api_hash", "old-hash")
    api._set_account_secret(106, "openai.api_key", "old-openai")
    _write_session(api, "account_106", b"old-session")
    pending = "pending_3333333333333333"
    _write_session(api, pending, b"new-session")

    def fail_resume(_account_id: int) -> None:
        raise RuntimeError("resume failed")

    monkeypatch.setattr(api.database, "resume_account_work", fail_resume)

    with pytest.raises(RuntimeError, match="resume failed"):
        api.register_authorized_account(
            {"id": 106, "name": "Must Roll Back"},
            {
                "automation.enabled": True,
                "telegram.api_hash": "new-hash",
                "openai.api_key": "new-openai",
            },
            pending_session_name=pending,
        )

    assert _session_path(api, "account_106").read_bytes() == b"old-session"
    assert not _session_path(api, pending).exists()
    account = api.database.get_telegram_account(106)
    assert account is not None
    assert account["display_name"] == "Original"
    assert account["stopped"] is True
    assert account["runtime_state"] == "stopped"
    assert api.database.get_account_settings(106) == {
        "automation.enabled": "False",
        "commenting.daily_limit": "25",
    }
    assert (
        api.secret_store.get(account_secret_key(106, "telegram.api_hash"))
        == "old-hash"
    )
    assert (
        api.secret_store.get(account_secret_key(106, "openai.api_key"))
        == "old-openai"
    )
    _assert_no_journal(api, 106)


def test_stop_account_without_running_worker_finishes_in_stopped_state(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    _register_existing(api, 107)

    result = api.stop_telegram_account(107)

    assert result["disconnected"] is False
    assert result["account_id"] == 107
    account = api.database.get_telegram_account(107)
    assert account is not None
    assert account["stopped"] is True
    assert account["runtime_state"] == "stopped"
    assert account["last_error"] is None


def test_stop_account_timeout_is_persisted_as_error_state(tmp_path: Path) -> None:
    worker = _TimeoutWorker()
    api = _api(tmp_path, queue_worker=worker)
    _register_existing(api, 108)

    with pytest.raises(RuntimeError, match="ручной проверки"):
        api.stop_telegram_account(108, timeout_seconds=0.01)

    account = api.database.get_telegram_account(108)
    assert account is not None
    assert account["stopped"] is False
    assert account["runtime_state"] == "error"
    assert "ручной проверки" in str(account["last_error"])
    assert worker.utilities == [
        ("stop_account_runtime", {"account_id": 108})
    ]
    assert ("account", 108) in worker.cancelled_scopes


def test_delete_account_removes_database_session_secrets_and_journal(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    _register_existing(api, 109)
    api.database.select_telegram_account(109)
    api.database.set_account_settings(109, {"automation.enabled": True})
    for key in SECRET_SETTING_KEYS:
        api._set_account_secret(109, key, f"secret-{key}")
    _write_session(api, "account_109", b"session-to-delete")

    result = api.delete_telegram_account(109)

    assert result["deleted_account_id"] == 109
    assert result["remaining_accounts"] == 0
    assert result["session_cleanup_warning"] == ""
    assert api.database.get_telegram_account(109) is None
    assert not _session_path(api, "account_109").exists()
    for key in SECRET_SETTING_KEYS:
        assert api.secret_store.get(account_secret_key(109, key), None) is None
    _assert_no_journal(api, 109)


def test_delete_failure_restores_session_and_secrets_and_keeps_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(tmp_path)
    _register_existing(api, 110)
    api.database.set_account_settings(110, {"automation.enabled": True})
    api._set_account_secret(110, "telegram.api_hash", "old-hash")
    api._set_account_secret(110, "openai.api_key", "old-openai")
    _write_session(api, "account_110", b"session-to-restore")

    def fail_delete(_account_id: int):
        raise RuntimeError("delete failed")

    monkeypatch.setattr(api.database, "delete_telegram_account_data", fail_delete)

    with pytest.raises(RuntimeError, match="delete failed"):
        api.delete_telegram_account(110)

    account = api.database.get_telegram_account(110)
    assert account is not None
    assert account["stopped"] is True
    assert _session_path(api, "account_110").read_bytes() == b"session-to-restore"
    assert api.database.get_account_settings(110) == {
        "automation.enabled": "True"
    }
    assert (
        api.secret_store.get(account_secret_key(110, "telegram.api_hash"))
        == "old-hash"
    )
    assert (
        api.secret_store.get(account_secret_key(110, "openai.api_key"))
        == "old-openai"
    )
    _assert_no_journal(api, 110)

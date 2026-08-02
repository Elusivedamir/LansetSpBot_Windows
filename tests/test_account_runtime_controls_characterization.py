from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.api_parts.accounts import AccountsAPIMixin
from storage.database import Database


class _AccountAPI(AccountsAPIMixin):
    def __init__(
        self,
        database: Database,
        session_dir: Path,
        *,
        queue_worker: Any = None,
    ) -> None:
        self.database = database
        self.secret_store = SimpleNamespace()
        self.config = SimpleNamespace(
            telegram=SimpleNamespace(session_dir=session_dir)
        )
        self.queue_worker = queue_worker
        self._secret_lock = threading.RLock()
        self.start_queue_calls = 0

    def start_queue(self) -> None:
        self.start_queue_calls += 1
        worker = self.queue_worker
        if worker is not None:
            worker.running = True


class _RuntimeFuture:
    def __init__(
        self,
        value: dict[str, Any] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.value = dict(value or {})
        self.error = error
        self.timeouts: list[float | None] = []

    def result(self, timeout: float | None = None) -> dict[str, Any]:
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return dict(self.value)


class _RuntimeWorker:
    def __init__(
        self,
        *,
        running: bool = True,
        result: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.running = bool(running)
        self.future = _RuntimeFuture(result, error=error)
        self.cleared_scopes: list[tuple[str, int]] = []
        self.utilities: list[tuple[str, dict[str, int]]] = []

    def isRunning(self) -> bool:
        return self.running

    def clear_scope_cancellation(self, scope_type: str, scope_id: int) -> None:
        self.cleared_scopes.append((str(scope_type), int(scope_id)))

    def submit_utility(
        self, name: str, payload: dict[str, int]
    ) -> _RuntimeFuture:
        self.utilities.append((str(name), dict(payload)))
        return self.future


def _api(tmp_path: Path, *, queue_worker: Any = None) -> _AccountAPI:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    return _AccountAPI(
        Database(tmp_path / "runtime-controls.db"),
        session_dir,
        queue_worker=queue_worker,
    )


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


def test_update_authorized_metadata_preserves_account_identity(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    _register_existing(api, 201, display_name="Original")

    result = api.update_authorized_account_metadata(
        {
            "id": 201,
            "name": "Updated Account",
            "username": "@updated_user",
            "phone": "+499876543210",
        }
    )

    assert result["telegram_account_id"] == 201
    assert result["session_name"] == "account_201"
    assert result["display_name"] == "Updated Account"
    assert result["username"] == "updated_user"
    assert result["authorized"] in (1, True)
    assert result["runtime_state"] == "connected"


def test_update_authorized_metadata_rejects_unknown_account(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)

    with pytest.raises(ValueError, match="не зарегистрирован"):
        api.update_authorized_account_metadata(
            {"id": 299, "name": "Unknown"}
        )


def test_resume_account_updates_database_and_clears_cancellation(
    tmp_path: Path,
) -> None:
    worker = _RuntimeWorker()
    api = _api(tmp_path, queue_worker=worker)
    _register_existing(api, 202, stopped=True)

    result = api.resume_telegram_account(202)

    assert result["telegram_account_id"] == 202
    assert result["stopped"] is False
    assert result["runtime_state"] == "connected"
    assert worker.cleared_scopes == [("account", 202)]


def test_check_runtime_without_worker_preserves_stopped_state(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    _register_existing(api, 203, stopped=True)

    with pytest.raises(RuntimeError, match="Фоновый обработчик не создан"):
        api.check_telegram_account_runtime(203)

    account = api.database.get_telegram_account(203)
    assert account is not None
    assert account["stopped"] is True
    assert account["runtime_state"] == "stopped"


def test_check_runtime_starts_worker_and_forwards_payload_and_timeout(
    tmp_path: Path,
) -> None:
    worker = _RuntimeWorker(
        running=False,
        result={"account_id": 204, "authorized": True},
    )
    api = _api(tmp_path, queue_worker=worker)
    _register_existing(api, 204, stopped=True)

    result = api.check_telegram_account_runtime(
        204, timeout_seconds=0.2
    )

    assert result == {"account_id": 204, "authorized": True}
    assert api.start_queue_calls == 1
    assert worker.running is True
    assert worker.cleared_scopes == [("account", 204)]
    assert worker.utilities == [
        ("check_account_runtime", {"account_id": 204})
    ]
    assert worker.future.timeouts == [1.0]
    account = api.database.get_telegram_account(204)
    assert account is not None
    assert account["stopped"] is False
    assert account["runtime_state"] == "connected"


def test_check_runtime_propagates_future_error_after_resume(
    tmp_path: Path,
) -> None:
    worker = _RuntimeWorker(
        error=RuntimeError("runtime check failed")
    )
    api = _api(tmp_path, queue_worker=worker)
    _register_existing(api, 205, stopped=True)

    with pytest.raises(RuntimeError, match="runtime check failed"):
        api.check_telegram_account_runtime(
            205, timeout_seconds=2.5
        )

    assert worker.cleared_scopes == [("account", 205)]
    assert worker.utilities == [
        ("check_account_runtime", {"account_id": 205})
    ]
    assert worker.future.timeouts == [2.5]
    account = api.database.get_telegram_account(205)
    assert account is not None
    assert account["stopped"] is False
    assert account["runtime_state"] == "connected"


def test_previous_account_imports_forward_source_target_and_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(tmp_path)
    _register_existing(api, 206)
    _register_existing(api, 207)
    api.database.select_telegram_account(206)
    api.database.select_telegram_account(207)

    calls: list[tuple[str, int, int, str | None]] = []

    def import_comments(
        *,
        source_account_id: int,
        target_account_id: int,
        mode: str,
    ) -> dict[str, Any]:
        calls.append(
            (
                "comments",
                int(source_account_id),
                int(target_account_id),
                str(mode),
            )
        )
        return {"imported": 4, "mode": mode}

    def import_channels(
        *,
        source_account_id: int,
        target_account_id: int,
    ) -> dict[str, int]:
        calls.append(
            (
                "channels",
                int(source_account_id),
                int(target_account_id),
                None,
            )
        )
        return {"imported": 3}

    monkeypatch.setattr(
        api.database,
        "import_comment_profile_between_accounts",
        import_comments,
    )
    monkeypatch.setattr(
        api.database,
        "import_channels_between_accounts",
        import_channels,
    )

    comment_result = api.import_comments_from_previous_account(
        mode="replace"
    )
    channel_result = api.import_channels_from_previous_account()

    assert comment_result == {"imported": 4, "mode": "replace"}
    assert channel_result == {"imported": 3}
    assert calls == [
        ("comments", 206, 207, "replace"),
        ("channels", 206, 207, None),
    ]


def test_previous_account_imports_reject_missing_transfer_pair(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    _register_existing(api, 208)
    api.database.select_telegram_account(208)

    with pytest.raises(ValueError, match="Сначала переключитесь"):
        api.import_comments_from_previous_account(mode="merge")
    with pytest.raises(ValueError, match="Сначала переключитесь"):
        api.import_channels_from_previous_account()

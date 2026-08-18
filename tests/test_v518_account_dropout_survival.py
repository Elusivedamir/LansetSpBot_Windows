from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from core.single_instance import SingleInstance
from services.account_context import AccountContainerView
from services.account_runtime_manager import (
    TelegramAccountRuntime,
    TelegramAccountRuntimeManager,
)
from services.telegram_service import TelegramService
from storage.database import Database
from storage.db_common import DatabaseError


ACCOUNT_ID = 77
OTHER_ACCOUNT_ID = 88
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT_SECONDS = 15.0


@pytest.fixture(scope="module")
def qcore_app():
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


def _run_single_instance_child(
    name: str,
    *,
    action: str,
    qcore_app,
) -> subprocess.CompletedProcess[str]:
    code = r"""
import os
import sys

from PySide6.QtCore import QCoreApplication
from core.single_instance import SingleInstance

app = QCoreApplication([])
instance = SingleInstance(sys.argv[1])
acquired = instance.acquire()
print("ACQUIRED=1" if acquired else "ACQUIRED=0", flush=True)

if acquired and sys.argv[2] == "crash":
    os._exit(0)

if acquired:
    instance.close()
"""
    env = os.environ.copy()
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(PROJECT_ROOT)
        if not inherited
        else str(PROJECT_ROOT) + os.pathsep + inherited
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, name, action],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + CHILD_TIMEOUT_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        qcore_app.processEvents()
        time.sleep(0.01)
    if process.poll() is None:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(
            f"single-instance child timed out\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )
    stdout, stderr = process.communicate(timeout=5)
    return subprocess.CompletedProcess(
        process.args,
        int(process.returncode or 0),
        stdout,
        stderr,
    )


def test_second_os_process_cannot_acquire_same_profile_guard(qcore_app):
    """A second LansetSpBot process must lose before it can open DB/session files."""
    name = f"com.marlen.pro.v518.concurrent.{uuid.uuid4().hex}"
    primary = SingleInstance(name=name)
    assert primary.acquire() is True
    try:
        child = _run_single_instance_child(
            name,
            action="close",
            qcore_app=qcore_app,
        )
        assert child.returncode == 0, child.stderr
        assert "ACQUIRED=0" in child.stdout
    finally:
        primary.close()


def test_single_instance_stale_lock_recovers_after_hard_process_exit(qcore_app):
    """A crashed primary must not permanently block the next legitimate launch."""
    name = f"com.marlen.pro.v518.crash.{uuid.uuid4().hex}"
    child = _run_single_instance_child(
        name,
        action="crash",
        qcore_app=qcore_app,
    )
    assert child.returncode == 0, child.stderr
    assert "ACQUIRED=1" in child.stdout

    replacement = SingleInstance(name=name)
    try:
        assert replacement.acquire() is True
    finally:
        replacement.close()


def test_production_entry_acquires_guard_before_application_container():
    """The losing process must exit before ApplicationContainer can touch sessions."""
    source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    guard = source.index("if not instance.acquire():")
    container = source.index("container = ApplicationContainer(config)")
    assert guard < container


class _RuntimeDatabase:
    def __init__(self, account_ids=(ACCOUNT_ID, OTHER_ACCOUNT_ID)) -> None:
        self.accounts = {
            int(account_id): {
                "telegram_account_id": int(account_id),
                "authorized": True,
                "runtime_state": "connected",
                "stopped": False,
                "last_error": None,
            }
            for account_id in account_ids
        }
        self.state_updates: list[tuple[int, str]] = []

    def get_telegram_account(self, account_id: int):
        row = self.accounts.get(int(account_id))
        return None if row is None else dict(row)

    def set_account_runtime_state(
        self,
        account_id: int,
        state: str,
        *,
        error: str | None = None,
    ) -> None:
        owner = int(account_id)
        row = self.accounts[owner]
        row["runtime_state"] = str(state)
        row["stopped"] = str(state) == "stopped"
        row["last_error"] = error
        self.state_updates.append((owner, str(state)))

    def get_account_restriction(self, *, account_id: int):
        return {"active": False, "account_id": int(account_id)}

    def get_account_rpc_cooldown(self, *, account_id: int):
        return {}


class _QueueWorker:
    def __init__(self, *, cooldown: int = 0) -> None:
        self.cooldown = int(cooldown)

    def is_scope_cancelled(self, *_args, **_kwargs) -> bool:
        return False

    def _account_rpc_cooldown_remaining(self, _account_id: int, _row) -> int:
        return self.cooldown


def _manager(
    *,
    account_ids=(ACCOUNT_ID, OTHER_ACCOUNT_ID),
    cooldown: int = 0,
) -> tuple[TelegramAccountRuntimeManager, _RuntimeDatabase]:
    database = _RuntimeDatabase(account_ids)
    container = SimpleNamespace(queue_worker=_QueueWorker(cooldown=cooldown))
    manager = TelegramAccountRuntimeManager(
        container,
        worker_database=database,
        create_worker_handlers=lambda *_args, **_kwargs: {},
        TelegramService=object,
        ImportService=object,
        LinkedChatService=object,
        CommentService=object,
    )
    return manager, database


@pytest.mark.asyncio
async def test_concurrent_same_account_requests_construct_exactly_one_runtime():
    """Concurrent tasks for one account must share one Telethon client graph."""
    manager, database = _manager(account_ids=(ACCOUNT_ID,))
    create_started = asyncio.Event()
    release_create = asyncio.Event()
    create_calls = 0
    cleanup_calls = 0

    async def create(
        account_id: int,
        *,
        publish_runtime_state: bool = True,
    ) -> TelegramAccountRuntime:
        nonlocal create_calls, cleanup_calls
        create_calls += 1
        create_started.set()
        await release_create.wait()

        async def cleanup() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1

        if publish_runtime_state:
            database.set_account_runtime_state(account_id, "connected")
        return TelegramAccountRuntime(
            account_id=account_id,
            handlers={},
            cleanup=cleanup,
            lock=asyncio.Lock(),
            last_used=time.monotonic(),
        )

    manager._create_runtime = create  # type: ignore[method-assign]

    first_task = asyncio.create_task(manager.get_runtime(ACCOUNT_ID))
    await create_started.wait()
    second_task = asyncio.create_task(manager.get_runtime(ACCOUNT_ID))
    await asyncio.sleep(0)

    assert create_calls == 1
    release_create.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first is second
    assert first.reservations == 2
    assert database.accounts[ACCOUNT_ID]["authorized"] is True

    manager._release_runtime(first)
    manager._release_runtime(second)
    await manager.close()
    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_stop_runtime_blocks_same_account_recreation_until_cleanup_finishes():
    """Stop and recreation may be sequential, never overlapping for one auth key."""
    manager, database = _manager(account_ids=(ACCOUNT_ID,))
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    generations = 0

    async def create(
        account_id: int,
        *,
        publish_runtime_state: bool = True,
    ) -> TelegramAccountRuntime:
        nonlocal generations
        generations += 1
        generation = generations

        async def cleanup() -> None:
            if generation == 1:
                cleanup_started.set()
                await release_cleanup.wait()

        return TelegramAccountRuntime(
            account_id=account_id,
            handlers={},
            cleanup=cleanup,
            lock=asyncio.Lock(),
            last_used=time.monotonic(),
        )

    manager._create_runtime = create  # type: ignore[method-assign]
    original = await manager.get_runtime(ACCOUNT_ID)
    manager._release_runtime(original)

    stop_task = asyncio.create_task(manager.stop_runtime(ACCOUNT_ID))
    await cleanup_started.wait()

    with pytest.raises(NonRetryableTelegramError) as raised:
        await manager.get_runtime(ACCOUNT_ID)
    assert raised.value.code == "account_stopped"
    assert generations == 1
    assert database.accounts[ACCOUNT_ID]["authorized"] is True

    release_cleanup.set()
    assert await stop_task == {
        "account_id": ACCOUNT_ID,
        "disconnected": True,
    }

    replacement = await manager.get_runtime(ACCOUNT_ID)
    assert generations == 2
    assert replacement is not original
    manager._release_runtime(replacement)
    await manager.close()


@pytest.mark.asyncio
async def test_failed_eviction_quarantines_runtime_instead_of_creating_duplicate():
    """Failed disconnect must keep ownership, not spawn a second client on same key."""
    manager, database = _manager(account_ids=(ACCOUNT_ID, OTHER_ACCOUNT_ID))
    manager.MAX_ACCOUNTS = 1
    create_calls: list[int] = []

    async def create(
        account_id: int,
        *,
        publish_runtime_state: bool = True,
    ) -> TelegramAccountRuntime:
        create_calls.append(int(account_id))

        async def cleanup() -> None:
            if int(account_id) == ACCOUNT_ID:
                raise RuntimeError("simulated disconnect failure")

        return TelegramAccountRuntime(
            account_id=account_id,
            handlers={},
            cleanup=cleanup,
            lock=asyncio.Lock(),
            last_used=time.monotonic(),
        )

    manager._create_runtime = create  # type: ignore[method-assign]
    first = await manager.get_runtime(ACCOUNT_ID)
    manager._release_runtime(first)

    with pytest.raises(RuntimeError, match="disconnect failure"):
        await manager.get_runtime(OTHER_ACCOUNT_ID)

    assert create_calls == [ACCOUNT_ID]
    assert manager._runtimes == {ACCOUNT_ID: first}
    assert ACCOUNT_ID in manager._evicting_accounts
    assert database.accounts[ACCOUNT_ID]["authorized"] is True

    with pytest.raises(DeferredTelegramError) as raised:
        await manager.get_runtime(ACCOUNT_ID)
    assert raised.value.code == "account_runtime_recycling"
    assert create_calls == [ACCOUNT_ID]

    async def clean_now() -> None:
        return None

    first.cleanup = clean_now
    await manager.stop_runtime(ACCOUNT_ID)
    assert manager._runtimes == {}


@pytest.mark.asyncio
async def test_capacity_eviction_and_later_recreation_never_overlap_same_account():
    """Runtime recycling can recreate an account only after its old client cleaned up."""
    manager, _database = _manager(account_ids=(ACCOUNT_ID, OTHER_ACCOUNT_ID))
    manager.MAX_ACCOUNTS = 1
    live = defaultdict(int)
    max_live = defaultdict(int)
    events: list[tuple[str, int]] = []

    async def create(
        account_id: int,
        *,
        publish_runtime_state: bool = True,
    ) -> TelegramAccountRuntime:
        owner = int(account_id)
        assert live[owner] == 0
        live[owner] += 1
        max_live[owner] = max(max_live[owner], live[owner])
        events.append(("create", owner))

        async def cleanup() -> None:
            events.append(("cleanup", owner))
            assert live[owner] == 1
            live[owner] -= 1

        return TelegramAccountRuntime(
            account_id=owner,
            handlers={},
            cleanup=cleanup,
            lock=asyncio.Lock(),
            last_used=time.monotonic(),
        )

    manager._create_runtime = create  # type: ignore[method-assign]

    first = await manager.get_runtime(ACCOUNT_ID)
    manager._release_runtime(first)

    other = await manager.get_runtime(OTHER_ACCOUNT_ID)
    manager._release_runtime(other)

    second = await manager.get_runtime(ACCOUNT_ID)
    manager._release_runtime(second)

    assert first is not second
    assert max_live[ACCOUNT_ID] == 1
    assert events[:5] == [
        ("create", ACCOUNT_ID),
        ("cleanup", ACCOUNT_ID),
        ("create", OTHER_ACCOUNT_ID),
        ("cleanup", OTHER_ACCOUNT_ID),
        ("create", ACCOUNT_ID),
    ]

    await manager.close()
    assert live[ACCOUNT_ID] == 0
    assert live[OTHER_ACCOUNT_ID] == 0


def test_repeated_stop_start_cycles_preserve_authorization(tmp_path):
    """Operator Stop/Start must never turn a healthy saved session into logout."""
    db = Database(tmp_path / "stop-resume.db")
    db.register_telegram_account(
        telegram_account_id=ACCOUNT_ID,
        session_name=f"account_{ACCOUNT_ID}",
        display_name="Stop Resume",
        authorized=True,
    )

    for _ in range(25):
        db.begin_account_stop(ACCOUNT_ID)
        db.finish_account_stop(ACCOUNT_ID)
        stopped = db.get_telegram_account(ACCOUNT_ID)
        assert stopped is not None
        assert stopped["authorized"] is True
        assert stopped["stopped"] is True
        assert stopped["runtime_state"] == "stopped"

        db.resume_account_work(ACCOUNT_ID)
        resumed = db.get_telegram_account(ACCOUNT_ID)
        assert resumed is not None
        assert resumed["authorized"] is True
        assert resumed["stopped"] is False
        assert resumed["runtime_state"] == "connected"

    assert db.account_accepts_new_work(ACCOUNT_ID) is True


def test_database_rejects_two_accounts_bound_to_one_session_name(tmp_path):
    """Two registry rows must never point at one reusable Telegram auth key."""
    db = Database(tmp_path / "session-unique.db")
    db.register_telegram_account(
        telegram_account_id=ACCOUNT_ID,
        session_name=f"account_{ACCOUNT_ID}",
        display_name="First",
        authorized=True,
    )

    with pytest.raises(DatabaseError):
        db.register_telegram_account(
            telegram_account_id=OTHER_ACCOUNT_ID,
            session_name=f"account_{ACCOUNT_ID}",
            display_name="Second",
            authorized=True,
        )

    first = db.get_telegram_account(ACCOUNT_ID)
    second = db.get_telegram_account(OTHER_ACCOUNT_ID)
    assert first is not None
    assert first["session_name"] == f"account_{ACCOUNT_ID}"
    assert second is None


class _SecretStore:
    def get(self, _key: str, default: str = ""):
        return default

    def get_strict_optional(self, _key: str):
        return None

    def set(self, _key: str, _value) -> None:
        return None

    def delete(self, _key: str) -> None:
        return None


def test_all_seventy_account_contexts_resolve_distinct_session_names(tmp_path):
    """Rapid account switching cannot project account_N onto another account."""
    db = Database(tmp_path / "seventy-session-bindings.db")
    account_ids = list(range(1001, 1071))
    for account_id in account_ids:
        db.register_telegram_account(
            telegram_account_id=account_id,
            session_name=f"account_{account_id}",
            display_name=f"Account {account_id}",
            authorized=True,
        )

    base = SimpleNamespace(
        config=SimpleNamespace(
            telegram=SimpleNamespace(
                api_id=12345,
                api_hash="fallback-api-hash",
                session_dir=tmp_path / "sessions",
                phone=None,
            )
        ),
        secret_store=_SecretStore(),
        queue_worker=SimpleNamespace(),
        api=SimpleNamespace(_secret_lock=nullcontext()),
    )

    resolved: dict[int, str] = {}
    for account_id in account_ids:
        context = AccountContainerView(
            base,
            account_id=account_id,
            worker_database=db,
        )
        settings = context._telegram_settings()
        assert int(settings.account_id) == account_id
        assert int(settings.expected_account_id) == account_id
        assert settings.session_name == f"account_{account_id}"
        resolved[account_id] = settings.session_name

    assert len(set(resolved.values())) == 70


@pytest.mark.asyncio
async def test_floodwait_health_check_defers_without_dropping_authorization():
    """A health check under FloodWait must not be mistaken for account logout."""
    manager, database = _manager(account_ids=(ACCOUNT_ID,), cooldown=180)
    health_calls = 0

    async def create(
        account_id: int,
        *,
        publish_runtime_state: bool = True,
    ) -> TelegramAccountRuntime:
        nonlocal health_calls

        async def health(_task):
            nonlocal health_calls
            health_calls += 1
            return {"ok": True}

        return TelegramAccountRuntime(
            account_id=account_id,
            handlers={"telegram_health": health},
            cleanup=None,
            lock=asyncio.Lock(),
            last_used=time.monotonic(),
        )

    manager._create_runtime = create  # type: ignore[method-assign]

    with pytest.raises(DeferredTelegramError) as raised:
        await manager.check_runtime(ACCOUNT_ID)

    assert raised.value.code == "account_flood_wait"
    assert raised.value.retry_after == 180
    assert health_calls == 0
    assert database.accounts[ACCOUNT_ID]["authorized"] is True
    assert database.accounts[ACCOUNT_ID]["stopped"] is False

    await manager.close()


def test_healthy_session_under_sqlite_lock_is_not_quarantined(tmp_path):
    """A temporary Windows SQLite lock must not be interpreted as corrupt auth."""
    session_file = tmp_path / f"account_{ACCOUNT_ID}.session"
    connection = sqlite3.connect(session_file)
    try:
        connection.execute("CREATE TABLE proof(value INTEGER)")
        connection.execute("INSERT INTO proof(value) VALUES(1)")
        connection.commit()
        connection.execute("BEGIN EXCLUSIVE")

        TelegramService._prepare_session_file(session_file)

        assert session_file.exists()
        assert list(tmp_path.glob(f"{session_file.name}.corrupt.*")) == []
    finally:
        connection.rollback()
        connection.close()


def test_purging_one_account_session_never_touches_another_account(tmp_path):
    """Logout/delete cleanup must stay scoped to the exact session base."""
    first = tmp_path / f"account_{ACCOUNT_ID}.session"
    second = tmp_path / f"account_{OTHER_ACCOUNT_ID}.session"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    first_sidecars = [Path(f"{first}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
    second_sidecars = [
        Path(f"{second}{suffix}") for suffix in ("-wal", "-shm", "-journal")
    ]
    for path in first_sidecars:
        path.write_bytes(b"first-sidecar")
    for path in second_sidecars:
        path.write_bytes(b"second-sidecar")

    TelegramService.purge_session_artifacts(first)

    assert not first.exists()
    assert all(not path.exists() for path in first_sidecars)
    assert second.read_bytes() == b"second"
    assert all(path.read_bytes() == b"second-sidecar" for path in second_sidecars)

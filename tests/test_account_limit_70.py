from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from core.account_limits import (
    MAX_ACTIVE_TELEGRAM_ACCOUNT_RUNTIMES,
    MAX_CONCURRENT_TELEGRAM_ACCOUNT_TASKS,
    MAX_PARALLEL_ACCOUNT_RUNTIMES,
    MAX_REGISTERED_TELEGRAM_ACCOUNTS,
)
from core.secret_store import SecretStore
from services.account_runtime_manager import TelegramAccountRuntimeManager
from storage.database import Database
from storage.db_common import DatabaseError


def _register(db: Database, account_id: int):
    return db.register_telegram_account(
        telegram_account_id=account_id,
        session_name=f"account_{account_id}",
        display_name=f"Account {account_id}",
    )


def test_limits_are_distinct() -> None:
    assert MAX_REGISTERED_TELEGRAM_ACCOUNTS == 70
    assert MAX_ACTIVE_TELEGRAM_ACCOUNT_RUNTIMES == 70
    assert MAX_CONCURRENT_TELEGRAM_ACCOUNT_TASKS == 5
    assert MAX_PARALLEL_ACCOUNT_RUNTIMES == MAX_CONCURRENT_TELEGRAM_ACCOUNT_TASKS
    assert TelegramAccountRuntimeManager.MAX_ACCOUNTS == 70


def test_existing_v31_database_replaces_the_five_account_trigger(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v31-account-limit.db"
    db = Database(path)
    with db.get_connection() as conn:
        conn.execute("DROP TRIGGER IF EXISTS telegram_accounts_limit_insert")
        conn.execute(
            """CREATE TRIGGER telegram_accounts_limit_insert
               BEFORE INSERT ON telegram_accounts
               WHEN (SELECT COUNT(*) FROM telegram_accounts) >= 5
               BEGIN
                   SELECT RAISE(ABORT, 'telegram account limit reached');
               END"""
        )
        conn.execute("DELETE FROM migrations WHERE version IN (32, 33)")
        conn.execute("PRAGMA user_version = 31")
    db.close_thread_connection()

    migrated = Database(path)
    assert migrated.get_version() == 33
    for account_id in range(1, 71):
        _register(migrated, account_id)
    try:
        _register(migrated, 71)
    except DatabaseError as exc:
        assert "не более 70" in str(exc)
    else:  # pragma: no cover - database trigger is the final authority
        raise AssertionError("The seventy-first account was accepted")


def test_existing_account_can_be_updated_at_capacity(tmp_path: Path) -> None:
    db = Database(tmp_path / "capacity-update.db")
    for account_id in range(1, 71):
        _register(db, account_id)
    row, created = db.register_telegram_account(
        telegram_account_id=1,
        session_name="account_1",
        display_name="Updated",
    )
    assert created is False
    assert row["display_name"] == "Updated"
    assert db.count_telegram_accounts() == 70


def test_delete_then_add_reopens_one_slot(tmp_path: Path) -> None:
    db = Database(tmp_path / "delete-add.db")
    for account_id in range(1, 71):
        _register(db, account_id)
    result = db.delete_telegram_account_data(35)
    assert result["remaining_accounts"] == 69
    _register(db, 71)
    assert db.count_telegram_accounts() == 70
    assert db.get_telegram_account(34) is not None
    assert db.get_telegram_account(36) is not None


def test_last_slot_is_atomic_under_concurrent_registration(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-last-slot.db"
    db = Database(path)
    for account_id in range(1, 70):
        _register(db, account_id)
    barrier = threading.Barrier(2)
    connections = (Database(path), Database(path))

    def attempt(request: tuple[Database, int]) -> str:
        local, account_id = request
        barrier.wait(timeout=5)
        try:
            _register(local, account_id)
        except DatabaseError as exc:
            assert "не более 70" in str(exc)
            return "limited"
        finally:
            local.finalize_shutdown()
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, zip(connections, (70, 71), strict=True)))
    assert sorted(results) == ["created", "limited"]
    assert db.count_telegram_accounts() == 70
    assert db.health_check()["quick_check"] == "ok"


def test_secret_store_capacity_covers_seventy_accounts(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / ".secrets.json")
    payload = {
        f"account.{account_id}.{key}": f"value-{account_id}-{key}"
        for account_id in range(1, 71)
        for key in (
            "telegram.api_hash",
            "telegram.phone",
            "telegram.proxy_username",
            "telegram.proxy_password",
            "openai.api_key",
        )
    }
    assert len(payload) == 350
    store.replace_snapshot(payload)
    assert store.export_snapshot() == payload

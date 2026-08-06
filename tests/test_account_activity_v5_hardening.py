from __future__ import annotations

import ast
import asyncio
import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools" / "account_activity_runner.py"
POWERSHELL = ROOT / "RUN_ACCOUNT_ACTIVITY_EXPERIMENTAL.ps1"
MIGRATION_V34 = ROOT / "storage" / "migrations" / "account_activity_leases_v34.py"
MIGRATION_V35 = ROOT / "storage" / "migrations" / "account_activity_lease_fk_v35.py"


def _load_supervisor():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "run_session_with_lease_supervision"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias("annotations")], level=0),
            ast.Import(names=[ast.alias("asyncio")]),
            ast.ImportFrom(
                module="contextlib", names=[ast.alias("suppress")], level=0
            ),
            function,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace: dict[str, object] = {}
    exec(compile(module, str(RUNNER), "exec"), namespace)
    return namespace


@pytest.mark.asyncio
async def test_lost_lease_cancels_session_immediately() -> None:
    namespace = _load_supervisor()
    session_cancelled = asyncio.Event()

    async def fake_session(**_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            session_cancelled.set()

    namespace["execute_session"] = fake_session

    class Policy:
        account_id = 101

    async def failed_heartbeat() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("lease lost")

    lease_task = asyncio.create_task(failed_heartbeat())
    with pytest.raises(RuntimeError, match="lease lost"):
        await namespace["run_session_with_lease_supervision"](
            policy=Policy(),
            account_db=object(),
            telegram=object(),
            rng=object(),
            lease_task=lease_task,
            lease_stop=asyncio.Event(),
        )
    assert session_cancelled.is_set()


@pytest.mark.asyncio
async def test_completed_session_stops_and_awaits_heartbeat() -> None:
    namespace = _load_supervisor()
    stop_seen = asyncio.Event()

    async def fake_session(**_kwargs):
        return {"ok": True}

    namespace["execute_session"] = fake_session
    lease_stop = asyncio.Event()

    async def heartbeat() -> None:
        await lease_stop.wait()
        stop_seen.set()

    class Policy:
        account_id = 101

    result = await namespace["run_session_with_lease_supervision"](
        policy=Policy(),
        account_db=object(),
        telegram=object(),
        rng=object(),
        lease_task=asyncio.create_task(heartbeat()),
        lease_stop=lease_stop,
    )
    assert result == {"ok": True}
    assert stop_seen.is_set()


def test_dry_run_branch_precedes_telegram_construction() -> None:
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    async_main = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_main"
    )
    telegram_lines = [
        node.lineno
        for node in ast.walk(async_main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "TelegramService"
    ]
    dry_return_lines = [
        node.lineno
        for node in ast.walk(async_main)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and node.value.value == 0
    ]
    assert telegram_lines
    assert dry_return_lines
    assert min(dry_return_lines) < min(telegram_lines)
    source = RUNNER.read_text(encoding="utf-8")
    assert '"telegram_rpc_performed": False' in source


def test_mutating_ledger_reservations_are_persisted_before_dispatch() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    assert source.index("ledger.record_message(rule.peer") < source.index(
        "await telegram.send_message("
    )
    assert source.index("ledger.record_join_attempt(target") < source.index(
        "joined = await telegram.join(target)"
    )
    first_reaction_record = source.index("ledger.record_reaction(rule.peer")
    first_reaction_send = source.index("await send_reaction_once(")
    assert first_reaction_record < first_reaction_send


def test_powershell_selects_python_before_single_runner_invocation() -> None:
    source = POWERSHELL.read_text(encoding="utf-8")
    assert source.count("@runnerArgs") == 1
    assert "if ($LASTEXITCODE -eq 0) { exit 0 }" not in source
    assert "Never retry a failed" in source


def _load_migration_module(path: Path, name: str):
    # Load the migration against stdlib sqlite without leaking the temporary
    # driver into the rest of the pytest process. Later project tests must still
    # see the real SQLCipher compatibility module.
    driver_name = "storage.sqlcipher_driver"
    previous_driver = sys.modules.get(driver_name)
    driver = types.ModuleType(driver_name)
    driver.dbapi = sqlite3
    sys.modules[driver_name] = driver
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_driver is None:
            sys.modules.pop(driver_name, None)
        else:
            sys.modules[driver_name] = previous_driver


def test_v35_repairs_old_lease_table_and_cascades_account_delete(tmp_path) -> None:
    path = tmp_path / "migration.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE migrations(version INTEGER PRIMARY KEY);
        CREATE TABLE telegram_accounts(
            telegram_account_id INTEGER PRIMARY KEY
        );
        CREATE TABLE account_activity_leases(
            account_id INTEGER PRIMARY KEY,
            activity TEXT NOT NULL,
            owner_token TEXT NOT NULL,
            started_at DATETIME NOT NULL,
            heartbeat_at DATETIME NOT NULL,
            lease_until DATETIME NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        INSERT INTO telegram_accounts VALUES(101);
        INSERT INTO account_activity_leases VALUES(
            101, 'warmup', 'owner', CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP, datetime('now', '+30 minutes'), '{}'
        );
        PRAGMA user_version=34;
        """
    )
    conn.close()

    module = _load_migration_module(MIGRATION_V35, "activity_v35")
    module.migrate_account_activity_lease_fk_v35(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    fk = conn.execute("PRAGMA foreign_key_list(account_activity_leases)").fetchall()
    assert any(
        row["table"] == "telegram_accounts"
        and row["from"] == "account_id"
        and row["on_delete"].upper() == "CASCADE"
        for row in fk
    )
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 35
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM telegram_accounts WHERE telegram_account_id=101")
    assert conn.execute("SELECT COUNT(*) FROM account_activity_leases").fetchone()[0] == 0
    conn.close()



def test_v35_prunes_orphan_even_when_fk_definition_already_exists(tmp_path) -> None:
    path = tmp_path / "orphan.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;
        CREATE TABLE migrations(version INTEGER PRIMARY KEY);
        CREATE TABLE telegram_accounts(
            telegram_account_id INTEGER PRIMARY KEY
        );
        CREATE TABLE account_activity_leases(
            account_id INTEGER PRIMARY KEY,
            activity TEXT NOT NULL CHECK(activity IN ('warmup')),
            owner_token TEXT NOT NULL,
            started_at DATETIME NOT NULL,
            heartbeat_at DATETIME NOT NULL,
            lease_until DATETIME NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(account_id)
                REFERENCES telegram_accounts(telegram_account_id)
                ON DELETE CASCADE
        );
        INSERT INTO account_activity_leases VALUES(
            999, 'warmup', 'owner', CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP, datetime('now', '+30 minutes'), '{}'
        );
        PRAGMA user_version=34;
        """
    )
    conn.close()

    module = _load_migration_module(MIGRATION_V35, "activity_v35_orphan")
    module.migrate_account_activity_lease_fk_v35(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )

    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT COUNT(*) FROM account_activity_leases"
    ).fetchone()[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 35
    conn.close()

def test_v34_and_v35_fresh_migrations_are_idempotent(tmp_path) -> None:
    path = tmp_path / "fresh.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE migrations(version INTEGER PRIMARY KEY);
        CREATE TABLE telegram_accounts(
            telegram_account_id INTEGER PRIMARY KEY
        );
        PRAGMA user_version=33;
        """
    )
    conn.close()

    v34 = _load_migration_module(MIGRATION_V34, "activity_v34_fresh")
    v35 = _load_migration_module(MIGRATION_V35, "activity_v35_fresh")
    v34.migrate_account_activity_leases_v34(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    v35.migrate_account_activity_lease_fk_v35(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    # A second v35 invocation must be harmless for recovery/retry paths.
    v35.migrate_account_activity_lease_fk_v35(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    versions = [row[0] for row in conn.execute("SELECT version FROM migrations ORDER BY version")]
    assert versions == [34, 35]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 35
    fk = conn.execute("PRAGMA foreign_key_list(account_activity_leases)").fetchall()
    assert len(fk) == 1
    conn.close()

GITIGNORE = ROOT / ".gitignore"
FACTORY_RESET_RUNTIME = ROOT / "core" / "factory_reset_runtime.py"




def test_activity_config_is_validated_as_private_regular_file() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    assert "validate_private_regular_file(" in source
    assert "max_bytes=MAX_ACTIVITY_CONFIG_BYTES" in source
    assert "harden=True" in source

def test_runner_uses_project_paginator_and_strict_ledger() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    assert "telegram._iter_with_timeout(iterator)" in source
    assert "strict=True" in source


def test_launcher_prefers_real_windows_venv_and_probes_dependencies() -> None:
    source = POWERSHELL.read_text(encoding="utf-8")
    assert ".venv-windows-x64\\Scripts\\python.exe" in source
    assert "import tools.account_activity_runner" in source
    assert source.count("@runnerArgs") == 1


def test_runtime_config_is_ignored_and_factory_reset_requires_lease_table() -> None:
    assert "account_activity.json" in GITIGNORE.read_text(encoding="utf-8")
    reset_source = FACTORY_RESET_RUNTIME.read_text(encoding="utf-8")
    assert '"account_activity_leases"' in reset_source


def test_cleanup_cannot_skip_lease_or_instance_release_after_disconnect_failure() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    disconnect = source.index("await telegram.disconnect()")
    release = source.index("database.release_account_activity_lease(")
    instance_close = source.index("instance.close()", release)
    assert disconnect < release < instance_close
    assert "with suppress(asyncio.CancelledError, Exception):\n                    await telegram.disconnect()" in source
    assert "finally:\n            if instance is not None:" in source
    assert "Could not release the process instance lock" in source


def test_fresh_database_bootstraps_activity_schema_and_cascade(tmp_path) -> None:
    from storage.database import Database

    database = Database(tmp_path / "fresh.db")
    account, created = database.register_telegram_account(
        telegram_account_id=101,
        session_name="account_101",
        display_name="Test Account",
        authorized=True,
    )
    assert created is True
    assert account["telegram_account_id"] == 101
    assert database.get_version() == 35
    with database.get_connection() as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='account_activity_leases'"
        ).fetchone()
        assert table is not None
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(account_activity_leases)"
        ).fetchall()
        assert any(
            str(row["table"]) == "telegram_accounts"
            and str(row["from"]) == "account_id"
            and str(row["on_delete"]).upper() == "CASCADE"
            for row in foreign_keys
        )
        assert str(connection.execute("PRAGMA quick_check").fetchone()[0]).lower() == "ok"
    database.acquire_account_activity_lease(101, owner_token="a" * 32)
    database.delete_telegram_account_data(101)
    assert database.get_account_activity_lease(101) is None


def test_real_database_enforces_both_directions_of_exclusion(tmp_path) -> None:
    from storage.database import Database
    from storage.db_account_activity import (
        CAMPAIGN_WARMUP_CONFLICT_MESSAGE,
        WARMUP_CAMPAIGN_CONFLICT_MESSAGE,
    )
    from storage.db_common import DatabaseError

    warmup_first = Database(tmp_path / "warmup-first.db")
    warmup_first.register_telegram_account(
        telegram_account_id=101,
        session_name="account_101",
        display_name="Test Account",
        authorized=True,
    )
    warmup_first.acquire_account_activity_lease(101, owner_token="a" * 32)
    with pytest.raises(DatabaseError, match=WARMUP_CAMPAIGN_CONFLICT_MESSAGE):
        warmup_first.create_comment_campaign(
            ["text"], slot_count=1, account_id=101
        )
    with pytest.raises(DatabaseError, match=WARMUP_CAMPAIGN_CONFLICT_MESSAGE):
        warmup_first.create_join_campaign(101)

    campaign_first = Database(tmp_path / "campaign-first.db")
    campaign_first.register_telegram_account(
        telegram_account_id=202,
        session_name="account_202",
        display_name="Test Account 2",
        authorized=True,
    )
    campaign_first.create_comment_campaign(
        ["text"], slot_count=1, account_id=202
    )
    with pytest.raises(DatabaseError, match=CAMPAIGN_WARMUP_CONFLICT_MESSAGE):
        campaign_first.acquire_account_activity_lease(
            202, owner_token="b" * 32
        )


def test_example_config_cannot_run_before_operator_sets_account_id() -> None:
    import json

    example = json.loads((ROOT / "account_activity.example.json").read_text(encoding="utf-8"))
    assert example["account_id"] == 0


def _load_safety_helpers():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    wanted = {
        "require_account_rpc_ready",
        "persist_account_safety_outcome",
    }
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    assert {node.name for node in functions} == wanted
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias("annotations")], level=0),
            ast.ImportFrom(module="datetime", names=[ast.alias("timedelta")], level=0),
            *functions,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace: dict[str, object] = {}
    exec(compile(module, str(RUNNER), "exec"), namespace)
    return namespace


def test_reaction_uses_standard_fail_closed_transport_and_send_pacing() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    transport = (ROOT / "services" / "telegram" / "transport.py").read_text(
        encoding="utf-8"
    )
    limiter = (ROOT / "core" / "rate_limiter.py").read_text(encoding="utf-8")
    reaction_start = runner.index("async def send_reaction_once(")
    reaction_end = runner.index("\n\nasync def execute_session(", reaction_start)
    reaction_body = runner[reaction_start:reaction_end]
    assert "await telegram.execute(" in reaction_body
    assert "retry_network=False" in reaction_body
    assert 'unknown_result_code="reaction_result_unknown"' in reaction_body
    assert "await telegram.client(request)" not in reaction_body
    assert "SendReactionRequest" in transport
    assert "SendReactionRequest" in limiter


def test_persisted_flood_wait_is_enforced_and_reanchored(tmp_path) -> None:
    from core.boot_clock import current_boot_identity, steady_time
    from core.campaign_schedule import utc_now
    from datetime import timedelta
    from storage.database import Database
    from workers.flood_wait_guard import (
        persist_account_flood_wait,
        persisted_account_flood_wait_remaining,
    )

    database = Database(tmp_path / "cooldown.db")
    database.register_telegram_account(
        telegram_account_id=101,
        session_name="account_101",
        display_name="Test Account",
        authorized=True,
    )
    persist_account_flood_wait(
        worker_db=database,
        account_id=101,
        retry_at=utc_now() + timedelta(seconds=120),
        code="flood_wait_deferred",
        wait_seconds=120,
    )
    remaining = persisted_account_flood_wait_remaining(
        worker_db=database, account_id=101
    )
    assert 100 <= remaining <= 120

    with database.get_connection() as connection:
        connection.execute(
            """UPDATE account_rpc_cooldowns
               SET boot_id='previous-boot', steady_deadline=1,
                   fallback_wait_seconds=120
               WHERE account_id=101"""
        )
    reanchored = persisted_account_flood_wait_remaining(
        worker_db=database, account_id=101
    )
    assert 100 <= reanchored <= 120
    row = database.get_account_rpc_cooldown(account_id=101)
    assert row["boot_id"] == current_boot_identity()
    assert float(row["steady_deadline"]) > steady_time()


def test_runner_blocks_before_telegram_when_persisted_flood_wait_exists() -> None:
    namespace = _load_safety_helpers()
    namespace["persisted_account_flood_wait_remaining"] = (
        lambda **_kwargs: 87
    )
    with pytest.raises(RuntimeError, match="87 сек"):
        namespace["require_account_rpc_ready"](object(), 101)


def test_runner_persists_flood_wait_before_releasing_lease() -> None:
    namespace = _load_safety_helpers()

    class Deferred(Exception):
        code = "flood_wait_deferred"
        retry_after = 180

    class NonRetryable(Exception):
        pass

    calls: list[dict[str, object]] = []
    namespace.update(
        DeferredTelegramError=Deferred,
        NonRetryableTelegramError=NonRetryable,
        RESTRICTION_CODES=frozenset(),
        utc_now=lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
        persist_account_flood_wait=lambda **kwargs: calls.append(kwargs),
        activate_account_restriction=lambda *_args, **_kwargs: {},
    )
    namespace["persist_account_safety_outcome"](
        database=object(), account_id=101, exc=Deferred("wait")
    )
    assert len(calls) == 1
    assert calls[0]["account_id"] == 101
    assert calls[0]["wait_seconds"] == 180
    assert calls[0]["source_task_id"] is None


def test_runner_persists_restricted_and_authorization_states() -> None:
    namespace = _load_safety_helpers()

    class Deferred(Exception):
        pass

    class NonRetryable(Exception):
        def __init__(self, code: str):
            super().__init__(code)
            self.code = code
            self.details = {"rpc_error": "TestError"}

    activated: list[dict[str, object]] = []

    class DatabaseStub:
        def __init__(self) -> None:
            self.states: list[tuple[int, str, str | None]] = []

        def set_account_runtime_state(
            self, account_id: int, state: str, *, error: str | None = None
        ) -> None:
            self.states.append((account_id, state, error))

    database = DatabaseStub()
    namespace.update(
        DeferredTelegramError=Deferred,
        NonRetryableTelegramError=NonRetryable,
        RESTRICTION_CODES=frozenset({"peer_flood"}),
        persist_account_flood_wait=lambda **_kwargs: None,
        activate_account_restriction=lambda *_args, **kwargs: activated.append(kwargs),
    )
    namespace["persist_account_safety_outcome"](
        database=database, account_id=101, exc=NonRetryable("peer_flood")
    )
    assert activated and activated[0]["account_id"] == 101
    assert database.states[-1][1] == "restricted"

    namespace["persist_account_safety_outcome"](
        database=database,
        account_id=101,
        exc=NonRetryable("authorization_required"),
    )
    assert database.states[-1][1] == "authorization_required"

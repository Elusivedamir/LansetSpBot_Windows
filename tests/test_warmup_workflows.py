from __future__ import annotations

import importlib.util
import sqlite3
import sys
import threading
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from core.warmup_planner import build_week_plan, generate_profile, validate_plan
from storage.db_warmup import WarmupRepositoryMixin
from workers.handler_registry import _warmup_contact_phone_provider

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "storage" / "migrations" / "warmup_workflows_v36.py"


def _load_migration():
    driver_name = "storage.sqlcipher_driver"
    previous = sys.modules.get(driver_name)
    driver = types.ModuleType(driver_name)
    driver.dbapi = sqlite3
    sys.modules[driver_name] = driver
    try:
        spec = importlib.util.spec_from_file_location("warmup_v36", MIGRATION)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop(driver_name, None)
        else:
            sys.modules[driver_name] = previous


def _base_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE migrations(version INTEGER PRIMARY KEY);
        CREATE TABLE telegram_accounts(
            telegram_account_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            username TEXT,
            phone_masked TEXT,
            authorized INTEGER NOT NULL DEFAULT 1,
            stopped INTEGER NOT NULL DEFAULT 0,
            runtime_state TEXT NOT NULL DEFAULT 'connected'
        );
        CREATE TABLE tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL DEFAULT 0,
            type TEXT NOT NULL,
            payload TEXT,
            status TEXT DEFAULT 'pending',
            progress INTEGER DEFAULT 0,
            status_text TEXT,
            error TEXT,
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3,
            not_before DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE comment_campaigns(
            id INTEGER PRIMARY KEY,
            account_id INTEGER,
            status TEXT
        );
        CREATE TABLE join_campaigns(
            id INTEGER PRIMARY KEY,
            account_id INTEGER,
            status TEXT
        );
        INSERT INTO telegram_accounts VALUES(101,'A','a','',1,0,'connected');
        INSERT INTO telegram_accounts VALUES(102,'B','b','',1,0,'connected');
        INSERT INTO telegram_accounts VALUES(103,'C','c','',1,0,'connected');
        PRAGMA user_version=35;
        """
    )
    conn.close()


def test_profile_is_deterministic_but_pairs_can_differ() -> None:
    first = generate_profile("a" * 32)
    repeated = generate_profile("a" * 32)
    second = generate_profile("b" * 32)
    assert first == repeated
    assert first.day_order != second.day_order or first.reply_min_seconds != second.reply_min_seconds
    assert sorted(first.day_order) == sorted(second.day_order)
    assert len(first.day_order) == 7


def test_week_plan_is_ordered_and_contains_dialogues_and_group_visits() -> None:
    profile = generate_profile("c" * 32)
    steps = build_week_plan(
        account_a_id=101,
        account_b_id=102,
        week_number=1,
        profile=profile,
        start_at=datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc),
    )
    validate_plan(steps)
    assert [step.sequence_no for step in steps] == list(range(1, len(steps) + 1))
    assert {step.day_number for step in steps} == set(range(1, 8))
    assert sum(step.action == "ensure_contact" for step in steps) == 2
    assert sum(step.action == "message" for step in steps) == 70
    assert 0 <= sum(step.action == "private_reaction" for step in steps) <= 7
    assert sum(step.action == "group_visit" for step in steps) == 7 * profile.group_visits_per_day
    assert all(1 <= step.typing_seconds <= 12 for step in steps if step.action == "message")


def test_v36_migration_is_idempotent_and_enforces_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / "warmup.db"
    _base_database(path)
    module = _load_migration()
    for _ in range(2):
        module.migrate_warmup_workflows_v36(
            path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
        )
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 36
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'warmup_%'"
        )
    }
    assert {
        "warmup_pairs",
        "warmup_accounts",
        "warmup_groups",
        "warmup_group_accounts",
        "warmup_steps",
    } <= names
    versions = [row[0] for row in conn.execute("SELECT version FROM migrations")]
    assert versions == [36]
    conn.close()


class _Repository(WarmupRepositoryMixin):
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _active_campaign_in_transaction(_conn, _account_id: int):
        return None


def _profile_and_steps(a: int, b: int, seed: str):
    profile = generate_profile(seed)
    steps = build_week_plan(
        account_a_id=a,
        account_b_id=b,
        week_number=1,
        profile=profile,
        start_at=datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc),
    )
    return profile.to_record(), [step.to_record() for step in steps]


def test_pair_creation_is_serialized_per_account(tmp_path: Path) -> None:
    path = tmp_path / "race.db"
    _base_database(path)
    migration = _load_migration()
    migration.migrate_warmup_workflows_v36(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    repository = _Repository(path)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def create(a: int, b: int, seed: str) -> None:
        profile, steps = _profile_and_steps(a, b, seed)
        barrier.wait()
        try:
            repository.create_warmup_pair(
                account_a_id=a,
                account_b_id=b,
                profile=profile,
                steps=steps,
                owner_token_a="a" * 32,
                owner_token_b="b" * 32,
                started_at=steps[0]["scheduled_at"],
                ends_at=steps[-1]["scheduled_at"],
            )
        except Exception as exc:
            result = f"failed:{exc}"
        else:
            result = "created"
        with lock:
            outcomes.append(result)

    threads = [
        threading.Thread(target=create, args=(101, 102, "1" * 32)),
        threading.Thread(target=create, args=(101, 103, "2" * 32)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert outcomes.count("created") == 1
    assert sum(value.startswith("failed:") for value in outcomes) == 1
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM warmup_pairs").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM warmup_accounts WHERE status='active'"
    ).fetchone()[0] == 2
    conn.close()


def test_gui_has_no_json_editor_and_reuses_theme_object_names() -> None:
    source = (ROOT / "gui" / "views" / "warmup_view.py").read_text(encoding="utf-8")
    assert "QPlainTextEdit" not in source
    assert "JSON" not in source
    assert 'setObjectName("card")' in source
    assert 'setObjectName("proxyToggle")' in source
    assert 'setObjectName("proxyCard")' in source


def test_transfer_does_not_touch_session_or_proxy_settings() -> None:
    source = (ROOT / "storage" / "db_warmup.py").read_text(encoding="utf-8")
    start = source.index("    def transfer_warmup_account")
    end = source.index("    def is_account_in_active_warmup", start)
    section = source[start:end]
    assert "account_settings" not in section
    assert "session_name" not in section
    assert "proxy" not in section


def test_warmup_tasks_are_non_idempotent_and_scheduled_without_long_worker_sleep() -> None:
    api = (ROOT / "services" / "api.py").read_text(encoding="utf-8")
    queue = (ROOT / "workers" / "queue_worker.py").read_text(encoding="utf-8")
    repository = (ROOT / "storage" / "db_warmup.py").read_text(encoding="utf-8")
    handler = (ROOT / "workers" / "handlers" / "warmup_step.py").read_text(encoding="utf-8")
    # Internal scheduler task: handled by the worker but not exposed through
    # the generic public create_task allow-list.
    assert '"warmup_step"' not in api
    assert '"warmup_step"' in queue
    assert "not_before" in repository
    assert "safe_sleep(" in handler
    assert "typing_seconds" in handler
    assert "ImportContactsRequest" in handler
    assert "SendReactionRequest" in handler
    assert "await asyncio.sleep(60" not in handler


def _create_running_first_step(repository: _Repository, seed: str = "d" * 32):
    profile, steps = _profile_and_steps(101, 102, seed)
    pair = repository.create_warmup_pair(
        account_a_id=101,
        account_b_id=102,
        profile=profile,
        steps=steps,
        owner_token_a="a" * 32,
        owner_token_b="b" * 32,
        started_at=steps[0]["scheduled_at"],
        ends_at=steps[-1]["scheduled_at"],
    )
    queued = repository.enqueue_warmup_step(int(pair["id"]))
    assert queued is not None
    with repository.get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET status='running' WHERE id=?",
            (int(queued["queue_task_id"]),),
        )
    begun = repository.begin_warmup_step(
        int(queued["id"]), account_id=int(queued["actor_account_id"])
    )
    assert begun is not None
    return pair, queued


def test_unknown_result_is_rescheduled_after_five_minutes_without_pair_pause(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unknown-retry.db"
    _base_database(path)
    migration = _load_migration()
    migration.migrate_warmup_workflows_v36(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    repository = _Repository(path)
    pair, queued = _create_running_first_step(repository)
    old_task_id = int(queued["queue_task_id"])

    retry = repository.reschedule_warmup_step_after_unknown(
        int(queued["id"]), delay_seconds=5 * 60
    )

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    step = conn.execute(
        "SELECT status,queue_task_id,result_text FROM warmup_steps WHERE id=?",
        (int(queued["id"]),),
    ).fetchone()
    pair_row = conn.execute(
        "SELECT status,last_error FROM warmup_pairs WHERE id=?", (int(pair["id"]),)
    ).fetchone()
    task = conn.execute(
        """SELECT status,not_before,
                  CASE WHEN not_before>=datetime('now','+295 seconds') THEN 1 ELSE 0 END AS delayed
           FROM tasks WHERE id=?""",
        (int(retry["task_id"]),),
    ).fetchone()
    conn.close()

    assert int(retry["task_id"]) != old_task_id
    assert step["status"] == "pending"
    assert int(step["queue_task_id"]) == int(retry["task_id"])
    assert "5 минут" in str(step["result_text"])
    assert pair_row["status"] == "running"
    assert pair_row["last_error"] is None
    assert task["status"] == "pending"
    assert task["not_before"]
    assert int(task["delayed"]) == 1


def test_crash_recovery_reschedules_running_step_instead_of_pausing_pair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "crash-retry.db"
    _base_database(path)
    migration = _load_migration()
    migration.migrate_warmup_workflows_v36(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    repository = _Repository(path)
    pair, queued = _create_running_first_step(repository, "e" * 32)

    assert repository.recover_stale_warmup_steps() == 1
    assert repository.recover_stale_warmup_steps() == 0

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    step = conn.execute(
        "SELECT status,queue_task_id,result_text FROM warmup_steps WHERE id=?",
        (int(queued["id"]),),
    ).fetchone()
    pair_row = conn.execute(
        "SELECT status,last_error FROM warmup_pairs WHERE id=?", (int(pair["id"]),)
    ).fetchone()
    task = conn.execute(
        "SELECT status,not_before FROM tasks WHERE id=?",
        (int(step["queue_task_id"]),),
    ).fetchone()
    conn.close()

    assert step["status"] == "pending"
    assert "5 минут" in str(step["result_text"])
    assert pair_row["status"] == "running"
    assert pair_row["last_error"] is None
    assert task["status"] == "pending"
    assert task["not_before"]


def test_warmup_message_retry_uses_stable_random_id_and_no_manual_stop() -> None:
    handler = (ROOT / "workers" / "handlers" / "warmup_step.py").read_text(
        encoding="utf-8"
    )
    repository = (ROOT / "storage" / "db_warmup.py").read_text(encoding="utf-8")
    assert "_stable_message_random_id" in handler
    assert "SendMessageRequest" in handler
    assert "message_random_id_duplicate" in handler
    assert "reschedule_warmup_step_after_unknown" in handler
    assert "delay_seconds=5 * 60" in handler
    assert "status='running', last_error=NULL" in repository
    assert "автоматический повтор отключён" not in repository

def test_warmup_contact_phone_provider_is_account_scoped() -> None:
    class Store:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def get_strict_optional(self, key: str) -> str | None:
            self.keys.append(key)
            return "+79990001122"

    store = Store()
    owner = types.SimpleNamespace(
        _base=types.SimpleNamespace(secret_store=store)
    )
    missing_store_owner = types.SimpleNamespace(
        _base=types.SimpleNamespace(secret_store=None)
    )

    assert _warmup_contact_phone_provider(owner, None, 0) is None
    assert (
        _warmup_contact_phone_provider(missing_store_owner, None, 101)
        is None
    )
    assert (
        _warmup_contact_phone_provider(owner, None, 101)
        == "+79990001122"
    )
    assert len(store.keys) == 1
    assert store.keys[0].endswith(".telegram.phone")


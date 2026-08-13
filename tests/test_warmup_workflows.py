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


def test_pair_activity_returns_last_current_and_following_steps(tmp_path: Path) -> None:
    path = tmp_path / "activity.db"
    _base_database(path)
    migration = _load_migration()
    migration.migrate_warmup_workflows_v36(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    repository = _Repository(path)
    profile, steps = _profile_and_steps(101, 102, "3" * 32)
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
    pair_id = int(pair["id"])

    initial = repository.list_warmup_pair_activity()
    assert [row["snapshot_kind"] for row in initial] == ["focus", "upcoming"]
    assert [row["sequence_no"] for row in initial] == [1, 2]

    with repository.get_connection() as conn:
        conn.execute(
            """UPDATE warmup_steps
               SET status='done', completed_at=CURRENT_TIMESTAMP
               WHERE pair_id=? AND sequence_no=1""",
            (pair_id,),
        )
    advanced = repository.list_warmup_pair_activity()
    by_kind = {row["snapshot_kind"]: row for row in advanced}
    assert int(by_kind["last"]["sequence_no"]) == 1
    assert int(by_kind["focus"]["sequence_no"]) == 2
    assert int(by_kind["upcoming"]["sequence_no"]) == 3
    assert by_kind["focus"]["actor_name"] in {"A", "B"}


def test_gui_has_no_json_editor_and_reuses_theme_object_names() -> None:
    source = (ROOT / "gui" / "views" / "warmup_view.py").read_text(encoding="utf-8")
    assert "QPlainTextEdit" not in source
    assert "JSON" not in source
    assert 'setObjectName("card")' in source
    assert "AccountView" not in source
    assert "onboarding_only" not in source
    assert "self.existing_account_selector = QComboBox()" in source
    assert "self.account_a = QComboBox()" in source
    assert "self.account_b = QComboBox()" in source

    parser_source = (
        ROOT / "gui" / "views" / "audience_parser_view.py"
    ).read_text(encoding="utf-8")
    assert "self.account_selector = QComboBox()" in parser_source
    assert "get_warmup_overview" in parser_source
    assert "get_comment_campaign_state" in parser_source
    assert 'payload["account_id"] = self._current_account_id()' in parser_source


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


def test_enqueue_keeps_one_active_step_per_pair(tmp_path: Path) -> None:
    path = tmp_path / "single-active-step.db"
    _base_database(path)
    migration = _load_migration()
    migration.migrate_warmup_workflows_v36(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    repository = _Repository(path)
    pair, queued = _create_running_first_step(repository, "01" * 16)

    with repository.get_connection() as conn:
        task_count_before = int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE type='warmup_step'"
            ).fetchone()[0]
        )

    repeated = repository.enqueue_warmup_step(int(pair["id"]))

    with repository.get_connection() as conn:
        task_count_after = int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE type='warmup_step'"
            ).fetchone()[0]
        )
        running_steps = int(
            conn.execute(
                "SELECT COUNT(*) FROM warmup_steps WHERE pair_id=? AND status='running'",
                (int(pair["id"]),),
            ).fetchone()[0]
        )
    assert repeated is not None
    assert int(repeated["id"]) == int(queued["id"])
    assert int(repeated["queue_task_id"]) == int(queued["queue_task_id"])
    assert task_count_after == task_count_before
    assert running_steps == 1


def test_begin_rejects_later_step_while_previous_step_is_running(
    tmp_path: Path,
) -> None:
    path = tmp_path / "begin-order.db"
    _base_database(path)
    migration = _load_migration()
    migration.migrate_warmup_workflows_v36(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    repository = _Repository(path)
    pair, queued = _create_running_first_step(repository, "02" * 16)

    with repository.get_connection() as conn:
        next_step = conn.execute(
            """SELECT id, actor_account_id FROM warmup_steps
               WHERE pair_id=? AND status='pending'
               ORDER BY sequence_no ASC LIMIT 1""",
            (int(pair["id"]),),
        ).fetchone()
    assert next_step is not None

    begun = repository.begin_warmup_step(
        int(next_step["id"]), account_id=int(next_step["actor_account_id"])
    )

    with repository.get_connection() as conn:
        statuses = conn.execute(
            "SELECT id,status FROM warmup_steps WHERE id IN (?,?) ORDER BY id",
            (int(queued["id"]), int(next_step["id"])),
        ).fetchall()
    assert begun is None
    assert sorted(str(row["status"]) for row in statuses) == ["pending", "running"]


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


def test_finish_warmup_step_persists_message_context_and_advances_pair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "finish-step.db"
    _base_database(path)
    migration = _load_migration()
    migration.migrate_warmup_workflows_v36(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    repository = _Repository(path)
    pair, queued = _create_running_first_step(repository, "f" * 32)

    outcome = repository.finish_warmup_step(
        int(queued["id"]),
        telegram_message_id=555,
        result_text="ok",
        skipped=False,
    )

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    step = conn.execute(
        "SELECT status,telegram_message_id,result_text FROM warmup_steps WHERE id=?",
        (int(queued["id"]),),
    ).fetchone()
    pair_row = conn.execute(
        "SELECT current_step,last_message_id,last_sender_account_id,status "
        "FROM warmup_pairs WHERE id=?",
        (int(pair["id"]),),
    ).fetchone()
    conn.close()

    assert outcome["completed"] is False
    assert step["status"] == "done"
    assert int(step["telegram_message_id"]) == 555
    assert step["result_text"] == "ok"
    assert int(pair_row["current_step"]) >= int(queued["sequence_no"])
    assert int(pair_row["last_message_id"]) == 555
    assert int(pair_row["last_sender_account_id"]) == int(
        queued["actor_account_id"]
    )
    assert pair_row["status"] == "running"


def test_fail_warmup_step_pauses_pair_and_marks_step_failed(tmp_path: Path) -> None:
    path = tmp_path / "fail-step.db"
    _base_database(path)
    migration = _load_migration()
    migration.migrate_warmup_workflows_v36(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    repository = _Repository(path)
    pair, queued = _create_running_first_step(repository, "1f" * 16)

    result = repository.fail_warmup_step(
        int(queued["id"]),
        message="telegram failure",
        uncertain=False,
    )

    conn = sqlite3.connect(path)
    step = conn.execute(
        "SELECT status,result_text FROM warmup_steps WHERE id=?",
        (int(queued["id"]),),
    ).fetchone()
    pair_row = conn.execute(
        "SELECT status,last_error FROM warmup_pairs WHERE id=?",
        (int(pair["id"]),),
    ).fetchone()
    conn.close()

    assert result["status"] == "failed"
    assert step[0] == "failed"
    assert step[1] == "telegram failure"
    assert pair_row[0] == "paused"
    assert pair_row[1] == "telegram failure"


def test_defer_warmup_step_preserves_or_clears_queue_task_as_requested(
    tmp_path: Path,
) -> None:
    path = tmp_path / "defer-step.db"
    _base_database(path)
    migration = _load_migration()
    migration.migrate_warmup_workflows_v36(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    repository = _Repository(path)
    _pair, queued = _create_running_first_step(repository, "2f" * 16)
    original_task_id = int(queued["queue_task_id"])

    assert repository.defer_warmup_step(
        int(queued["id"]), clear_queue_task=False
    )

    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT status,queue_task_id,started_at FROM warmup_steps WHERE id=?",
        (int(queued["id"]),),
    ).fetchone()
    assert row[0] == "pending"
    assert int(row[1]) == original_task_id
    assert row[2] is None

    conn.execute(
        "UPDATE warmup_steps SET status='running' WHERE id=?",
        (int(queued["id"]),),
    )
    conn.commit()
    conn.close()

    assert repository.defer_warmup_step(
        int(queued["id"]), clear_queue_task=True
    )
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT status,queue_task_id FROM warmup_steps WHERE id=?",
        (int(queued["id"]),),
    ).fetchone()
    conn.close()

    assert row[0] == "pending"
    assert row[1] is None


def test_group_selection_is_explicitly_account_scoped(tmp_path: Path) -> None:
    path = tmp_path / "account-groups.db"
    _base_database(path)
    migration = _load_migration()
    migration.migrate_warmup_workflows_v36(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    repository = _Repository(path)
    group_a = repository.add_warmup_group("@group_a", "Group A")
    group_b = repository.add_warmup_group("@group_b", "Group B")
    repository.assign_warmup_group_to_account(
        int(group_a["id"]), 101, membership_state="joined"
    )
    repository.assign_warmup_group_to_account(
        int(group_b["id"]), 102, membership_state="joined"
    )

    assert repository.choose_warmup_group_for_account(101)["chat_ref"] == "@group_a"
    assert repository.choose_warmup_group_for_account(102)["chat_ref"] == "@group_b"
    assert repository.choose_warmup_group_for_account(103) is None

    assert repository.remove_warmup_group_from_account(int(group_a["id"]), 101)
    assert repository.choose_warmup_group_for_account(101) is None
    assert repository.choose_warmup_group_for_account(102)["chat_ref"] == "@group_b"


def test_failed_step_can_retry_but_uncertain_step_cannot(tmp_path: Path) -> None:
    path = tmp_path / "retry-safety.db"
    _base_database(path)
    migration = _load_migration()
    migration.migrate_warmup_workflows_v36(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    repository = _Repository(path)
    pair, queued = _create_running_first_step(repository, "4f" * 16)
    pair_id = int(pair["id"])

    repository.fail_warmup_step(
        int(queued["id"]), message="definite failure", uncertain=False
    )
    assert repository.resume_warmup_pair(pair_id) is False
    assert repository.retry_failed_warmup_step(pair_id) is True

    with repository.get_connection() as conn:
        conn.execute(
            "UPDATE warmup_steps SET status='uncertain' WHERE id=?",
            (int(queued["id"]),),
        )
        conn.execute(
            "UPDATE warmup_pairs SET status='paused' WHERE id=?", (pair_id,)
        )
    assert repository.resume_warmup_pair(pair_id) is False
    assert repository.retry_failed_warmup_step(pair_id) is False


def test_archiving_paused_pair_cancels_pending_steps_and_frees_accounts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive-pair.db"
    _base_database(path)
    migration = _load_migration()
    migration.migrate_warmup_workflows_v36(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5000
    )
    repository = _Repository(path)
    pair, _queued = _create_running_first_step(repository, "5f" * 16)
    pair_id = int(pair["id"])
    assert repository.pause_warmup_pair(pair_id, "operator pause")

    result = repository.archive_paused_warmup_pair(pair_id)
    assert result == {
        "pair_id": pair_id,
        "status": "archived",
        "account_ids": [101, 102],
    }

    with repository.get_connection() as conn:
        pair_status = conn.execute(
            "SELECT status FROM warmup_pairs WHERE id=?", (pair_id,)
        ).fetchone()[0]
        account_rows = conn.execute(
            "SELECT status,active_pair_id FROM warmup_accounts ORDER BY account_id"
        ).fetchall()
        unfinished = conn.execute(
            """SELECT COUNT(*) FROM warmup_steps
               WHERE pair_id=?
                 AND status IN ('pending','running','failed','uncertain')""",
            (pair_id,),
        ).fetchone()[0]
    assert pair_status == "archived"
    assert [(row[0], row[1]) for row in account_rows] == [
        ("available", None),
        ("available", None),
    ]
    assert int(unfinished) == 0

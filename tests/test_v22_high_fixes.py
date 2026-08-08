from __future__ import annotations

import json
from datetime import timedelta

import pytest

from core.campaign_schedule import utc_now
from core.exceptions import NonRetryableTelegramError
from core.redaction import (
    sanitize_data,
    sanitize_exception,
    sanitize_json,
    sanitize_text,
)
from storage.database import Database
from workers.queue_worker import QueueWorker
from tests.conftest import open_project_database


@pytest.mark.asyncio
async def test_persisted_cooldown_survives_worker_restart_and_forward_wall_clock(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "restart-cooldown.db")
    db.set_setting("telegram.account_id", 501)
    task_id = db.insert_task("comment", {"account_id": 501})
    task = db.claim_next_pending_task()
    assert task and task["id"] == task_id
    stored = db.set_account_rpc_cooldown(
        account_id=501,
        retry_at=utc_now() + timedelta(hours=1),
        source_task_id=task_id,
        wait_seconds=3600,
    )
    assert stored["boot_id"]
    assert float(stored["steady_deadline"]) > 0
    assert int(stored["fallback_wait_seconds"]) == 3600

    # A fresh worker represents a process restart before the first cooldown read.
    # SQLite's wall-clock projection is forced to look expired, as it would after
    # a system-clock jump beyond next_allowed_at.
    real_get = db.get_account_rpc_cooldown

    def jumped_wall_clock(*, account_id):
        row = real_get(account_id=account_id)
        row["active"] = 0
        row["remaining_seconds"] = 0
        return row

    monkeypatch.setattr(db, "get_account_rpc_cooldown", jumped_wall_clock)
    called: list[int] = []

    async def handler(current):
        called.append(int(current["id"]))

    restarted = QueueWorker(lambda: {})
    restarted._db = db
    restarted._handlers = {"comment": handler}
    await restarted._process_task_impl(task)

    assert called == []
    after = db.get_task(task_id)
    assert after["status"] == "pending"
    assert after["not_before"] is not None


@pytest.mark.asyncio
async def test_cooldown_boot_change_reanchors_full_recorded_wait(tmp_path, monkeypatch):
    db = Database(tmp_path / "reboot-cooldown.db")
    db.set_setting("telegram.account_id", 502)
    task_id = db.insert_task("comment", {"account_id": 502})
    task = db.claim_next_pending_task()
    db.set_account_rpc_cooldown(
        account_id=502,
        retry_at=utc_now() + timedelta(seconds=90),
        source_task_id=task_id,
        wait_seconds=90,
    )
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE account_rpc_cooldowns SET boot_id='previous-boot', steady_deadline=1 "
            "WHERE account_id=502"
        )

    import workers.queue_parts.cooldowns as cooldown_module

    monkeypatch.setattr(cooldown_module, "current_boot_identity", lambda: "new-boot")
    monkeypatch.setattr(cooldown_module, "steady_time", lambda: 1000.0)
    called: list[int] = []

    async def handler(current):
        called.append(int(current["id"]))

    restarted = QueueWorker(lambda: {})
    restarted._db = db
    restarted._handlers = {"comment": handler}
    await restarted._process_task_impl(task)

    assert called == []
    row = db.get_account_rpc_cooldown(account_id=502)
    assert row["boot_id"] == "new-boot"
    assert float(row["steady_deadline"]) >= 1090.0
    assert int(row["fallback_wait_seconds"]) >= 90


@pytest.mark.asyncio
async def test_restriction_and_structured_exception_redact_every_persistent_sink(
    tmp_path,
):
    secret = "V22_PROXY_SECRET_42"
    session_path = "/home/alice/.local/share/marlen/sessions/main.session"
    db = Database(tmp_path / "restricted-secret.db")
    db.register_telegram_account(
        telegram_account_id=601,
        session_name="account_601",
        display_name="Restricted test account",
        authorized=True,
    )
    db.set_setting("telegram.account_id", 601)
    db.set_setting("ui.selected_account_id", 601)
    task_id = db.insert_task("comment", {"account_id": 601})
    task = db.claim_next_pending_task()
    assert task is not None

    async def restricted(_task):
        raise NonRetryableTelegramError(
            f"peer flood via password={secret}; session={session_path}",
            code="peer_flood",
            details={
                "proxy": {"password": secret, "username": "private-user"},
                "session_path": session_path,
                "nested": [{"verification_code": "12345"}],
            },
        )

    worker = QueueWorker(lambda: {})
    worker._db = db
    worker._handlers = {"comment": restricted}
    emitted: list[str] = []
    worker.task_failed.connect(lambda _task_id, message: emitted.append(str(message)))
    await worker._process_task_impl(task)

    task_error = str(db.get_task(task_id)["error"])
    restriction = db.get_account_restriction(601)
    logs = "\n".join(str(row["message"]) for row in db.get_logs(limit=50))
    details_text = json.dumps(restriction["details"], ensure_ascii=False)
    all_sinks = "\n".join(
        [task_error, str(restriction["message"]), details_text, logs, *emitted]
    )
    assert secret not in all_sinks
    assert session_path not in all_sinks
    assert "12345" not in all_sinks
    assert "private-user" not in all_sinks
    assert "<redacted>" in all_sinks

    # Clear both the durable restriction and its account runtime state before
    # exercising the unrelated generic exception sink.
    db.clear_account_restriction(account_id=601, checked_at=utc_now().isoformat())
    db.set_account_runtime_state(601, "connected")
    second_id = db.insert_task("comment", {"account_id": 601})
    second = db.claim_next_pending_task()
    assert second and second["id"] == second_id

    async def structured(_task):
        try:
            raise ValueError({"password": secret, "session": session_path})
        except ValueError as cause:
            raise RuntimeError(
                {"proxy": {"password": secret}, "session_path": session_path}
            ) from cause

    worker._handlers = {"comment": structured}
    await worker._process_task_impl(second)
    generic_error = str(db.get_task(second_id)["error"])
    assert secret not in generic_error
    assert session_path not in generic_error
    assert "<redacted>" in generic_error


def test_recursive_redaction_handles_dict_json_repr_and_session_paths():
    secret = "DICT_SECRET_9988"
    path = r"C:\\Users\\Alice\\Marlen\\main.session"
    payload = {
        "proxy": {"password": secret, "username": "alice"},
        "items": [{"session_path": path}, {"phone": "+4912345"}],
    }
    sanitized = sanitize_data(payload)
    encoded = sanitize_json(payload)
    repr_text = sanitize_text(repr(payload))
    chained = RuntimeError(payload)
    rendered = sanitize_exception(chained)
    combined = f"{sanitized}\n{encoded}\n{repr_text}\n{rendered}"
    assert secret not in combined
    assert path not in combined
    assert "+4912345" not in combined
    assert "alice" not in combined
    assert "<redacted>" in combined


def test_v26_migrates_existing_cooldown_with_conservative_fallback(tmp_path):
    path = tmp_path / "v25-cooldown.db"
    db = Database(path)
    db.set_account_rpc_cooldown(
        account_id=701,
        retry_at=utc_now() + timedelta(seconds=120),
        source_task_id=1,
        wait_seconds=120,
    )
    db.close_thread_connection()


    with open_project_database(path) as conn:
        conn.execute("DELETE FROM migrations WHERE version=26")
        conn.execute("PRAGMA user_version=25")
        # Rebuild the table in its v25 form to exercise the real ALTER migration.
        conn.executescript(
            """
            ALTER TABLE account_rpc_cooldowns RENAME TO account_rpc_cooldowns_v26;
            CREATE TABLE account_rpc_cooldowns(
                account_id INTEGER PRIMARY KEY,
                next_allowed_at DATETIME NOT NULL,
                code TEXT NOT NULL DEFAULT 'flood_wait_deferred',
                source_task_id INTEGER,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO account_rpc_cooldowns(
                account_id, next_allowed_at, code, source_task_id, updated_at
            )
            SELECT account_id, next_allowed_at, code, source_task_id, updated_at
            FROM account_rpc_cooldowns_v26;
            DROP TABLE account_rpc_cooldowns_v26;
            """
        )

    migrated = Database(path)
    assert migrated.get_version() == Database.SCHEMA_VERSION
    row = migrated.get_account_rpc_cooldown(account_id=701)
    assert "boot_id" in row
    assert "steady_deadline" in row
    assert int(row["fallback_wait_seconds"]) >= 120


def test_equal_wall_deadline_cannot_shorten_persisted_steady_guard(tmp_path):
    db = Database(tmp_path / "equal-deadline.db")
    retry_at = utc_now() + timedelta(seconds=120)
    first = db.set_account_rpc_cooldown(
        account_id=801,
        retry_at=retry_at,
        source_task_id=1,
        wait_seconds=120,
    )
    second = db.set_account_rpc_cooldown(
        account_id=801,
        retry_at=retry_at,
        source_task_id=2,
        wait_seconds=1,
    )
    assert float(second["steady_deadline"]) >= float(first["steady_deadline"])
    assert int(second["fallback_wait_seconds"]) >= 120


def test_v26_migration_scrubs_secrets_already_persisted_by_v25(tmp_path):
    secret = "LEGACY_PLAINTEXT_PROXY_77"
    session_path = "/Users/alice/Library/Application Support/Marlen/main.session"
    path = tmp_path / "legacy-secret.db"
    db = Database(path)
    task_id = db.insert_task("noop", {})
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET error=?, status_text=? WHERE id=?",
            (
                f"password={secret}; session={session_path}",
                f"proxy_username=alice; password={secret}",
                task_id,
            ),
        )
        conn.execute(
            """INSERT OR REPLACE INTO account_restrictions(
                   account_id, active, code, message, detected_at, details_json, updated_at)
               VALUES(901, 1, 'peer_flood', ?, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP)""",
            (
                f"password={secret}; session_path={session_path}",
                json.dumps(
                    {
                        "proxy": {"password": secret, "username": "alice"},
                        "session_path": session_path,
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO logs(level, message) VALUES('ERROR', ?)",
            (f"password={secret}; session={session_path}",),
        )
        conn.execute("DELETE FROM migrations WHERE version=26")
        conn.execute("PRAGMA user_version=25")
    db.close_thread_connection()

    migrated = Database(path)
    task = migrated.get_task(task_id)
    restriction = migrated.get_account_restriction(901)
    logs = "\n".join(str(row["message"]) for row in migrated.get_logs(limit=20))
    combined = "\n".join(
        [
            str(task["error"]),
            str(task["status_text"]),
            str(restriction["message"]),
            json.dumps(restriction["details"], ensure_ascii=False),
            logs,
        ]
    )
    assert migrated.get_version() == Database.SCHEMA_VERSION
    assert secret not in combined
    assert session_path not in combined
    assert "alice" not in combined
    assert "<redacted>" in combined

from __future__ import annotations

import sqlite3
from pathlib import Path

from storage.database import Database
from storage.migrations.activity_log_account_scope_v29 import (
    migrate_activity_log_account_scope_v29,
)


def test_new_database_uses_schema_v29_and_account_scoped_logs(tmp_path):
    db = Database(tmp_path / "fresh.db")
    assert db.get_version() == 30
    with db.get_connection() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(logs)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(logs)")}
    assert "account_id" in columns
    assert "idx_logs_account_id_id" in indexes


def test_activity_logs_do_not_leak_between_accounts(tmp_path):
    db = Database(tmp_path / "accounts.db")

    db.set_setting("telegram.account_id", 101)
    db.insert_log("INFO", "only account A")
    db.set_setting("telegram.account_id", 202)
    db.insert_log("INFO", "only account B")
    db.insert_log("WARNING", "explicitly A", account_id=101)

    rows_a = db.get_logs(account_id=101, limit=20)
    rows_b = db.get_logs(account_id=202, limit=20)

    assert {row["message"] for row in rows_a} == {
        "only account A",
        "explicitly A",
    }
    assert {row["account_id"] for row in rows_a} == {101}
    assert {row["message"] for row in rows_b} == {"only account B"}
    assert {row["account_id"] for row in rows_b} == {202}


def test_legacy_activity_rows_become_account_zero_and_stay_hidden(tmp_path):
    path = tmp_path / "legacy-v28.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE migrations(version INTEGER PRIMARY KEY);
            CREATE TABLE logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO logs(level, message) VALUES('INFO', 'legacy mixed row');
            PRAGMA user_version = 28;
            """
        )
        conn.commit()
    finally:
        conn.close()

    migrate_activity_log_account_scope_v29(path)

    conn = sqlite3.connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        row = conn.execute(
            "SELECT account_id, message FROM logs ORDER BY id"
        ).fetchone()
    finally:
        conn.close()

    assert version == 29
    assert row == (0, "legacy mixed row")


def test_restriction_audit_row_is_owned_by_restricted_account(tmp_path):
    db = Database(tmp_path / "restriction.db")
    db.set_setting("telegram.account_id", 101)

    db.activate_account_restriction_atomic(
        account_id=202,
        code="peer_flood",
        message="restricted B",
        details_json="{}",
        detected_at="2026-07-22 00:00:00",
        reason="stop B",
    )

    assert db.get_logs(account_id=101) == []
    rows_b = db.get_logs(account_id=202)
    assert len(rows_b) == 1
    assert rows_b[0]["account_id"] == 202
    assert "restricted B" in rows_b[0]["message"]



def test_active_link_task_is_selected_only_for_current_account(tmp_path):
    db = Database(tmp_path / "link-task.db")

    db.set_setting("telegram.account_id", 101)
    task_a = db.insert_task("link_channels", {"account_id": 101})
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', error='old A failure' WHERE id=?",
            (task_a,),
        )

    db.set_setting("telegram.account_id", 202)
    task_b = db.insert_task("link_channels", {"account_id": 202})
    active_b = db.get_active_link_task()
    assert active_b is not None
    assert int(active_b["id"]) == task_b

    db.set_setting("telegram.account_id", 101)
    assert db.get_active_link_task() is None

def test_activity_panel_uses_current_account_logs_and_active_link_task_only():
    source = (
        Path(__file__).resolve().parents[1] / "gui" / "activity_panel.py"
    ).read_text(encoding="utf-8")

    assert '"account_id": owner_account_id' in source
    assert "limit=150, account_id=owner_account_id" in source
    assert 'getattr(self.adapter, "get_active_link_task", None)' in source
    load_snapshot = source.split("def _load_snapshot", 1)[1].split(
        "def request_refresh", 1
    )[0]
    assert "if callable(link_task_getter)" in load_snapshot
    assert "compatibility with older/lightweight adapters only" in load_snapshot
    assert 'in {"pending", "running", "paused"}' in load_snapshot
    assert "current_account_id != snapshot_account_id" in source
    assert "self._reset_for_account(snapshot_account_id)" in source


def test_instruction_explains_account_switch_isolation():
    source = (
        Path(__file__).resolve().parents[1]
        / "gui"
        / "views"
        / "instructions_view.py"
    ).read_text(encoding="utf-8")

    assert "Инструкция актуальна для V46" in source
    assert "Смена Telegram-аккаунта" in source
    assert "Живой журнал» изолированы по Telegram account_id" in source
    assert "Общий сохранённый список каналов" in source

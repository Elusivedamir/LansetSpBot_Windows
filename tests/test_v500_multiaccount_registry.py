from __future__ import annotations

import json
from pathlib import Path

import pytest

from storage.database import Database
from storage.db_common import DatabaseError


def _database(tmp_path: Path) -> Database:
    return Database(tmp_path / "multiaccount.db")


def test_registers_seventy_accounts_and_rejects_seventy_first(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    for account_id in range(1001, 1071):
        row, created = db.register_telegram_account(
            telegram_account_id=account_id,
            session_name=f"account_{account_id}",
            display_name=f"Account {account_id}",
        )
        assert created is True
        assert row["telegram_account_id"] == account_id
    with pytest.raises(DatabaseError, match="не более 70"):
        db.register_telegram_account(
            telegram_account_id=1071,
            session_name="account_1071",
            display_name="Seventy first",
        )
    assert db.count_telegram_accounts() == 70


def test_selection_tracks_only_real_switches(tmp_path: Path) -> None:
    db = _database(tmp_path)
    for account_id in (2001, 2002, 2003):
        db.register_telegram_account(
            telegram_account_id=account_id,
            session_name=f"account_{account_id}",
            display_name=str(account_id),
        )
    db.select_telegram_account(2001)
    assert db.get_selected_account_id() == 2001
    assert db.get_previous_selected_account_id() == 0
    db.select_telegram_account(2001)
    assert db.get_previous_selected_account_id() == 0
    db.select_telegram_account(2002)
    assert db.get_selected_account_id() == 2002
    assert db.get_previous_selected_account_id() == 2001
    db.select_telegram_account(2003)
    assert db.get_selected_account_id() == 2003
    assert db.get_previous_selected_account_id() == 2002


def test_account_settings_are_isolated(tmp_path: Path) -> None:
    db = _database(tmp_path)
    for account_id in (3001, 3002):
        db.register_telegram_account(
            telegram_account_id=account_id,
            session_name=f"account_{account_id}",
            display_name=str(account_id),
        )
    db.set_account_settings(3001, {"telegram.proxy_host": "proxy-a"})
    db.set_account_settings(3002, {"telegram.proxy_host": "proxy-b"})
    assert db.for_account(3001).get_setting("telegram.proxy_host") == "proxy-a"
    assert db.for_account(3002).get_setting("telegram.proxy_host") == "proxy-b"
    assert db.for_account(3001).get_setting("telegram.account_id") == "3001"


def test_tasks_store_authoritative_account_column(tmp_path: Path) -> None:
    db = _database(tmp_path)
    db.register_telegram_account(
        telegram_account_id=4001,
        session_name="account_4001",
        display_name="A",
    )
    task_id = db.insert_task(
        "sync_channels",
        {"account_id": 4001},
    )
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT account_id, payload FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert row["account_id"] == 4001
    assert json.loads(row["payload"])["account_id"] == 4001


def test_comment_import_uses_explicit_source_and_resets_bag(tmp_path: Path) -> None:
    db = _database(tmp_path)
    for account_id in (5001, 5002):
        db.register_telegram_account(
            telegram_account_id=account_id,
            session_name=f"account_{account_id}",
            display_name=str(account_id),
        )
    db.save_account_comment_profile(
        ["one", "two"], visible_count=10, account_id=5001
    )
    db.save_account_comment_profile(
        ["existing"], visible_count=10, account_id=5002
    )
    result = db.import_comment_profile_between_accounts(
        source_account_id=5001,
        target_account_id=5002,
        mode="fill",
    )
    assert result["source_account_id"] == 5001
    target = db.get_account_comment_profile(5002)
    assert target["comments"][:3] == ["existing", "one", "two"]
    assert target["bag_order_json"] == "[]"
    source = db.get_account_comment_profile(5001)
    assert source["comments"][:2] == ["one", "two"]


def test_duplicate_telegram_id_does_not_create_a_second_row(tmp_path: Path) -> None:
    db = _database(tmp_path)
    first, created = db.register_telegram_account(
        telegram_account_id=6001,
        session_name="account_6001",
        display_name="First",
    )
    assert created is True
    second, created = db.register_telegram_account(
        telegram_account_id=6001,
        session_name="account_6001",
        display_name="Updated",
    )
    assert created is False
    assert first["telegram_account_id"] == second["telegram_account_id"]
    assert db.count_telegram_accounts() == 1


def test_selecting_account_clears_stale_compatibility_settings(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    for account_id in (7001, 7002):
        db.register_telegram_account(
            telegram_account_id=account_id,
            session_name=f"account_{account_id}",
            display_name=str(account_id),
        )
    db.set_account_settings(
        7001,
        {
            "telegram.proxy_host": "proxy-a",
            "openai.model": "model-a",
        },
    )
    db.set_account_settings(7002, {"telegram.proxy_host": "proxy-b"})
    db.select_telegram_account(7001)
    assert db.get_setting("openai.model") == "model-a"
    db.select_telegram_account(7002)
    assert db.get_setting("telegram.proxy_host") == "proxy-b"
    assert db.get_setting("openai.model", "") == ""


def test_stopping_one_account_cancels_only_its_pending_tasks(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    for account_id in (8001, 8002):
        db.register_telegram_account(
            telegram_account_id=account_id,
            session_name=f"account_{account_id}",
            display_name=str(account_id),
        )
    task_a = db.insert_task("sync_channels", {"account_id": 8001})
    task_b = db.insert_task("sync_channels", {"account_id": 8002})
    result = db.begin_account_stop(8001)
    assert task_a in result["task_ids"]
    assert db.get_task(task_a)["status"] == "cancelled"
    assert db.get_task(task_b)["status"] == "pending"
    assert db.get_telegram_account(8002)["stopped"] is False


def test_session_names_reject_path_traversal(tmp_path: Path) -> None:
    from services.account_sessions import session_base, validate_session_name

    with pytest.raises(ValueError, match="Unsafe"):
        validate_session_name("../main")
    with pytest.raises(ValueError, match="Unsafe"):
        session_base(tmp_path, "account_1/../../outside")

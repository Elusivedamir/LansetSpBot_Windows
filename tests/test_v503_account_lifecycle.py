from __future__ import annotations

from pathlib import Path

import pytest

from services.account_sessions import (
    finalize_pending_session,
    replace_pending_session,
    stage_account_session_removal,
)
from storage.database import Database
from storage.db_common import DatabaseError


def _session(root: Path, name: str) -> Path:
    return (root / name).with_suffix(".session")


def test_missing_pending_session_is_rejected(tmp_path: Path) -> None:
    db = Database(tmp_path / "database.db")
    with pytest.raises(RuntimeError, match="temporary Telegram session is missing"):
        finalize_pending_session(
            db,
            tmp_path / "sessions",
            pending_session_name="pending_" + "a" * 16,
            telegram_account_id=9101,
        )


def test_existing_session_swap_rolls_back_on_failure(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    pending_name = "pending_" + "b" * 16
    old_path = _session(session_dir, "account_9102")
    pending_path = _session(session_dir, pending_name)
    old_path.write_bytes(b"old-session")
    pending_path.write_bytes(b"new-session")

    with pytest.raises(RuntimeError, match="stop"):
        with replace_pending_session(
            session_dir,
            pending_session_name=pending_name,
            telegram_account_id=9102,
        ):
            assert old_path.read_bytes() == b"new-session"
            raise RuntimeError("stop")

    assert old_path.read_bytes() == b"old-session"
    assert pending_path.read_bytes() == b"new-session"


def test_staged_session_removal_restores_on_database_failure(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    live = _session(session_dir, "account_9103")
    live.write_bytes(b"authorization")

    with pytest.raises(RuntimeError, match="database"):
        with stage_account_session_removal(
            session_dir, session_name="account_9103"
        ):
            assert not live.exists()
            raise RuntimeError("database")

    assert live.read_bytes() == b"authorization"


def test_account_delete_preserves_other_accounts(tmp_path: Path) -> None:
    db = Database(tmp_path / "database.db")
    for account_id in (9201, 9202):
        db.register_telegram_account(
            telegram_account_id=account_id,
            session_name=f"account_{account_id}",
            display_name=str(account_id),
        )
    db.set_account_settings(9201, {"telegram.proxy_host": "one"})
    db.set_account_settings(9202, {"telegram.proxy_host": "two"})
    task_one = db.insert_task("sync_channels", {"account_id": 9201})
    task_two = db.insert_task("sync_channels", {"account_id": 9202})
    db.select_telegram_account(9201)

    result = db.delete_telegram_account_data(9201)

    assert result["deleted_account_id"] == 9201
    assert db.get_telegram_account(9201) is None
    assert db.get_telegram_account(9202) is not None
    assert db.get_task(task_one) is None
    assert db.get_task(task_two) is not None
    assert db.get_selected_account_id() == 9202
    assert db.get_setting("telegram.account_id") == "9202"


def test_post_commit_hardening_failure_does_not_report_write_failure(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "database.db")
    original = db._harden_database_artifacts
    calls = 0

    def fail_once(*, force: bool = False) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DatabaseError("simulated ACL failure")
        original(force=force)

    db._harden_database_artifacts = fail_once  # type: ignore[method-assign]
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?)",
            ("v503.committed", "yes"),
        )

    assert db._artifact_security_failure is not None
    assert db.get_setting("v503.committed") == "yes"
    assert db._artifact_security_failure is None


def test_first_connection_creates_a_pending_session_before_auth() -> None:
    source = Path("gui/views/account_view.py").read_text(encoding="utf-8")
    request_code = source.split("    def request_code(self):", 1)[1].split(
        "    def confirm_login(self):", 1
    )[0]
    assert "if not self._pending_session_name:" in request_code
    assert 'f"pending_{secrets.token_hex(16)}"' in request_code
    assert "self._auth_settings_snapshot = dict(settings)" in request_code

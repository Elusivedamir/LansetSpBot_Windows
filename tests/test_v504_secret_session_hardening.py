from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.account_sessions import (
    migrate_legacy_account_secrets,
    recover_interrupted_session_moves,
)
from storage.database import Database
from storage.db_common import DatabaseError


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_strict_optional(self, key: str):
        return self.values.get(key)

    def get(self, key: str, default=""):
        return self.values.get(key, default)

    def set(self, key: str, value) -> None:
        if value in (None, ""):
            self.values.pop(key, None)
        else:
            self.values[key] = str(value)

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _database(tmp_path: Path) -> Database:
    return Database(tmp_path / "secret-hardening.db")


def test_legacy_account_setting_secrets_move_to_secret_store(tmp_path: Path) -> None:
    db = _database(tmp_path)
    db.register_telegram_account(
        telegram_account_id=9001,
        session_name="account_9001",
        display_name="A",
    )
    db.select_telegram_account(9001)
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO account_settings(account_id, key, value)
               VALUES(9001, 'telegram.api_hash', 'legacy-account-copy')"""
        )
        conn.execute(
            """INSERT INTO settings(key, value)
               VALUES('telegram.phone', '+79990000000')
               ON CONFLICT(key) DO UPDATE SET value=excluded.value"""
        )

    store = MemorySecretStore()
    result = migrate_legacy_account_secrets(db, store)

    assert result["deleted"] == 2
    assert store.values["account.9001.telegram.api_hash"] == "legacy-account-copy"
    assert store.values["account.9001.telegram.phone"] == "+79990000000"
    assert db.get_account_setting(9001, "telegram.api_hash", "") == ""
    assert db.get_setting("telegram.phone", "") == ""


def test_public_account_settings_reject_secret_keys(tmp_path: Path) -> None:
    db = _database(tmp_path)
    db.register_telegram_account(
        telegram_account_id=9002,
        session_name="account_9002",
        display_name="B",
    )
    with pytest.raises(DatabaseError, match="SecretStore"):
        db.set_account_settings(9002, {"openai.api_key": "must-not-enter-sqlite"})
    with pytest.raises(DatabaseError, match="SecretStore"):
        db.replace_account_settings(
            9002, {"telegram.proxy_password": "must-not-enter-sqlite"}
        )


def test_select_account_never_projects_legacy_secret_copy(tmp_path: Path) -> None:
    db = _database(tmp_path)
    db.register_telegram_account(
        telegram_account_id=9003,
        session_name="account_9003",
        display_name="C",
    )
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO account_settings(account_id, key, value)
               VALUES(9003, 'openai.api_key', 'legacy')"""
        )
    db.select_telegram_account(9003)
    assert db.get_setting("openai.api_key", "") == ""
    assert "openai.api_key" not in db.get_account_settings(9003)


def test_session_move_journal_completes_split_family(
    tmp_path: Path, monkeypatch
) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    source_file = session_dir / "pending_0123456789abcdef.session"
    destination_file = session_dir / "account_9010.session"
    source_file.write_bytes(b"source-main")
    source_sidecar = Path(f"{source_file}-wal")
    source_sidecar.write_bytes(b"source-wal")
    source_file.replace(destination_file)

    journal = session_dir / ".session_move_0123456789abcdef01234567.json"
    journal.write_text(
        json.dumps(
            {
                "version": 1,
                "source": "pending_0123456789abcdef",
                "destination": "account_9010",
            }
        ),
        encoding="utf-8",
    )

    from services.telegram_service import TelegramService

    monkeypatch.setattr(
        TelegramService,
        "_secure_session_file",
        staticmethod(lambda _path: None),
    )
    result = recover_interrupted_session_moves(session_dir)

    assert result["recovered"] == 1
    assert destination_file.read_bytes() == b"source-main"
    assert Path(f"{destination_file}-wal").read_bytes() == b"source-wal"
    assert not source_sidecar.exists()
    assert not journal.exists()


def test_session_move_recovery_fails_closed_on_two_main_files(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "pending_fedcba9876543210.session").write_bytes(b"source")
    (session_dir / "account_9011.session").write_bytes(b"destination")
    journal = session_dir / ".session_move_fedcba9876543210fedcba98.json"
    journal.write_text(
        json.dumps(
            {
                "version": 1,
                "source": "pending_fedcba9876543210",
                "destination": "account_9011",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Conflicting Telegram session files"):
        recover_interrupted_session_moves(session_dir)
    assert journal.exists()

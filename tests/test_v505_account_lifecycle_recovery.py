from __future__ import annotations

from pathlib import Path

from core.secret_store import SecretStore
from services.account_context import account_secret_key
from services.account_sessions import (
    recover_account_lifecycle,
    recover_session_residues,
    write_account_lifecycle_journal,
)
from storage.database import Database


def _session(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return (root / name).with_suffix(".session")


def test_interrupted_delete_restores_live_account(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile.db")
    secrets = SecretStore(tmp_path / ".secrets.json")
    sessions = tmp_path / "sessions"
    owner = 9501
    db.register_telegram_account(
        telegram_account_id=owner,
        session_name=f"account_{owner}",
        display_name="Old",
    )
    db.set_account_settings(owner, {"telegram.proxy_host": "old-proxy"})
    secrets.set(account_secret_key(owner, "telegram.api_hash"), "old-hash")
    tombstone = f".delete_account_{owner}_" + "a" * 24
    _session(sessions, tombstone).write_bytes(b"old-session")
    write_account_lifecycle_journal(
        secrets,
        account_id=owner,
        operation="delete",
        phase="session_staged",
        final_session_name=f"account_{owner}",
        tombstone_name=tombstone,
        old_account=db.get_telegram_account(owner),
        old_public=db.get_account_settings(owner),
        old_secrets={"telegram.api_hash": "old-hash"},
        selected_before=owner,
        secret_keys=["telegram.api_hash"],
    )

    result = recover_account_lifecycle(db, secrets, sessions)

    assert result["recovered"] == 1
    assert _session(sessions, f"account_{owner}").read_bytes() == b"old-session"
    assert db.get_telegram_account(owner) is not None
    assert db.get_account_setting(owner, "telegram.proxy_host") == "old-proxy"
    assert secrets.get_strict_optional(
        account_secret_key(owner, "telegram.api_hash")
    ) == "old-hash"


def test_interrupted_reauthorization_rolls_back_all_resources(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile.db")
    secrets = SecretStore(tmp_path / ".secrets.json")
    sessions = tmp_path / "sessions"
    owner = 9502
    db.register_telegram_account(
        telegram_account_id=owner,
        session_name=f"account_{owner}",
        display_name="Old",
    )
    old_account = db.get_telegram_account(owner)
    db.replace_account_settings(owner, {"telegram.proxy_host": "new-proxy"})
    secrets.set(account_secret_key(owner, "telegram.api_hash"), "new-hash")
    swap = f".swap_account_{owner}_" + "b" * 24
    _session(sessions, f"account_{owner}").write_bytes(b"new-session")
    _session(sessions, swap).write_bytes(b"old-session")
    pending = "pending_" + "c" * 16
    write_account_lifecycle_journal(
        secrets,
        account_id=owner,
        operation="reauthorize",
        phase="session_swapped",
        pending_session_name=pending,
        final_session_name=f"account_{owner}",
        swap_name=swap,
        old_account=old_account,
        old_public={"telegram.proxy_host": "old-proxy"},
        old_secrets={"telegram.api_hash": "old-hash"},
        selected_before=owner,
        secret_keys=["telegram.api_hash"],
    )

    recover_account_lifecycle(db, secrets, sessions)

    assert _session(sessions, f"account_{owner}").read_bytes() == b"old-session"
    assert not _session(sessions, swap).exists()
    assert db.get_account_setting(owner, "telegram.proxy_host") == "old-proxy"
    assert secrets.get_strict_optional(
        account_secret_key(owner, "telegram.api_hash")
    ) == "old-hash"


def test_residue_scavenger_removes_orphans_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile.db")
    sessions = tmp_path / "sessions"
    owner = 9503
    db.register_telegram_account(
        telegram_account_id=owner,
        session_name=f"account_{owner}",
        display_name="Live",
    )
    _session(sessions, f"account_{owner}").write_bytes(b"live")
    _session(sessions, "account_9999").write_bytes(b"orphan")
    _session(sessions, "pending_" + "d" * 16).write_bytes(b"pending")

    result = recover_session_residues(db, sessions)

    assert result["purged"] == 2
    assert _session(sessions, f"account_{owner}").read_bytes() == b"live"
    assert not _session(sessions, "account_9999").exists()


def test_container_marks_synchronous_secret_migration_verified() -> None:
    source = Path("core/composition.py").read_text(encoding="utf-8")
    assert "secret_migration_verified=True" in source

"""Backup was removed from the product; it must not creep back.

A copy of main.session carries the same Telegram authorization key as the
original, and a profile archive is one more place the operator has to guard.
Both were removed on request, so what these tests pin is an absence: no code
path writes a session copy, no API offers an archive, and any backups left by
an older version are cleared out rather than inherited.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.telegram_service import TelegramService

ROOT = Path(__file__).resolve().parents[1]


def _healthy_session(path: Path) -> Path:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE sessions(value INTEGER)")
        connection.execute("INSERT INTO sessions(value) VALUES(1)")
        connection.commit()
    return path


def test_the_session_mixin_can_no_longer_copy_a_session() -> None:
    assert not hasattr(TelegramService, "backup_session")
    assert not hasattr(TelegramService, "SESSION_BACKUP_LIMIT")


def test_no_module_imports_the_deleted_backup_code() -> None:
    assert not (ROOT / "core" / "profile_backup.py").exists()
    assert not (ROOT / "core" / "profile_restore_runtime.py").exists()
    offenders = []
    for path in ROOT.rglob("*.py"):
        if ".venv" in path.parts or "scratchpad" in path.parts:
            continue
        if path.name == Path(__file__).name:
            continue  # this file names the modules in order to forbid them
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "core.profile_backup" in text or "core.profile_restore_runtime" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


@pytest.mark.parametrize(
    "symbol",
    [
        "create_profile_backup",
        "inspect_profile_backup",
        "restore_profile_backup",
        "session_backup_enabled",
    ],
)
def test_the_public_surface_no_longer_offers_backup(symbol: str) -> None:
    for relative in (
        "services/api_parts/settings.py",
        "gui/gui_service_adapter.py",
        "gui/views/account_view.py",
        "core/config.py",
        "core/composition.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert symbol not in text, f"{relative} still exposes {symbol}"


def test_starting_the_service_clears_backups_left_by_an_older_version(
    tmp_path: Path,
) -> None:
    """An upgrade must not inherit copies of the authorization key."""

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session = _healthy_session(sessions / "main.session")
    legacy = sessions / "backups"
    legacy.mkdir()
    (legacy / "main.session.20260101T000000Z.bak").write_bytes(b"old-authorization-key")

    TelegramService.purge_session_backups(session)

    assert not legacy.exists(), "legacy session backups survived startup"
    assert session.exists(), "the live session must never be removed"
    assert not list(sessions.glob("backups.revoked.*"))


def test_a_corrupt_session_is_quarantined_rather_than_restored(tmp_path: Path) -> None:
    """With no backups there is nothing to restore from - and the broken file
    must never be left where Telethon will open it."""

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session = sessions / "main.session"
    session.write_bytes(b"not a sqlite database")

    TelegramService._prepare_session_file(session)

    assert TelegramService._session_is_healthy(session) is False
    quarantined = list(sessions.glob("main.session.corrupt.*"))
    assert quarantined, "the corrupt session was not quarantined"
    assert not session.exists() or session.stat().st_size == 0


def test_connecting_does_not_write_a_session_copy(tmp_path: Path) -> None:
    """transport.connect() used to call backup_session() on every connect."""

    transport = (ROOT / "services" / "telegram" / "transport.py").read_text(
        encoding="utf-8"
    )
    assert "backup_session" not in transport


def test_the_shutdown_path_no_longer_carries_a_restore_handoff() -> None:
    app = (ROOT / "gui" / "app.py").read_text(encoding="utf-8")
    for symbol in (
        "profile_restore_executor",
        "_schedule_profile_restore",
        "request_profile_restore",
    ):
        assert symbol not in app, f"gui/app.py still references {symbol}"
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "PROFILE_RESTORE_HELPER_FLAG" not in main


def test_settings_no_longer_accept_a_session_backup_policy(tmp_path: Path) -> None:
    settings = (ROOT / "services" / "api_parts" / "settings.py").read_text(
        encoding="utf-8"
    )
    assert "telegram.session_backup_enabled" not in settings


def test_the_config_carries_no_backup_flag() -> None:
    from core.config import TelegramSettings

    settings = TelegramSettings(
        api_id=1, api_hash="hash", session_dir=Path("sessions")
    )
    assert not hasattr(settings, "session_backup_enabled")


def test_service_startup_reports_no_backup_setting(tmp_path: Path) -> None:
    """The service used to read settings.session_backup_enabled to decide."""

    source = (ROOT / "services" / "telegram_service.py").read_text(encoding="utf-8")
    assert "session_backup_enabled" not in source
    assert "purge_session_backups" in source, (
        "startup must still clear copies an older version may have left"
    )


def test_settings_object_without_the_flag_is_accepted() -> None:
    """Nothing may read the removed attribute off a settings object."""

    service = object.__new__(TelegramService)
    service.settings = SimpleNamespace()
    assert not hasattr(service.settings, "session_backup_enabled")

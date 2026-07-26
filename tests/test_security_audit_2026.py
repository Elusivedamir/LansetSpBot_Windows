from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from zipfile import ZipFile

import pytest

from core.local_security import LocalFileSecurityError
from core.private_trace import open_helper_trace
from core.profile_backup import ProfileBackupError, create_profile_backup
from core.redaction import sanitize_data, sanitize_log_text, sanitize_text
from core.secret_store import SecretStore
from core.paths import AppPaths
from storage.database import Database
from tests.conftest import export_plaintext_copy


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        database=root / "marlen.db",
        logs=root / "logs",
        sessions=root / "sessions",
        backups=root / "backups",
    )


def test_backup_v3_encrypts_database_and_excludes_credentials_and_sessions(tmp_path: Path) -> None:
    paths = _paths(tmp_path / "profile")
    paths.ensure()
    database = Database(paths.database)
    database.close_thread_connection()
    session = paths.sessions / "main.session"
    session.write_bytes(b"synthetic-not-a-real-session")

    result = create_profile_backup(
        database_path=paths.database,
        session_dir=paths.sessions,
        secret_snapshot={
            "openai.api_key": "sk-test-not-a-real-key",
            "telegram.api_hash": "TEST_TELEGRAM_API_HASH",
            "proxy.password": "synthetic-password",
        },
        destination=tmp_path / "profile.zip",
        include_sessions=True,
    )
    with ZipFile(result.path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert names == {"profile/marlen.db", "manifest.json"}
    assert manifest["format_version"] == 3
    assert manifest["database_encrypted"] is True
    assert manifest["key_binding"] == "current_os_profile"
    assert manifest["contains_secrets"] is False
    assert manifest["contains_sessions"] is False


def test_backup_destination_symlink_is_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path / "profile")
    paths.ensure()
    database = Database(paths.database)
    database.close_thread_connection()
    target = tmp_path / "target.marlen-backup.zip"
    target.write_bytes(b"ORIGINAL")
    link = tmp_path / "link.marlen-backup.zip"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ProfileBackupError, match="symlink"):
        create_profile_backup(
            database_path=paths.database,
            session_dir=paths.sessions,
            secret_snapshot={},
            destination=link,
            include_sessions=False,
        )
    assert target.read_bytes() == b"ORIGINAL"


def test_helper_trace_rejects_symlink_and_is_owner_only(tmp_path: Path) -> None:
    trace_dir = tmp_path / "private"
    trace_dir.mkdir(mode=0o700)
    victim = tmp_path / "victim.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    trace = trace_dir / "helper.log"
    try:
        trace.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    with pytest.raises((LocalFileSecurityError, OSError)):
        open_helper_trace(trace)
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"

    trace.unlink()
    with open_helper_trace(trace) as stream:
        stream.write(b"safe")
    assert trace.read_bytes() == b"safe"
    if os.name != "nt":
        assert stat.S_IMODE(trace.stat().st_mode) == 0o600


def test_redaction_covers_provider_keys_headers_urls_and_nested_data() -> None:
    assert "sk-test" not in sanitize_text("api_key=sk-test-not-a-real-key")
    assert "test-secret-token" not in sanitize_text(
        "Authorization: Bearer test-secret-token"
    )
    uri = sanitize_text("socks5://alice:secret@127.0.0.1:1080")
    assert "alice" not in uri and "secret" not in uri
    sanitized = sanitize_data(
        {
            "api_key": "sk-proj-1234567890",
            "nested": {"authorization": "Bearer abcdefghijk"},
        }
    )
    assert sanitized["api_key"] == "<redacted>"
    assert sanitized["nested"]["authorization"] == "<redacted>"


def test_log_text_neutralizes_forged_lines_and_controls() -> None:
    rendered = sanitize_log_text("channel title\nERROR forged\r\x1b[31m")
    assert "\n" not in rendered and "\r" not in rendered and "\x1b" not in rendered
    assert r"\nERROR forged\r\x1b" in rendered


def test_legacy_v1_restore_discards_archived_credentials_and_sessions(tmp_path: Path) -> None:
    import hashlib
    from zipfile import ZIP_DEFLATED

    from core.profile_backup import restore_profile_backup

    source_paths = _paths(tmp_path / "source")
    source_paths.ensure()
    source_db = Database(source_paths.database)
    source_db.set_setting("audit.marker", "legacy-database")
    schema_version = source_db.get_version()
    source_db.close_thread_connection()

    secrets = json.dumps(
        {"openai.api_key": "sk-test-not-a-real-key"}, sort_keys=True
    ).encode("utf-8")
    session_path = tmp_path / "legacy.session"
    session_db = Database(session_path)
    session_db.close_thread_connection()
    # A real format_version 1 archive predates SQLCipher and therefore holds an
    # ordinary plaintext SQLite database.
    legacy_database = export_plaintext_copy(
        source_paths.database, tmp_path / "legacy-plaintext.db"
    )
    legacy_session = export_plaintext_copy(
        session_path, tmp_path / "legacy-plaintext.session"
    )
    members = {
        "profile/marlen.db": legacy_database.read_bytes(),
        "profile/secrets.json": secrets,
        "profile/sessions/main.session": legacy_session.read_bytes(),
    }
    kinds = {
        "profile/marlen.db": "database",
        "profile/secrets.json": "secrets",
        "profile/sessions/main.session": "telegram_session",
    }
    manifest = {
        "format": "marlen-profile-backup",
        "format_version": 1,
        "app_version": "legacy-test",
        "schema_version": schema_version,
        "created_at": "2026-01-01T00:00:00+00:00",
        "contains_secrets": True,
        "contains_sessions": True,
        "files": [
            {
                "path": name,
                "kind": kinds[name],
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in members.items()
        ],
    }
    archive_path = tmp_path / "legacy.marlen-backup.zip"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
        archive.writestr("manifest.json", json.dumps(manifest))

    active_paths = _paths(tmp_path / "active")
    active_paths.ensure()
    active_db = Database(active_paths.database)
    active_db.close_thread_connection()
    (active_paths.root / ".secrets.json").write_text(
        json.dumps({"telegram.api_hash": "current"}), encoding="utf-8"
    )
    os.chmod(active_paths.root / ".secrets.json", 0o600)

    result = restore_profile_backup(
        archive_path=archive_path,
        paths=active_paths,
        secret_path=active_paths.root / ".secrets.json",
    )
    assert result.contained_sessions is True
    restored = Database(active_paths.database)
    assert restored.get_setting("audit.marker", "") == "legacy-database"
    restored.close_thread_connection()
    secret_path = active_paths.root / ".secrets.json"
    assert SecretStore(secret_path).export_snapshot() == {}
    assert secret_path.read_bytes().startswith(b"LSPBV1\x00")
    assert not list(active_paths.sessions.glob("*.session"))

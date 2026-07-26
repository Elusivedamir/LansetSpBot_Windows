from __future__ import annotations

import json
import math
import random
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from core.campaign_schedule import redistribute_slots
from core.paths import AppPaths
from core.profile_backup import (
    ProfileBackupError,
    create_profile_backup,
    inspect_profile_backup,
    restore_profile_backup,
)
from core.secret_store import SecretStore
from services.api_parts.task_queue import TaskQueueAPIMixin
from services.telegram_service import TelegramService
from storage.database import Database


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        database=root / "marlen.db",
        logs=root / "logs",
        sessions=root / "sessions",
        backups=root / "backups",
    )


def _sqlite_session(path: Path, value: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE sample(value INTEGER NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES(?)", (value,))
        connection.commit()


class _TaskAPI(TaskQueueAPIMixin):
    ALLOWED_TASK_TYPES = frozenset({"auto_comment"})
    ACCOUNT_BOUND_TASK_TYPES = frozenset({"auto_comment"})
    NON_IDEMPOTENT_TASK_TYPES = frozenset({"auto_comment"})

    def __init__(self, database: Database) -> None:
        self.database = database
        self._auth_in_progress = False


def _task_api(database: Database) -> _TaskAPI:
    return _TaskAPI(database)


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf, "NaN", "Infinity"])
def test_auto_comment_rejects_non_finite_delays_without_partial_task(
    tmp_path: Path, invalid: object
) -> None:
    database = Database(tmp_path / "marlen.db")
    database.set_setting("telegram.account_id", 42)
    api = _task_api(database)

    with pytest.raises(ValueError, match="finite"):
        api.create_task(
            "auto_comment",
            {"comments": ["test"], "delay_min": invalid, "delay_max": 10},
        )

    assert database.get_tasks(limit=10) == []


def test_redistribute_slots_honors_minimum_lead_after_wake() -> None:
    now = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    lead = 180
    slots = redistribute_slots(
        now,
        now + timedelta(hours=2),
        20,
        minimum_lead_seconds=lead,
        minimum_gap_seconds=15,
        rng=random.Random(7331),
    )

    assert len(slots) == 20
    assert slots == sorted(slots)
    assert slots[0] > now + timedelta(seconds=lead)
    assert all(b - a >= timedelta(seconds=15) for a, b in zip(slots, slots[1:]))


def test_session_backup_is_opt_in_and_disabling_revokes_old_copies(
    tmp_path: Path,
) -> None:
    source = tmp_path / "account.session"
    _sqlite_session(source, 7)
    service = object.__new__(TelegramService)
    service.client = SimpleNamespace(session=SimpleNamespace(filename=str(source)))
    service.settings = SimpleNamespace(session_backup_enabled=False)

    assert service.backup_session() is None
    assert not (tmp_path / "backups").exists()

    service.settings.session_backup_enabled = True
    backup = service.backup_session()
    assert backup is not None and backup.is_file()

    TelegramService.purge_session_backups(source)
    assert not (tmp_path / "backups").exists()
    assert source.is_file()


def test_profile_backup_excludes_sessions_by_default_and_restores_atomically(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path / "profile")
    paths.ensure()
    database = Database(paths.database)
    database.set_setting("audit.marker", "from-backup")
    restored_task_id = database.insert_task("noop", {"source": "backup"})
    database.close_thread_connection()
    secret_path = paths.root / ".secrets.json"
    secrets = SecretStore(secret_path)
    secrets.replace_snapshot({"telegram.api_hash": "secret-value"})
    _sqlite_session(paths.sessions / "42.session", 42)

    archive = tmp_path / "profile-copy.zip"
    created = create_profile_backup(
        database_path=paths.database,
        session_dir=paths.sessions,
        secret_snapshot=secrets.export_snapshot(),
        destination=archive,
        include_sessions=False,
    )
    assert created.path.name.endswith(".marlen-backup.zip")
    info = inspect_profile_backup(created.path)
    assert info.contains_sessions is False
    with ZipFile(created.path) as backup_zip:
        assert not any(name.endswith(".session") for name in backup_zip.namelist())

    current = Database(paths.database)
    current.set_setting("audit.marker", "current-profile")
    current.close_thread_connection()
    secrets.replace_snapshot({"telegram.api_hash": "current-secret"})

    restored = restore_profile_backup(
        archive_path=created.path,
        paths=paths,
        secret_path=secret_path,
    )
    assert restored.contained_sessions is False
    assert restored.previous_database_backup is not None
    assert restored.previous_database_backup.is_file()

    active = Database(paths.database)
    assert active.get_setting("audit.marker", "") == "from-backup"
    restored_task = active.get_task(restored_task_id)
    assert restored_task is not None
    assert restored_task["status"] == "cancelled"
    assert "ручное продолжение" in str(restored_task["error"])
    active.close_thread_connection()
    # Unencrypted backup archives never restore credentials.
    assert SecretStore(secret_path).export_snapshot() == {}
    # A backup without sessions must not silently retain authorization files
    # from the profile that it replaced.
    assert not list(paths.sessions.glob("*.session"))

    previous = Database(restored.previous_database_backup)
    assert previous.get_setting("audit.marker", "") == "current-profile"
    previous.close_thread_connection()


def test_profile_backup_never_exports_sessions_even_after_legacy_consent(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path / "profile")
    paths.ensure()
    database = Database(paths.database)
    database.close_thread_connection()
    session = paths.sessions / "777.session"
    _sqlite_session(session, 777)

    result = create_profile_backup(
        database_path=paths.database,
        session_dir=paths.sessions,
        secret_snapshot={},
        destination=tmp_path / "with-session.zip",
        include_sessions=True,
    )
    info = inspect_profile_backup(result.path)
    assert info.contains_sessions is False
    with ZipFile(result.path) as backup_zip:
        assert "profile/sessions/777.session" not in backup_zip.namelist()
        assert "profile/secrets.json" not in backup_zip.namelist()


def test_tampered_profile_backup_is_rejected_before_activation(tmp_path: Path) -> None:
    paths = _paths(tmp_path / "profile")
    paths.ensure()
    database = Database(paths.database)
    database.set_setting("audit.marker", "safe-current")
    database.close_thread_connection()
    archive = create_profile_backup(
        database_path=paths.database,
        session_dir=paths.sessions,
        secret_snapshot={},
        destination=tmp_path / "clean.zip",
        include_sessions=False,
    ).path

    tampered = tmp_path / "tampered.marlen-backup.zip"
    with (
        ZipFile(archive, "r") as source,
        ZipFile(tampered, "w", compression=ZIP_DEFLATED) as destination,
    ):
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "profile/marlen.db":
                payload = bytes([payload[0] ^ 1]) + payload[1:]
            destination.writestr(info, payload)

    with pytest.raises(ProfileBackupError, match="SHA-256"):
        inspect_profile_backup(tampered)

    active = Database(paths.database)
    assert active.get_setting("audit.marker", "") == "safe-current"
    active.close_thread_connection()


def test_profile_backup_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "traversal.marlen-backup.zip"
    manifest = {
        "format": "marlen-profile-backup",
        "format_version": 1,
        "schema_version": 0,
        "files": [],
    }
    with ZipFile(archive, "w") as backup_zip:
        backup_zip.writestr("../escape", b"bad")
        backup_zip.writestr("manifest.json", json.dumps(manifest))

    with pytest.raises(ProfileBackupError, match="Небезопасное имя"):
        inspect_profile_backup(archive)

from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

from core import factory_reset
from storage import database as database_module
from storage.database import Database


def test_rollback_snapshot_fsync_uses_writable_descriptor(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    root.mkdir()
    target = root / "marlen.db"
    target.write_bytes(b"sqlite-profile")
    real_fsync = os.fsync
    checked = []

    def require_writable_descriptor(descriptor: int) -> None:
        # Windows' _commit (used by os.fsync) rejects read-only descriptors with
        # Errno 9. A zero-byte write is a side-effect-free capability check.
        os.write(descriptor, b"")
        checked.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(factory_reset.os, "fsync", require_writable_descriptor)

    snapshot, entries = factory_reset._create_rollback_snapshot(root, (target,))

    assert snapshot is not None and snapshot.is_file()
    assert entries and entries[0].original == target
    assert checked
    snapshot.unlink()


def test_windows_acl_hardening_is_cached_until_file_identity_changes(
    tmp_path, monkeypatch
):
    path = tmp_path / "marlen.db"
    wal = Path(f"{path}-wal")
    path.write_bytes(b"db")
    wal.write_bytes(b"wal-1")

    database = Database.__new__(Database)
    database.path = path
    database._artifact_security_lock = threading.RLock()
    database._artifact_security_identities = {}

    harden_calls: list[Path] = []
    monkeypatch.setattr(database_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        database_module,
        "validate_private_regular_file",
        lambda candidate, harden=False: Path(candidate).stat(),
    )
    monkeypatch.setattr(
        database_module,
        "harden_private_file",
        lambda candidate: harden_calls.append(Path(candidate)) or True,
    )

    database._harden_database_artifacts()
    database._harden_database_artifacts()

    assert harden_calls.count(path) == 1
    assert harden_calls.count(wal) == 1

    wal.unlink()
    wal.write_bytes(b"wal-2")
    database._harden_database_artifacts()

    assert harden_calls.count(path) == 1
    assert harden_calls.count(wal) == 2


def test_periodic_sqlite_refreshes_are_wired_to_background_entrypoints():
    project_root = Path(__file__).resolve().parents[1]
    activity = (project_root / "gui" / "activity_panel.py").read_text(encoding="utf-8")
    channels = (project_root / "gui" / "views" / "channels_view.py").read_text(
        encoding="utf-8"
    )
    commenting = (project_root / "gui" / "views" / "commenting_view.py").read_text(
        encoding="utf-8"
    )

    assert "self.timer.timeout.connect(self.request_refresh)" in activity
    assert "self.timer.timeout.connect(self.request_join_state_refresh)" in channels
    assert (
        "self.refresh_timer.timeout.connect(self.request_campaign_refresh)"
        in commenting
    )

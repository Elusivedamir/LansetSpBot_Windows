from __future__ import annotations

from pathlib import Path

import storage.database as database_module
from core.local_security import LocalFileSecurityError
from storage.database import Database


def test_transient_sqlite_sidecar_disappearance_does_not_fail_database_hardening(
    monkeypatch, tmp_path
):
    """A WAL/SHM sidecar may disappear between existence check and lstat."""

    path = tmp_path / "sidecar-race.db"
    db = Database(path)
    db.close_thread_connection()
    sidecar = Path(f"{path}-shm")
    sidecar.write_bytes(b"transient")
    real_validate = database_module.validate_private_regular_file
    injected = False

    def disappearing_once(candidate, *, max_bytes=None, harden=True):
        nonlocal injected
        candidate = Path(candidate)
        if candidate == sidecar and not injected:
            injected = True
            cause = FileNotFoundError(2, "No such file or directory", str(candidate))
            error = LocalFileSecurityError(
                f"Could not inspect local path {candidate}: {cause}"
            )
            error.__cause__ = cause
            raise error
        return real_validate(candidate, max_bytes=max_bytes, harden=harden)

    monkeypatch.setattr(
        database_module, "validate_private_regular_file", disappearing_once
    )

    db._harden_database_artifacts(force=True)

    assert injected is True
    assert db.get_setting("audit.sidecar.race", "ok") == "ok"

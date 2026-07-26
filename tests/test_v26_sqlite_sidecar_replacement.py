from __future__ import annotations
import os
from pathlib import Path
import pytest
import storage.database as database_module
from storage.database import Database


def test_replaced_private_sidecar_is_revalidated_not_reported_as_corruption(
    tmp_path, monkeypatch
):
    path = tmp_path / "marlen.db"
    db = Database(path)
    db.close_thread_connection()
    sidecar = Path(f"{path}-wal")
    sidecar.write_bytes(b"old")
    os.chmod(sidecar, 0o600)
    real_harden = database_module.harden_private_file
    replaced = False

    def replace_during_harden(candidate: Path) -> bool:
        nonlocal replaced
        candidate = Path(candidate)
        if candidate == sidecar and not replaced:
            replaced = True
            candidate.unlink()
            candidate.write_bytes(b"new")
            os.chmod(candidate, 0o600)
            return False
        return real_harden(candidate)

    monkeypatch.setattr(database_module, "harden_private_file", replace_during_harden)
    db._artifact_security_identities.clear()
    db._harden_database_artifacts(force=True)
    assert replaced
    assert sidecar in db._artifact_security_identities


def test_same_unhardenable_sidecar_remains_fail_closed(tmp_path, monkeypatch):
    from storage.db_common import DatabaseError

    path = tmp_path / "marlen.db"
    db = Database(path)
    db.close_thread_connection()
    sidecar = Path(f"{path}-wal")
    sidecar.write_bytes(b"same")
    os.chmod(sidecar, 0o644)
    real_harden = database_module.harden_private_file

    def refuse_same_sidecar(candidate: Path) -> bool:
        return False if Path(candidate) == sidecar else real_harden(candidate)

    monkeypatch.setattr(database_module, "harden_private_file", refuse_same_sidecar)
    db._artifact_security_identities.clear()
    with pytest.raises(DatabaseError, match="could not restrict private file"):
        db._harden_database_artifacts(force=True)

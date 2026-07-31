from __future__ import annotations

from io import StringIO
from pathlib import Path

from core.profile_migration import MIGRATION_TRANSACTION_NAME
from core.profile_migration_cli import run_profile_migration_command


def _profile(root: Path) -> None:
    (root / "sessions").mkdir(parents=True)
    (root / "marlen.db").write_bytes(b"database")
    (root / "marlen.db-wal").write_bytes(b"wal")
    (root / "sessions" / "main.session").write_bytes(b"session")


def _run(tmp_path: Path, **environ: str) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    environment = {"APPDATA": str(tmp_path), **environ}
    code = run_profile_migration_command(
        environ=environment,
        platform_name="nt",
        home=tmp_path,
        stdout=stdout,
        stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_explicit_command_migrates_closed_legacy_profile(tmp_path: Path) -> None:
    legacy = tmp_path / "Marlen"
    _profile(legacy)

    code, stdout, stderr = _run(tmp_path)

    assert code == 0
    assert stderr == ""
    assert "3 files" in stdout
    assert not legacy.exists()
    assert (tmp_path / "LansetSpBot" / "marlen.db").read_bytes() == b"database"
    assert (
        tmp_path / "LansetSpBot" / "sessions" / "main.session"
    ).read_bytes() == b"session"


def test_path_override_refuses_migration(tmp_path: Path) -> None:
    legacy = tmp_path / "Marlen"
    _profile(legacy)

    code, _, stderr = _run(
        tmp_path,
        LANSETSPBOT_DATA_DIR=str(tmp_path / "custom"),
    )

    assert code == 2
    assert "path overrides" in stderr
    assert legacy.exists()
    assert not (tmp_path / "LansetSpBot").exists()


def test_instance_lock_refuses_migration_without_guessing_staleness(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "Marlen"
    _profile(legacy)
    (legacy / ".instance-deadbeef.lock").write_text("lock", encoding="utf-8")

    code, _, stderr = _run(tmp_path)

    assert code == 2
    assert "instance lock files exist" in stderr
    assert legacy.exists()
    assert not (tmp_path / "LansetSpBot").exists()


def test_both_profiles_fail_closed(tmp_path: Path) -> None:
    legacy = tmp_path / "Marlen"
    canonical = tmp_path / "LansetSpBot"
    _profile(legacy)
    canonical.mkdir()
    (canonical / "marker").write_text("canonical", encoding="utf-8")

    code, _, stderr = _run(tmp_path)

    assert code == 2
    assert "Both legacy and canonical" in stderr
    assert legacy.exists()
    assert (canonical / "marker").read_text(encoding="utf-8") == "canonical"


def test_unfinished_transaction_is_recovered_then_migrated(tmp_path: Path) -> None:
    transaction = tmp_path / MIGRATION_TRANSACTION_NAME
    _profile(transaction)

    code, stdout, stderr = _run(tmp_path)

    assert code == 0
    assert stderr == ""
    assert "Recovered unfinished migration" in stdout
    assert not transaction.exists()
    assert not (tmp_path / "Marlen").exists()
    assert (tmp_path / "LansetSpBot" / "marlen.db").read_bytes() == b"database"


def test_existing_canonical_profile_is_successful_noop(tmp_path: Path) -> None:
    canonical = tmp_path / "LansetSpBot"
    _profile(canonical)

    code, stdout, stderr = _run(tmp_path)

    assert code == 0
    assert stderr == ""
    assert "nothing to migrate" in stdout
    assert canonical.exists()


def test_missing_profiles_return_usage_error(tmp_path: Path) -> None:
    code, _, stderr = _run(tmp_path)

    assert code == 2
    assert "Legacy profile does not exist" in stderr


def test_non_windows_is_rejected(tmp_path: Path) -> None:
    stdout = StringIO()
    stderr = StringIO()

    code = run_profile_migration_command(
        environ={},
        platform_name="posix",
        home=tmp_path,
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 2
    assert "only on Windows" in stderr.getvalue()

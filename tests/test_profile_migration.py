from __future__ import annotations

from pathlib import Path

import pytest

from core.profile_migration import (
    MIGRATION_TRANSACTION_NAME,
    ProfileMigrationError,
    build_profile_manifest,
    migrate_legacy_profile,
    recover_incomplete_profile_migration,
)


def _create_profile(root: Path) -> dict[str, bytes]:
    payloads = {
        "marlen.db": b"encrypted-database",
        "marlen.db-wal": b"pending-wal-pages",
        "marlen.db-shm": b"shared-memory-index",
        "sessions/account.session": b"telethon-session",
        ".secrets.json": b"encrypted-secrets",
        "logs/app.log": b"log-data",
        "backups/archive.bin": bytes(range(64)),
    }
    for relative, payload in payloads.items():
        candidate = root / relative
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(payload)
    return payloads


def test_manifest_covers_database_sidecars_sessions_secrets_and_logs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Marlen"
    payloads = _create_profile(source)

    manifest = build_profile_manifest(source)

    assert {entry.relative_path for entry in manifest} == set(payloads)
    assert sum(entry.size for entry in manifest) == sum(map(len, payloads.values()))


def test_migration_preserves_every_profile_file(tmp_path: Path) -> None:
    source = tmp_path / "Marlen"
    destination = tmp_path / "LansetSpBot"
    payloads = _create_profile(source)

    result = migrate_legacy_profile(source, destination)

    assert not source.exists()
    assert destination.is_dir()
    assert result.files_verified == len(payloads)
    assert result.bytes_verified == sum(map(len, payloads.values()))
    for relative, payload in payloads.items():
        assert (destination / relative).read_bytes() == payload
    assert not (tmp_path / MIGRATION_TRANSACTION_NAME).exists()


def test_existing_destination_fails_without_touching_either_profile(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Marlen"
    destination = tmp_path / "LansetSpBot"
    _create_profile(source)
    destination.mkdir()
    (destination / "marker").write_text("canonical", encoding="utf-8")
    source_manifest = build_profile_manifest(source)

    with pytest.raises(ProfileMigrationError, match="already exists"):
        migrate_legacy_profile(source, destination)

    assert build_profile_manifest(source) == source_manifest
    assert (destination / "marker").read_text(encoding="utf-8") == "canonical"


def test_failure_during_second_rename_restores_legacy_profile(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Marlen"
    destination = tmp_path / "LansetSpBot"
    payloads = _create_profile(source)
    calls = 0

    def failing_rename(old: Path, new: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("simulated Windows lock")
        old.rename(new)

    with pytest.raises(ProfileMigrationError, match="original profile restored"):
        migrate_legacy_profile(source, destination, rename=failing_rename)

    assert source.is_dir()
    assert not destination.exists()
    assert not (tmp_path / MIGRATION_TRANSACTION_NAME).exists()
    for relative, payload in payloads.items():
        assert (source / relative).read_bytes() == payload


def test_post_migration_manifest_mismatch_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "Marlen"
    destination = tmp_path / "LansetSpBot"
    payloads = _create_profile(source)
    original_builder = build_profile_manifest
    calls = 0

    def changing_manifest(root: Path, *, chunk_size: int = 1024 * 1024):
        nonlocal calls
        calls += 1
        manifest = original_builder(root, chunk_size=chunk_size)
        if calls == 2:
            changed = list(manifest)
            changed[0] = type(changed[0])(
                changed[0].relative_path,
                changed[0].size,
                "0" * 64,
            )
            return tuple(changed)
        return manifest

    monkeypatch.setattr(
        "core.profile_migration.build_profile_manifest",
        changing_manifest,
    )

    with pytest.raises(ProfileMigrationError, match="verification failed"):
        migrate_legacy_profile(source, destination)

    assert source.is_dir()
    assert not destination.exists()
    for relative, payload in payloads.items():
        assert (source / relative).read_bytes() == payload


def test_crash_transaction_can_be_recovered_to_legacy_path(tmp_path: Path) -> None:
    source = tmp_path / "Marlen"
    destination = tmp_path / "LansetSpBot"
    transaction = tmp_path / MIGRATION_TRANSACTION_NAME
    payloads = _create_profile(transaction)

    assert recover_incomplete_profile_migration(source, destination) is True

    assert source.is_dir()
    assert not transaction.exists()
    for relative, payload in payloads.items():
        assert (source / relative).read_bytes() == payload


def test_ambiguous_recovery_state_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "Marlen"
    destination = tmp_path / "LansetSpBot"
    transaction = tmp_path / MIGRATION_TRANSACTION_NAME
    _create_profile(transaction)
    source.mkdir()

    with pytest.raises(ProfileMigrationError, match="coexist"):
        recover_incomplete_profile_migration(source, destination)

    assert source.is_dir()
    assert transaction.is_dir()
    assert not destination.exists()


def test_symlink_in_profile_is_rejected_without_migration(tmp_path: Path) -> None:
    source = tmp_path / "Marlen"
    destination = tmp_path / "LansetSpBot"
    source.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    link = source / "unsafe-link"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable in this test environment")

    with pytest.raises(ProfileMigrationError, match="symbolic-link"):
        migrate_legacy_profile(source, destination)

    assert source.is_dir()
    assert not destination.exists()
    assert target.read_text(encoding="utf-8") == "outside"

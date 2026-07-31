from __future__ import annotations

from pathlib import Path

import pytest

from core.paths import (
    CANONICAL_DATA_DIR_ENV,
    LEGACY_DATA_DIR_ENV,
    ProfilePathConflictError,
    _resolve_profile_root,
)


def test_new_windows_install_uses_canonical_profile(tmp_path: Path) -> None:
    root = _resolve_profile_root(
        environ={"APPDATA": str(tmp_path)},
        platform_name="nt",
        home=tmp_path,
    )

    assert root == tmp_path / "LansetSpBot"
    assert not root.exists()


def test_existing_legacy_profile_remains_source_of_truth(tmp_path: Path) -> None:
    legacy = tmp_path / "Marlen"
    session = legacy / "sessions" / "main.session"
    database = legacy / "marlen.db"
    session.parent.mkdir(parents=True)
    session.write_bytes(b"session-canary")
    database.write_bytes(b"sqlcipher-canary")

    root = _resolve_profile_root(
        environ={"APPDATA": str(tmp_path)},
        platform_name="nt",
        home=tmp_path,
    )

    assert root == legacy
    assert session.read_bytes() == b"session-canary"
    assert database.read_bytes() == b"sqlcipher-canary"
    assert not (tmp_path / "LansetSpBot").exists()


def test_existing_canonical_profile_is_selected(tmp_path: Path) -> None:
    canonical = tmp_path / "LansetSpBot"
    canonical.mkdir()

    root = _resolve_profile_root(
        environ={"APPDATA": str(tmp_path)},
        platform_name="nt",
        home=tmp_path,
    )

    assert root == canonical


def test_two_existing_profiles_stop_startup_without_merging(tmp_path: Path) -> None:
    legacy = tmp_path / "Marlen"
    canonical = tmp_path / "LansetSpBot"
    legacy.mkdir()
    canonical.mkdir()
    (legacy / "legacy-marker").write_text("legacy", encoding="utf-8")
    (canonical / "canonical-marker").write_text("canonical", encoding="utf-8")

    with pytest.raises(ProfilePathConflictError, match="Both legacy and canonical"):
        _resolve_profile_root(
            environ={"APPDATA": str(tmp_path)},
            platform_name="nt",
            home=tmp_path,
        )

    assert (legacy / "legacy-marker").read_text(encoding="utf-8") == "legacy"
    assert (canonical / "canonical-marker").read_text(encoding="utf-8") == "canonical"


def test_canonical_override_has_priority_when_used_alone(tmp_path: Path) -> None:
    legacy = tmp_path / "Marlen"
    legacy.mkdir()
    override = tmp_path / "custom-profile"

    root = _resolve_profile_root(
        environ={CANONICAL_DATA_DIR_ENV: str(override), "APPDATA": str(tmp_path)},
        platform_name="nt",
        home=tmp_path,
    )

    assert root == override.resolve()
    assert legacy.exists()


def test_legacy_override_remains_supported(tmp_path: Path) -> None:
    override = tmp_path / "legacy-override"

    root = _resolve_profile_root(
        environ={LEGACY_DATA_DIR_ENV: str(override)},
        platform_name="nt",
        home=tmp_path,
    )

    assert root == override.resolve()


def test_matching_overrides_are_accepted(tmp_path: Path) -> None:
    override = tmp_path / "shared-override"

    root = _resolve_profile_root(
        environ={
            CANONICAL_DATA_DIR_ENV: str(override),
            LEGACY_DATA_DIR_ENV: str(override),
        },
        platform_name="nt",
        home=tmp_path,
    )

    assert root == override.resolve()


def test_conflicting_overrides_stop_startup(tmp_path: Path) -> None:
    with pytest.raises(ProfilePathConflictError, match="point to different"):
        _resolve_profile_root(
            environ={
                CANONICAL_DATA_DIR_ENV: str(tmp_path / "new"),
                LEGACY_DATA_DIR_ENV: str(tmp_path / "old"),
            },
            platform_name="nt",
            home=tmp_path,
        )


def test_non_windows_default_is_unchanged(tmp_path: Path) -> None:
    root = _resolve_profile_root(
        environ={},
        platform_name="posix",
        home=tmp_path,
    )

    assert root == tmp_path / ".local" / "share" / "marlen"

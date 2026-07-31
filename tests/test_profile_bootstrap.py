from __future__ import annotations

from pathlib import Path

import pytest

from core.profile_bootstrap import (
    CANONICAL_DATA_DIR_ENV,
    LEGACY_DATA_DIR_ENV,
    ProfileBootstrapError,
    ProfileBootstrapState,
    prepare_profile_environment,
)


def test_new_windows_profile_is_selected_without_creating_it(tmp_path: Path) -> None:
    environ = {"APPDATA": str(tmp_path)}

    result = prepare_profile_environment(
        environ=environ,
        platform_name="nt",
        home=tmp_path,
    )

    expected = (tmp_path / "LansetSpBot").resolve()
    assert result.state is ProfileBootstrapState.NEW
    assert result.selected_root == expected
    assert environ[CANONICAL_DATA_DIR_ENV] == str(expected)
    assert not expected.exists()


def test_existing_legacy_profile_is_pinned_without_moving_data(tmp_path: Path) -> None:
    legacy = tmp_path / "Marlen"
    database = legacy / "marlen.db"
    session = legacy / "sessions" / "account.session"
    session.parent.mkdir(parents=True)
    database.write_bytes(b"database-canary")
    session.write_bytes(b"session-canary")
    environ = {"APPDATA": str(tmp_path)}

    result = prepare_profile_environment(
        environ=environ,
        platform_name="nt",
        home=tmp_path,
    )

    assert result.state is ProfileBootstrapState.LEGACY
    assert result.selected_root == legacy.resolve()
    assert environ[CANONICAL_DATA_DIR_ENV] == str(legacy.resolve())
    assert database.read_bytes() == b"database-canary"
    assert session.read_bytes() == b"session-canary"
    assert not (tmp_path / "LansetSpBot").exists()


def test_existing_canonical_profile_is_pinned(tmp_path: Path) -> None:
    canonical = tmp_path / "LansetSpBot"
    canonical.mkdir()
    environ = {"APPDATA": str(tmp_path)}

    result = prepare_profile_environment(
        environ=environ,
        platform_name="nt",
        home=tmp_path,
    )

    assert result.state is ProfileBootstrapState.CANONICAL
    assert result.selected_root == canonical.resolve()
    assert environ[CANONICAL_DATA_DIR_ENV] == str(canonical.resolve())


def test_two_profiles_fail_before_any_mutation(tmp_path: Path) -> None:
    legacy = tmp_path / "Marlen"
    canonical = tmp_path / "LansetSpBot"
    legacy.mkdir()
    canonical.mkdir()
    legacy_marker = legacy / "legacy-marker"
    canonical_marker = canonical / "canonical-marker"
    legacy_marker.write_text("legacy", encoding="utf-8")
    canonical_marker.write_text("canonical", encoding="utf-8")
    environ = {"APPDATA": str(tmp_path)}

    with pytest.raises(ProfileBootstrapError, match="Both legacy and canonical"):
        prepare_profile_environment(
            environ=environ,
            platform_name="nt",
            home=tmp_path,
        )

    assert CANONICAL_DATA_DIR_ENV not in environ
    assert legacy_marker.read_text(encoding="utf-8") == "legacy"
    assert canonical_marker.read_text(encoding="utf-8") == "canonical"


def test_legacy_override_is_normalized_into_canonical_environment(
    tmp_path: Path,
) -> None:
    override = tmp_path / "legacy-override"
    environ = {LEGACY_DATA_DIR_ENV: str(override)}

    result = prepare_profile_environment(
        environ=environ,
        platform_name="nt",
        home=tmp_path,
    )

    assert result.state is ProfileBootstrapState.OVERRIDE
    assert result.selected_root == override.resolve()
    assert environ[CANONICAL_DATA_DIR_ENV] == str(override.resolve())


def test_matching_overrides_are_accepted(tmp_path: Path) -> None:
    override = tmp_path / "shared"
    environ = {
        CANONICAL_DATA_DIR_ENV: str(override),
        LEGACY_DATA_DIR_ENV: str(override),
    }

    result = prepare_profile_environment(
        environ=environ,
        platform_name="nt",
        home=tmp_path,
    )

    assert result.state is ProfileBootstrapState.OVERRIDE
    assert result.selected_root == override.resolve()


def test_conflicting_overrides_fail_without_rewriting_environment(
    tmp_path: Path,
) -> None:
    environ = {
        CANONICAL_DATA_DIR_ENV: str(tmp_path / "new"),
        LEGACY_DATA_DIR_ENV: str(tmp_path / "old"),
    }
    original = dict(environ)

    with pytest.raises(ProfileBootstrapError, match="point to different"):
        prepare_profile_environment(
            environ=environ,
            platform_name="nt",
            home=tmp_path,
        )

    assert environ == original


def test_non_windows_default_does_not_override_existing_path_rules(
    tmp_path: Path,
) -> None:
    environ: dict[str, str] = {}

    result = prepare_profile_environment(
        environ=environ,
        platform_name="posix",
        home=tmp_path,
    )

    assert result.state is ProfileBootstrapState.NON_WINDOWS
    assert result.selected_root is None
    assert CANONICAL_DATA_DIR_ENV not in environ

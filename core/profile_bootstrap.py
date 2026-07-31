"""Early profile selection before Qt, SQLite, SecretStore, or Telethon imports."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import MutableMapping


CANONICAL_DATA_DIR_ENV = "LANSETSPBOT_DATA_DIR"
LEGACY_DATA_DIR_ENV = "MARLEN_DATA_DIR"
CANONICAL_WINDOWS_PROFILE = "LansetSpBot"
LEGACY_WINDOWS_PROFILE = "Marlen"


class ProfileBootstrapError(RuntimeError):
    """Raised when startup cannot select exactly one profile safely."""


class ProfileBootstrapState(StrEnum):
    OVERRIDE = "override"
    LEGACY = "legacy"
    CANONICAL = "canonical"
    NEW = "new"
    NON_WINDOWS = "non_windows"


@dataclass(frozen=True, slots=True)
class ProfileBootstrapResult:
    state: ProfileBootstrapState
    selected_root: Path | None


def _normalized_override(value: str | None) -> Path | None:
    if value is None or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def prepare_profile_environment(
    *,
    environ: MutableMapping[str, str] | None = None,
    platform_name: str | None = None,
    home: Path | None = None,
) -> ProfileBootstrapResult:
    """Select a profile before importing modules that resolve ``APP_PATHS``.

    This phase intentionally performs no move, copy, merge, deletion, database
    access, secret-store access, or session access. It only makes the selected
    profile explicit through ``LANSETSPBOT_DATA_DIR`` so every later import uses
    the same root.
    """

    env = os.environ if environ is None else environ
    current_platform = os.name if platform_name is None else platform_name
    current_home = Path.home() if home is None else Path(home)

    canonical_override = _normalized_override(env.get(CANONICAL_DATA_DIR_ENV))
    legacy_override = _normalized_override(env.get(LEGACY_DATA_DIR_ENV))
    if (
        canonical_override is not None
        and legacy_override is not None
        and canonical_override != legacy_override
    ):
        raise ProfileBootstrapError(
            f"{CANONICAL_DATA_DIR_ENV} and {LEGACY_DATA_DIR_ENV} point to "
            "different profile directories."
        )

    override = canonical_override or legacy_override
    if override is not None:
        env[CANONICAL_DATA_DIR_ENV] = str(override)
        return ProfileBootstrapResult(ProfileBootstrapState.OVERRIDE, override)

    if current_platform != "nt":
        return ProfileBootstrapResult(ProfileBootstrapState.NON_WINDOWS, None)

    appdata_value = env.get("APPDATA")
    base = Path(appdata_value) if appdata_value else current_home
    canonical_root = base / CANONICAL_WINDOWS_PROFILE
    legacy_root = base / LEGACY_WINDOWS_PROFILE
    canonical_exists = _path_exists(canonical_root)
    legacy_exists = _path_exists(legacy_root)

    if canonical_exists and legacy_exists:
        raise ProfileBootstrapError(
            "Both legacy and canonical LansetSpBot profiles exist: "
            f"{legacy_root} and {canonical_root}. "
            "No data was moved or merged. Keep both directories unchanged "
            "until a controlled migration is performed."
        )

    if legacy_exists:
        selected = legacy_root.resolve()
        env[CANONICAL_DATA_DIR_ENV] = str(selected)
        return ProfileBootstrapResult(ProfileBootstrapState.LEGACY, selected)

    if canonical_exists:
        selected = canonical_root.resolve()
        env[CANONICAL_DATA_DIR_ENV] = str(selected)
        return ProfileBootstrapResult(ProfileBootstrapState.CANONICAL, selected)

    selected = canonical_root.resolve()
    env[CANONICAL_DATA_DIR_ENV] = str(selected)
    return ProfileBootstrapResult(ProfileBootstrapState.NEW, selected)

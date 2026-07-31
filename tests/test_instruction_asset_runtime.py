from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gui.instruction_asset_runtime import prepare_source_instruction_assets
from gui.instruction_assets import (
    SCREENSHOT_NAMES,
    instruction_asset_cache_directory,
    instruction_assets_ready,
    mark_instruction_assets_stale,
    write_instruction_asset_metadata,
)
from gui.resources import (
    INSTRUCTION_ASSET_OVERRIDE_ENV,
    INSTRUCTION_ASSET_STATUS_ENV,
    asset_path,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _source_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(
        p
        for p in PROJECT_ROOT.rglob("*")
        if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
    ):
        digest.update(path.relative_to(PROJECT_ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_verified_fixture(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(SCREENSHOT_NAMES, start=1):
        (destination / name).write_bytes(b"verified-screenshot-" + bytes([index]))
    write_instruction_asset_metadata(destination, PROJECT_ROOT)


def test_source_run_generates_verified_assets_only_in_profile_cache(tmp_path):
    profile = tmp_path / "profile"
    environment: dict[str, str] = {}
    before = _source_digest()

    def runner(command, *, cwd, env, timeout):
        assert cwd == PROJECT_ROOT
        assert command[2] == "--destination"
        destination = Path(command[3])
        assert destination.is_relative_to(profile)
        assert env[INSTRUCTION_ASSET_OVERRIDE_ENV] == str(destination)
        assert timeout == 180
        _write_verified_fixture(destination)
        return 0

    result = prepare_source_instruction_assets(
        profile_root=profile,
        project_root=PROJECT_ROOT,
        environment=environment,
        runner=runner,
    )

    assert result.status == "generated"
    assert result.directory == instruction_asset_cache_directory(profile, PROJECT_ROOT)
    assert instruction_assets_ready(result.directory, project_root=PROJECT_ROOT)
    assert environment[INSTRUCTION_ASSET_OVERRIDE_ENV] == str(result.directory)
    assert INSTRUCTION_ASSET_STATUS_ENV not in environment
    assert _source_digest() == before


def test_source_run_reuses_verified_cache_without_running_generator(tmp_path):
    profile = tmp_path / "profile"
    cache = instruction_asset_cache_directory(profile, PROJECT_ROOT)
    _write_verified_fixture(cache)
    environment: dict[str, str] = {}

    def runner(*args, **kwargs):  # pragma: no cover - failure documents contract
        raise AssertionError("verified cache must avoid regeneration")

    result = prepare_source_instruction_assets(
        profile_root=profile,
        project_root=PROJECT_ROOT,
        environment=environment,
        runner=runner,
    )

    assert result.status == "cache_ready"
    assert result.directory == cache
    assert environment[INSTRUCTION_ASSET_OVERRIDE_ENV] == str(cache)


def test_generation_failure_is_fail_closed_and_keeps_main_app_usable(tmp_path):
    environment: dict[str, str] = {INSTRUCTION_ASSET_OVERRIDE_ENV: "stale-value"}

    result = prepare_source_instruction_assets(
        profile_root=tmp_path / "profile",
        project_root=PROJECT_ROOT,
        environment=environment,
        runner=lambda *args, **kwargs: 7,
    )

    assert result.status == "generation_failed"
    assert result.exit_code == 7
    assert INSTRUCTION_ASSET_OVERRIDE_ENV not in environment
    assert environment[INSTRUCTION_ASSET_STATUS_ENV] == "generation_failed"


def test_corrupt_or_missing_cached_asset_is_never_accepted(tmp_path):
    cache = instruction_asset_cache_directory(tmp_path / "profile", PROJECT_ROOT)
    _write_verified_fixture(cache)
    assert instruction_assets_ready(cache, project_root=PROJECT_ROOT)

    (cache / SCREENSHOT_NAMES[0]).write_bytes(b"tampered")
    assert not instruction_assets_ready(cache, project_root=PROJECT_ROOT)

    mark_instruction_assets_stale(cache)
    assert not instruction_assets_ready(cache, project_root=PROJECT_ROOT)


def test_resource_override_is_limited_to_instruction_assets(tmp_path, monkeypatch):
    monkeypatch.setenv(INSTRUCTION_ASSET_OVERRIDE_ENV, str(tmp_path))
    assert asset_path("instructions", "01_account.png") == tmp_path / "01_account.png"
    assert asset_path("lansetspbot.png") != tmp_path / "lansetspbot.png"
    with pytest.raises(ValueError, match="invalid instruction asset path"):
        asset_path("instructions", "nested/../../secret.txt")
    with pytest.raises(ValueError, match="invalid instruction asset path"):
        asset_path("instructions", "..\\secret.txt")

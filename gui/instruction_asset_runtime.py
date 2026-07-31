"""Source-run preparation of verified instruction screenshots.

This module deliberately has no Qt imports so its checkout-safety and
fail-closed decisions can be verified on every supported Python version.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, MutableMapping, Sequence

from gui.instruction_assets import (
    instruction_asset_cache_directory,
    instruction_assets_ready,
)
from gui.resources import (
    INSTRUCTION_ASSET_OVERRIDE_ENV,
    INSTRUCTION_ASSET_STATUS_ENV,
)


@dataclass(frozen=True)
class InstructionAssetPreparation:
    status: str
    directory: Path | None = None
    exit_code: int | None = None


def _default_runner(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> int:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    return int(completed.returncode)


def prepare_source_instruction_assets(
    *,
    profile_root: str | Path,
    project_root: str | Path,
    frozen: bool = False,
    python_executable: str | Path | None = None,
    environment: MutableMapping[str, str] | None = None,
    runner: Callable[..., int] = _default_runner,
    timeout_seconds: int = 180,
) -> InstructionAssetPreparation:
    """Select or generate verified instruction assets without editing sources."""

    env = os.environ if environment is None else environment
    env.pop(INSTRUCTION_ASSET_OVERRIDE_ENV, None)
    env.pop(INSTRUCTION_ASSET_STATUS_ENV, None)
    if frozen:
        return InstructionAssetPreparation("frozen")

    root = Path(project_root).resolve()
    bundled = root / "gui" / "assets" / "instructions"
    if instruction_assets_ready(bundled, project_root=root):
        return InstructionAssetPreparation("bundled_ready", bundled)

    try:
        cache = instruction_asset_cache_directory(profile_root, root).resolve()
        if instruction_assets_ready(cache, project_root=root):
            env[INSTRUCTION_ASSET_OVERRIDE_ENV] = str(cache)
            return InstructionAssetPreparation("cache_ready", cache)

        capture_script = root / "tools" / "capture_instruction_screenshots.py"
        child_env = dict(env)
        child_env[INSTRUCTION_ASSET_OVERRIDE_ENV] = str(cache)
        exit_code = int(
            runner(
                [
                    str(python_executable or sys.executable),
                    str(capture_script),
                    "--destination",
                    str(cache),
                ],
                cwd=root,
                env=child_env,
                timeout=max(1, int(timeout_seconds)),
            )
        )
        if exit_code == 0 and instruction_assets_ready(cache, project_root=root):
            env[INSTRUCTION_ASSET_OVERRIDE_ENV] = str(cache)
            return InstructionAssetPreparation("generated", cache, exit_code)
        env[INSTRUCTION_ASSET_STATUS_ENV] = "generation_failed"
        return InstructionAssetPreparation("generation_failed", cache, exit_code)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
        env[INSTRUCTION_ASSET_STATUS_ENV] = "generation_failed"
        return InstructionAssetPreparation("generation_failed")


__all__ = ["InstructionAssetPreparation", "prepare_source_instruction_assets"]

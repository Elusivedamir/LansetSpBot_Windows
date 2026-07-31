from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


METADATA_FILENAME = "capture_metadata.json"
METADATA_FORMAT_VERSION = 1
SCREENSHOT_NAMES = (
    "01_account.png",
    "02_channels.png",
    "03_links.png",
    "04_comments.png",
    "05_instructions.png",
)


def _source_paths(project_root: Path) -> list[Path]:
    gui_root = project_root / "gui"
    gui_sources = sorted(gui_root.rglob("*.py")) if gui_root.is_dir() else []
    required = (
        project_root / "core" / "version.py",
        project_root / "tools" / "capture_instruction_screenshots.py",
    )
    if not gui_sources or any(not path.is_file() for path in required):
        return []
    return gui_sources + list(required)


def source_fingerprint(project_root: str | Path) -> str | None:
    """Hash every source file that can alter an instruction screenshot."""

    root = Path(project_root).resolve()
    paths = _source_paths(root)
    if not paths:
        # Frozen builds package bytecode rather than the source tree. Their
        # metadata was generated immediately after the build-time capture and
        # remains verifiable through the PNG hashes below.
        return None
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def instruction_asset_cache_directory(
    profile_root: str | Path,
    project_root: str | Path,
) -> Path:
    """Return the source-run cache directory bound to the current GUI sources."""

    fingerprint = source_fingerprint(project_root)
    if not fingerprint:
        raise RuntimeError("Instruction source files are unavailable")
    return Path(profile_root) / "instruction-assets" / fingerprint


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mark_instruction_assets_stale(destination: str | Path) -> None:
    directory = Path(destination)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": METADATA_FORMAT_VERSION,
        "status": "stale",
        "source_fingerprint": "",
        "files": {},
    }
    (directory / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_instruction_asset_metadata(
    destination: str | Path,
    project_root: str | Path,
) -> dict[str, Any]:
    directory = Path(destination)
    fingerprint = source_fingerprint(project_root)
    if not fingerprint:
        raise RuntimeError("Instruction source files are unavailable during capture")
    files: dict[str, str] = {}
    for name in SCREENSHOT_NAMES:
        path = directory / name
        if not path.is_file():
            raise RuntimeError(f"Instruction screenshot is missing after capture: {name}")
        files[name] = _file_digest(path)
    payload: dict[str, Any] = {
        "format": METADATA_FORMAT_VERSION,
        "status": "ready",
        "source_fingerprint": fingerprint,
        "files": files,
    }
    (directory / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def instruction_assets_ready(
    directory: str | Path,
    *,
    project_root: str | Path | None = None,
) -> bool:
    asset_directory = Path(directory)
    metadata_path = asset_directory / METADATA_FILENAME
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("format") != METADATA_FORMAT_VERSION
            or payload.get("status") != "ready"
        ):
            return False
        files = payload.get("files")
        if not isinstance(files, dict) or set(files) != set(SCREENSHOT_NAMES):
            return False
        for name in SCREENSHOT_NAMES:
            expected = str(files.get(name) or "")
            path = asset_directory / name
            if (
                len(expected) != 64
                or not path.is_file()
                or _file_digest(path) != expected
            ):
                return False
        root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path(__file__).resolve().parents[1]
        )
        current_fingerprint = source_fingerprint(root)
        if current_fingerprint is not None:
            return payload.get("source_fingerprint") == current_fingerprint
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False

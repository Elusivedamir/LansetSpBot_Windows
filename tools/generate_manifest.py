#!/usr/bin/env python3
"""Regenerate SHA256SUMS.txt from the files the project actually ships.

A Git checkout uses ``git ls-files`` as the authoritative set. A source ZIP has
no ``.git`` directory, so it reuses the already committed manifest path set.
This keeps Windows ZIP builds reproducible without silently adding caches or
other machine-local files.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = PROJECT_ROOT / "SHA256SUMS.txt"
MANIFEST_NAME = MANIFEST.name


def _safe_relative_name(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized == MANIFEST_NAME
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
    ):
        raise RuntimeError(f"Manifest contains an unsafe project path: {value!r}")
    return path.as_posix()


def _git_tracked_files() -> list[str]:
    completed = subprocess.run(  # noqa: S603 - fixed, internal command
        ["git", "ls-files", "-z"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        check=True,
    )
    return [
        _safe_relative_name(name)
        for name in completed.stdout.decode("utf-8").split("\0")
        if name and name != MANIFEST_NAME
    ]


def _manifest_path_set() -> list[str]:
    if not MANIFEST.is_file():
        raise RuntimeError(
            "Git metadata is unavailable and SHA256SUMS.txt is missing; "
            "extract the complete source archive"
        )
    names: list[str] = []
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        parts = raw.strip().split(None, 1)
        if len(parts) != 2:
            raise RuntimeError(f"Malformed SHA256SUMS.txt entry: {raw!r}")
        relative = _safe_relative_name(parts[1])
        target = PROJECT_ROOT / relative
        if not target.is_file():
            raise RuntimeError(f"Manifest path is missing from the source archive: {relative}")
        names.append(relative)
    if not names:
        raise RuntimeError("SHA256SUMS.txt contains no project files")
    if len(names) != len(set(names)):
        raise RuntimeError("SHA256SUMS.txt contains duplicate project paths")
    return names


def tracked_files() -> list[str]:
    """Return the authoritative shipped file set with a ZIP-safe fallback."""

    try:
        names = _git_tracked_files()
    except (FileNotFoundError, subprocess.CalledProcessError, UnicodeDecodeError, OSError):
        names = _manifest_path_set()
    return sorted(names)


def render_manifest() -> str:
    lines = []
    for relative in tracked_files():
        digest = hashlib.sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
        lines.append(f"{digest}  {relative}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if SHA256SUMS.txt is out of date",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=MANIFEST,
        help=(
            "write or check the rendered manifest at this path; the shipped file "
            "set is still derived from the project checkout"
        ),
    )
    arguments = parser.parse_args()
    output = arguments.output.resolve()

    try:
        expected = render_manifest()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2
    if arguments.check:
        current = output.read_text(encoding="utf-8") if output.is_file() else ""
        if current != expected:
            print(f"{output} is out of date; run tools/generate_manifest.py")
            return 1
        print(f"{output} is up to date ({len(expected.splitlines())} files)")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(expected)
    print(f"{output}: {len(expected.splitlines())} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

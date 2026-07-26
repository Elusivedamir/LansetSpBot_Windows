#!/usr/bin/env python3
"""Regenerate SHA256SUMS.txt from the files the project actually ships.

The manifest exists so a user can prove their copy is intact. That only works
if it lists exactly the tracked source files - no build-machine caches, which
are absent from a fresh checkout and would report as missing.

Usage:
    python tools/generate_manifest.py           # rewrite SHA256SUMS.txt
    python tools/generate_manifest.py --check   # verify without writing
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = PROJECT_ROOT / "SHA256SUMS.txt"
MANIFEST_NAME = MANIFEST.name


def tracked_files() -> list[str]:
    """The shipped file set, as git sees it."""

    completed = subprocess.run(  # noqa: S603 - fixed, internal command
        ["git", "ls-files", "-z"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        check=True,
    )
    names = [
        name
        for name in completed.stdout.decode("utf-8").split("\0")
        if name and name != MANIFEST_NAME
    ]
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
    arguments = parser.parse_args()

    expected = render_manifest()
    if arguments.check:
        current = MANIFEST.read_text(encoding="utf-8") if MANIFEST.is_file() else ""
        if current != expected:
            print(f"{MANIFEST_NAME} is out of date; run tools/generate_manifest.py")
            return 1
        print(f"{MANIFEST_NAME} is up to date ({len(expected.splitlines())} files)")
        return 0

    MANIFEST.write_text(expected, encoding="utf-8")
    print(f"{MANIFEST_NAME}: {len(expected.splitlines())} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

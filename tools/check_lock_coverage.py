#!/usr/bin/env python3
"""Verify requirements-runtime.lock covers every supported interpreter.

sqlcipher3 and cffi publish one wheel per CPython version. A lock generated on
3.13 alone installs fine there and fails hard on 3.14 with

    ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE

which reads like tampering but only means the lock never listed that wheel.
Both versions are advertised in README.txt and accepted by the launcher, so
both must resolve.

The check asks PyPI which wheels exist and confirms the lock pins at least one
that pip would install for each supported interpreter on win_amd64.

Usage:
    python tools/check_lock_coverage.py
    python tools/check_lock_coverage.py --lock requirements-runtime.lock
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The interpreters RUN_FROM_SOURCE_WINDOWS.ps1 accepts, on the only platform
# this product supports.
SUPPORTED_TAGS = ("cp313", "cp314")
PLATFORM_SUFFIX = "win_amd64.whl"
PURE_SUFFIX = "none-any.whl"

_ENTRY = re.compile(
    r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)((?:\s*\\\s*\n\s*--hash=sha256:[0-9a-f]+)+)",
    re.MULTILINE,
)


def parse_lock(path: Path) -> list[tuple[str, str, set[str]]]:
    text = path.read_text(encoding="utf-8")
    entries = []
    for name, version, block in _ENTRY.findall(text):
        hashes = set(re.findall(r"--hash=sha256:([0-9a-f]+)", block))
        entries.append((name, version, hashes))
    return entries


def released_files(name: str, version: str) -> list[dict[str, Any]]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        payload: dict[str, Any] = json.load(response)
    return list(payload["urls"])


def installable_on(files: list[dict[str, Any]], tag: str) -> list[dict[str, Any]]:
    """Wheels pip would consider for `tag` on win_amd64.

    A free-threaded build (cp314t) is a different interpreter with its own
    wheels; the launcher does not support it, so it is excluded here too.
    """

    candidates: list[dict[str, Any]] = []
    for entry in files:
        filename = entry["filename"]
        if f"{tag}t-" in filename:
            continue
        if filename.endswith(PURE_SUFFIX):
            candidates.append(entry)
        elif filename.endswith(PLATFORM_SUFFIX) and (
            f"-{tag}-{tag}-" in filename or "abi3" in filename
        ):
            candidates.append(entry)
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        default=str(PROJECT_ROOT / "requirements-runtime.lock"),
        help="lock file to verify",
    )
    arguments = parser.parse_args()

    lock = Path(arguments.lock)
    entries = parse_lock(lock)
    if not entries:
        print(f"{lock.name}: no pinned requirements found")
        return 1

    failures: list[str] = []
    for name, version, hashes in entries:
        try:
            files = released_files(name, version)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"{name}=={version}: could not reach PyPI ({exc})")
            return 2
        wheels = [entry for entry in files if entry["filename"].endswith(".whl")]
        if not wheels:
            continue  # source-only distribution: one artifact serves every version
        for tag in SUPPORTED_TAGS:
            candidates = installable_on(files, tag)
            if not candidates:
                failures.append(f"{name}=={version}: no {tag} {PLATFORM_SUFFIX} wheel")
                continue
            if not any(entry["digests"]["sha256"] in hashes for entry in candidates):
                names = ", ".join(entry["filename"] for entry in candidates)
                failures.append(
                    f"{name}=={version}: {tag} not covered; pip would need one of {names}"
                )

    for failure in failures:
        print(f"MISSING  {failure}")
    if failures:
        print(f"\n{lock.name} does not cover {', '.join(SUPPORTED_TAGS)} on win_amd64.")
        return 1

    print(
        f"{lock.name}: {len(entries)} packages cover "
        f"{', '.join(SUPPORTED_TAGS)} on win_amd64"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

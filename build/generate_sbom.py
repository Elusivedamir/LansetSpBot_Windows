#!/usr/bin/env python3
"""Generate a compact CycloneDX 1.5 SBOM from complete pinned locks."""

from __future__ import annotations

import argparse
import json
import re
from importlib import metadata
from pathlib import Path

_REQUIREMENT_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*==\s*([^\s\\]+)")


def _canonicalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _logical_requirement_lines(path: Path) -> list[str]:
    lines: list[str] = []
    current = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.endswith(chr(92)):
            current += line[:-1].strip() + " "
            continue
        current += line
        lines.append(current.strip())
        current = ""
    if current:
        raise RuntimeError(f"Unterminated requirement continuation in {path}")
    return lines


def _read_requirements(paths: list[Path]) -> dict[str, tuple[str, str]]:
    requirements: dict[str, tuple[str, str]] = {}
    for path in paths:
        for line in _logical_requirement_lines(path):
            match = _REQUIREMENT_RE.match(line)
            if match is None:
                continue
            name, version = match.groups()
            normalized = _canonicalize(name)
            existing = requirements.get(normalized)
            if existing is not None and existing[1] != version:
                raise RuntimeError(
                    f"Conflicting pinned versions for {name}: {existing[1]} and {version}"
                )
            requirements[normalized] = (name, version)
    return requirements


def _installed_distributions() -> dict[str, metadata.Distribution]:
    result: dict[str, metadata.Distribution] = {}
    for distribution in metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if raw_name:
            result[_canonicalize(raw_name)] = distribution
    return result


def _component(name: str, version: str) -> dict[str, str]:
    normalized = _canonicalize(name)
    return {
        "type": "library",
        "name": name,
        "version": version,
        "purl": f"pkg:pypi/{normalized}@{version}",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--name", default="LansetSpBot")
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--requirements",
        action="append",
        default=[],
        type=Path,
        help="Include exact package/version pairs from this complete pinned lock.",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Additionally include this package when it is installed.",
    )
    args = parser.parse_args()

    installed = _installed_distributions()
    pinned = _read_requirements(args.requirements)
    if not pinned:
        raise SystemExit("SBOM generation requires at least one exact pinned lock")

    failures: list[str] = []
    components_by_name: dict[str, dict[str, str]] = {}
    for normalized, (name, version) in pinned.items():
        distribution = installed.get(normalized)
        if distribution is None:
            failures.append(f"not installed: {name}=={version}")
            continue
        if distribution.version != version:
            failures.append(
                f"version mismatch: {name} {distribution.version} != {version}"
            )
            continue
        components_by_name[normalized] = _component(name, version)

    for requested_name in args.include:
        normalized = _canonicalize(requested_name)
        distribution = installed.get(normalized)
        if distribution is None:
            failures.append(f"included package is not installed: {requested_name}")
            continue
        name = distribution.metadata.get("Name") or requested_name
        components_by_name[normalized] = _component(name, distribution.version)

    if failures:
        raise SystemExit("SBOM environment does not match the locks:\n - " + "\n - ".join(failures))

    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": args.name,
                "version": args.version,
            }
        },
        "components": [components_by_name[key] for key in sorted(components_by_name)],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

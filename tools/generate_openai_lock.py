#!/usr/bin/env python3
"""Generate and verify the hash-locked OpenAI Windows dependency graph.

The source pin lives in requirements-openai.txt. Generation resolves the complete
binary-wheel graph independently for CPython 3.14 and 3.13 on win_amd64, then
requires the same package versions on both interpreters and records every wheel
SHA-256 accepted by either interpreter.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
import zipfile
from email.parser import Parser
from importlib import metadata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "requirements-openai.txt"
LOCK = ROOT / "requirements-openai.lock"
SUPPORTED_TARGETS = (("3.14", "cp314"), ("3.13", "cp313"))
# pip download --platform selects target wheels but environment markers can still
# be evaluated against the host interpreter. Keep Windows-only transitive
# dependencies explicit and fail closed if a required pin disappears.
REQUIRED_WINDOWS_SOURCE_PINS = {"colorama": "0.4.6"}
_PIN_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*==\s*([^\s\\]+)\s*$")
_LOCK_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)(.*)$")
_HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def _canonicalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def _read_source_pins(path: Path = SOURCE) -> list[str]:
    pins: list[str] = []
    seen: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = _PIN_RE.fullmatch(line)
        if match is None:
            raise RuntimeError(f"Unsupported OpenAI source requirement: {raw!r}")
        name, version = match.groups()
        normalized = _canonicalize(name)
        previous = seen.get(normalized)
        if previous is not None:
            raise RuntimeError(
                f"Duplicate OpenAI source pin: {name}=={version} "
                f"(already pinned to {previous})"
            )
        seen[normalized] = version
        pins.append(f"{name}=={version}")
    if not pins:
        raise RuntimeError("requirements-openai.txt contains no exact source pin")
    for name, version in REQUIRED_WINDOWS_SOURCE_PINS.items():
        actual = seen.get(_canonicalize(name))
        if actual != version:
            raise RuntimeError(
                f"Missing required Windows OpenAI source pin: {name}=={version}"
            )
    return pins


def _pip_download(
    destination: Path,
    pins: list[str],
    *,
    python_version: str,
    abi: str,
    constraints: Path | None = None,
) -> None:
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--platform",
        "win_amd64",
        "--implementation",
        "cp",
        "--python-version",
        python_version,
        "--abi",
        abi,
        "--dest",
        str(destination),
    ]
    if constraints is not None:
        command.extend(["--constraint", str(constraints)])
    command.extend(pins)
    completed = subprocess.run(command, cwd=str(ROOT), text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"pip could not resolve the OpenAI Windows wheel graph for CPython {python_version}"
        )


def _wheel_metadata(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        candidates = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(candidates) != 1:
            raise RuntimeError(f"Wheel has no unique METADATA file: {path.name}")
        message = Parser().parsestr(archive.read(candidates[0]).decode("utf-8"))
    name = str(message.get("Name") or "").strip()
    version = str(message.get("Version") or "").strip()
    if not name or not version:
        raise RuntimeError(f"Wheel metadata is incomplete: {path.name}")
    return name, version


def _read_wheel_graph(directory: Path) -> dict[str, dict[str, Any]]:
    graph: dict[str, dict[str, Any]] = {}
    wheels = sorted(directory.glob("*.whl"))
    if not wheels:
        raise RuntimeError(f"No wheels were downloaded into {directory}")
    for wheel in wheels:
        name, version = _wheel_metadata(wheel)
        normalized = _canonicalize(name)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        current = graph.get(normalized)
        if current is None:
            graph[normalized] = {
                "name": name,
                "version": version,
                "hashes": {digest},
            }
            continue
        if str(current["version"]) != version:
            raise RuntimeError(
                f"Resolver downloaded conflicting versions for {name}: "
                f"{current['version']} and {version}"
            )
        current["hashes"].add(digest)
    return graph


def _write_constraints(graph: dict[str, dict[str, Any]], path: Path) -> None:
    text = "".join(
        f"{graph[key]['name']}=={graph[key]['version']}\n" for key in sorted(graph)
    )
    path.write_text(text, encoding="utf-8")


def _merge_graphs(
    graphs: list[dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    if not graphs:
        raise RuntimeError("No OpenAI dependency graph was resolved")
    expected_names = set(graphs[0])
    for index, graph in enumerate(graphs[1:], start=2):
        if set(graph) != expected_names:
            missing = sorted(expected_names - set(graph))
            extra = sorted(set(graph) - expected_names)
            raise RuntimeError(
                "OpenAI dependency graphs differ across supported Pythons "
                f"(graph {index}; missing={missing}, extra={extra})"
            )
    merged: dict[str, dict[str, Any]] = {}
    for normalized in sorted(expected_names):
        first = graphs[0][normalized]
        versions = {str(graph[normalized]["version"]) for graph in graphs}
        if len(versions) != 1:
            raise RuntimeError(
                f"OpenAI dependency {first['name']} resolves to different versions: "
                f"{sorted(versions)}"
            )
        hashes: set[str] = set()
        for graph in graphs:
            hashes.update(str(value) for value in graph[normalized]["hashes"])
        merged[normalized] = {
            "name": str(first["name"]),
            "version": str(first["version"]),
            "hashes": hashes,
        }
    return merged


def _render_lock(graph: dict[str, dict[str, Any]]) -> str:
    lines = [
        "# Complete hash-locked OpenAI runtime graph for Windows x64.",
        "# Generated by tools/generate_openai_lock.py for CPython 3.13 and 3.14.",
        "# Do not edit by hand; regenerate after changing requirements-openai.txt.",
    ]
    slash = chr(92)
    for normalized in sorted(graph):
        item = graph[normalized]
        hashes = sorted(str(value) for value in item["hashes"])
        if not hashes:
            raise RuntimeError(f"No wheel hashes recorded for {item['name']}")
        lines.append(f"{item['name']}=={item['version']} " + slash)
        for index, value in enumerate(hashes):
            continuation = " " + slash if index + 1 < len(hashes) else ""
            lines.append(f"    --hash=sha256:{value}{continuation}")
    return "\n".join(lines) + "\n"


def generate(source: Path = SOURCE, output: Path = LOCK) -> dict[str, dict[str, Any]]:
    pins = _read_source_pins(source)
    with tempfile.TemporaryDirectory(prefix="lanset-openai-lock-") as temporary:
        root = Path(temporary)
        graphs: list[dict[str, dict[str, Any]]] = []
        constraints: Path | None = None
        for index, (python_version, abi) in enumerate(SUPPORTED_TARGETS):
            destination = root / abi
            destination.mkdir()
            _pip_download(
                destination,
                pins,
                python_version=python_version,
                abi=abi,
                constraints=constraints,
            )
            graph = _read_wheel_graph(destination)
            graphs.append(graph)
            if index == 0:
                constraints = root / "constraints.txt"
                _write_constraints(graph, constraints)
        merged = _merge_graphs(graphs)
    output.write_text(_render_lock(merged), encoding="utf-8")
    print(f"{output.name}: {len(merged)} hash-locked packages")
    return merged


def _logical_lock_lines(path: Path) -> list[str]:
    logical: list[str] = []
    current = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.endswith(chr(92)):
            current += line[:-1].strip() + " "
            continue
        current += line
        logical.append(current.strip())
        current = ""
    if current:
        raise RuntimeError(f"Unterminated requirement continuation in {path.name}")
    return logical


def parse_lock(path: Path = LOCK) -> dict[str, dict[str, Any]]:
    parsed: dict[str, dict[str, Any]] = {}
    for line in _logical_lock_lines(path):
        match = _LOCK_RE.fullmatch(line)
        if match is None:
            raise RuntimeError(f"Unsupported lock entry: {line!r}")
        name, version, options = match.groups()
        hashes = set(_HASH_RE.findall(options))
        if not hashes:
            raise RuntimeError(f"Lock entry has no SHA-256 hash: {name}=={version}")
        normalized = _canonicalize(name)
        if normalized in parsed:
            raise RuntimeError(f"Duplicate lock entry: {name}")
        parsed[normalized] = {
            "name": name,
            "version": version,
            "hashes": hashes,
        }
    if "openai" not in parsed:
        raise RuntimeError("requirements-openai.lock does not contain openai")
    return parsed


def check_lock(path: Path = LOCK) -> None:
    locked = parse_lock(path)
    installed: dict[str, metadata.Distribution] = {}
    for installed_distribution in metadata.distributions():
        raw_name = installed_distribution.metadata.get("Name")
        if raw_name:
            installed[_canonicalize(raw_name)] = installed_distribution

    failures: list[str] = []
    for normalized, item in locked.items():
        distribution = installed.get(normalized)
        if distribution is None:
            failures.append(f"missing installed package: {item['name']}=={item['version']}")
        elif distribution.version != item["version"]:
            failures.append(
                f"installed version mismatch: {item['name']} "
                f"{distribution.version} != {item['version']}"
            )

    try:
        from pip._vendor.packaging.requirements import Requirement
    except ImportError as exc:  # pragma: no cover - pip is required by the launchers
        raise RuntimeError("pip packaging parser is unavailable") from exc

    pending = ["openai"]
    visited: set[str] = set()
    while pending:
        normalized = pending.pop()
        if normalized in visited:
            continue
        visited.add(normalized)
        distribution = installed.get(normalized)
        if distribution is None:
            continue
        for raw_requirement in distribution.requires or []:
            requirement = Requirement(raw_requirement)
            if requirement.marker is not None and not requirement.marker.evaluate(
                {"extra": ""}
            ):
                continue
            dependency = _canonicalize(requirement.name)
            if dependency not in locked:
                failures.append(
                    f"unlocked runtime dependency: {distribution.metadata.get('Name')} -> "
                    f"{requirement.name}"
                )
                continue
            pending.append(dependency)

    if failures:
        raise RuntimeError("OpenAI lock verification failed:\n - " + "\n - ".join(failures))
    print(f"{path.name} verified: {len(locked)} installed packages")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=LOCK)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.check:
            check_lock(arguments.output)
        else:
            generate(arguments.source, arguments.output)
    except (OSError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

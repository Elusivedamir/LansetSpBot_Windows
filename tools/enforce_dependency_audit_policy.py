#!/usr/bin/env python3
"""Fail closed when a pip-audit JSON report contains unresolved vulnerabilities."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def vulnerable_dependencies(report: Any) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        raise ValueError("pip-audit JSON root must be an object")
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError("pip-audit JSON does not contain a dependencies list")
    vulnerable: list[dict[str, Any]] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise ValueError("pip-audit dependency entry must be an object")
        vulns = dependency.get("vulns")
        if not isinstance(vulns, list):
            raise ValueError("pip-audit dependency entry has no vulnerability list")
        if vulns:
            vulnerable.append(dependency)
    return vulnerable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    arguments = parser.parse_args()
    try:
        payload = json.loads(arguments.report.read_text(encoding="utf-8"))
        vulnerable = vulnerable_dependencies(payload)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"ERROR: dependency audit evidence is invalid: {exc}")
        return 2
    if vulnerable:
        print("ERROR: unresolved dependency vulnerabilities are release-blocking:")
        for dependency in vulnerable:
            name = str(dependency.get("name") or "unknown")
            version = str(dependency.get("version") or "unknown")
            identifiers = ", ".join(
                str(item.get("id") or "unknown")
                for item in dependency.get("vulns", [])
                if isinstance(item, dict)
            )
            print(f" - {name}=={version}: {identifiers or 'unidentified vulnerability'}")
        return 1
    print("Dependency audit policy passed: no unresolved vulnerabilities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Fast dependency-free validation for GitHub workflow changes."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
USES = re.compile(r"^\s*uses:\s*([^@\s]+)@([^\s#]+)", re.MULTILINE)


def main() -> int:
    failures: list[str] = []
    workflow_files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    if not workflow_files:
        failures.append("no workflow files found")

    for path in workflow_files:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("name:"):
            failures.append(f"{path.relative_to(ROOT)}: missing top-level name")
        if "\non:" not in text:
            failures.append(f"{path.relative_to(ROOT)}: missing top-level on")
        if "\njobs:" not in text:
            failures.append(f"{path.relative_to(ROOT)}: missing top-level jobs")
        for action, ref in USES.findall(text):
            if action.startswith("./"):
                continue
            if not FULL_SHA.fullmatch(ref):
                failures.append(
                    f"{path.relative_to(ROOT)}: {action}@{ref} is not pinned to a full commit SHA"
                )

    dependabot = ROOT / ".github" / "dependabot.yml"
    if not dependabot.is_file():
        failures.append(".github/dependabot.yml is missing")
    else:
        text = dependabot.read_text(encoding="utf-8")
        for ecosystem in ("pip", "github-actions"):
            if f"package-ecosystem: {ecosystem}" not in text:
                failures.append(f"dependabot missing {ecosystem} updates")

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    print(f"Workflow contracts OK: {len(workflow_files)} workflows, all external actions full-SHA pinned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

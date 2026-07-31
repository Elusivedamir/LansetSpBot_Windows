#!/usr/bin/env python3
"""Fail-closed clean-checkout proof for release generation stages."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def checkout_status(project_root: str | Path) -> tuple[str, ...]:
    root = Path(project_root).resolve()
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git status failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return tuple(line for line in completed.stdout.splitlines() if line)


def assert_clean_checkout(
    project_root: str | Path,
    *,
    stage: str,
    evidence: str | Path | None = None,
) -> None:
    root = Path(project_root).resolve()
    dirty = checkout_status(root)
    target: Path | None = None
    if evidence is not None:
        target = Path(evidence).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "format": 1,
                    "stage": str(stage),
                    "project_root": str(root),
                    "clean": not dirty,
                    "status": list(dirty),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        # An evidence path inside the checkout is allowed only when repository
        # policy already excludes it (the release build uses ignored dist/).
        # Re-read after the write so the proof cannot accidentally create an
        # untracked file and still report a clean tree.
        dirty = checkout_status(root)
    if dirty:
        rendered = "\n".join(f"  {line}" for line in dirty)
        raise RuntimeError(
            f"Release proof modified the checkout at stage {stage!r}:\n{rendered}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--evidence", type=Path)
    arguments = parser.parse_args()
    try:
        assert_clean_checkout(
            arguments.root,
            stage=arguments.stage,
            evidence=arguments.evidence,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"checkout clean: {arguments.stage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

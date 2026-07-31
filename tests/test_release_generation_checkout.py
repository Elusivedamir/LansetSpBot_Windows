from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.release_checkout import assert_clean_checkout, checkout_status


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _git(*arguments: str, cwd: Path = PROJECT_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_generated_manifest_and_version_info_can_live_outside_checkout(tmp_path):
    before_status = checkout_status(PROJECT_ROOT)
    before_diff = _git("diff", "--binary")
    assert before_diff.returncode == 0, before_diff.stdout + before_diff.stderr

    manifest = tmp_path / "stage" / "source-SHA256SUMS.txt"
    version_info = tmp_path / "stage" / "windows_version_info.txt"
    commands = (
        [sys.executable, "tools/generate_manifest.py", "--output", str(manifest)],
        [
            sys.executable,
            "build/generate_windows_version_info.py",
            "--output",
            str(version_info),
        ],
    )
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

    assert manifest.is_file()
    assert version_info.is_file()
    assert checkout_status(PROJECT_ROOT) == before_status
    after_diff = _git("diff", "--binary")
    assert after_diff.returncode == 0, after_diff.stdout + after_diff.stderr
    assert after_diff.stdout == before_diff.stdout


def test_clean_checkout_proof_records_stage(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    assert _git("init", cwd=repository).returncode == 0
    (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    assert _git("add", "tracked.txt", cwd=repository).returncode == 0
    assert _git(
        "-c",
        "user.name=Release Test",
        "-c",
        "user.email=release@example.invalid",
        "commit",
        "-m",
        "baseline",
        cwd=repository,
    ).returncode == 0
    evidence = tmp_path / "clean.json"
    assert checkout_status(repository) == ()
    assert_clean_checkout(repository, stage="unit-test", evidence=evidence)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["stage"] == "unit-test"
    assert payload["clean"] is True
    assert payload["status"] == []


def test_clean_checkout_proof_fails_closed_on_untracked_file(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    assert _git("init", cwd=repository).returncode == 0
    (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    assert _git("add", "tracked.txt", cwd=repository).returncode == 0
    assert _git(
        "-c",
        "user.name=Release Test",
        "-c",
        "user.email=release@example.invalid",
        "commit",
        "-m",
        "baseline",
        cwd=repository,
    ).returncode == 0
    (repository / "unexpected.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Release proof modified the checkout"):
        assert_clean_checkout(repository, stage="after-generation")


def test_evidence_file_cannot_be_created_untracked_inside_checkout(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    assert _git("init", cwd=repository).returncode == 0
    (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    assert _git("add", "tracked.txt", cwd=repository).returncode == 0
    assert _git(
        "-c",
        "user.name=Release Test",
        "-c",
        "user.email=release@example.invalid",
        "commit",
        "-m",
        "baseline",
        cwd=repository,
    ).returncode == 0

    with pytest.raises(RuntimeError, match="Release proof modified the checkout"):
        assert_clean_checkout(
            repository,
            stage="bad-evidence-location",
            evidence=repository / "proof.json",
        )

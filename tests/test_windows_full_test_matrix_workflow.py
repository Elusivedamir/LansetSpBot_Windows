from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "windows-full-test-matrix.yml"
RUNNER = ROOT / "tools" / "run_windows_source_ci.ps1"


def test_windows_full_matrix_runs_both_supported_pythons() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "name: Windows CI" in text
    assert '          - "3.13"' in text
    assert '          - "3.14"' in text
    assert "runs-on: windows-2022" in text
    assert "fail-fast: false" in text


def test_only_windows_ci_runs_automatically_for_main_changes() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "\n  pull_request:\n" in text
    assert "\n  push:\n" in text
    assert "uses: ./.github/workflows/workflow-contracts.yml" in text
    assert "uses: ./.github/workflows/windows-release-proof.yml" in text
    assert "needs:\n      - contracts\n      - source-proof" in text
    assert "skip_source_tests: true" in text

    for reusable_name in ("workflow-contracts.yml", "windows-release-proof.yml"):
        reusable = (WORKFLOW.parent / reusable_name).read_text(encoding="utf-8")
        assert "\n  workflow_call:\n" in reusable
        assert "\n  workflow_dispatch:\n" in reusable
        assert "\n  pull_request:\n" not in reusable
        assert "\n  push:\n" not in reusable


def test_windows_full_matrix_installs_only_hash_locked_requirements() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    for lock in (
        "requirements-bootstrap.txt",
        "requirements-runtime.lock",
        "requirements-openai.lock",
        "requirements-dev-windows-x64.lock",
    ):
        assert f"--require-hashes -r {lock}" in text or (
            lock in text and "--require-hashes --no-build-isolation" in text
        )
    assert "python -m pip check" in text
    assert "requirements-openai.txt" not in text


def test_windows_full_matrix_keeps_complete_failure_evidence() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "if: always()" in text
    assert "dist/windows-ci/**" in text
    assert "if-no-files-found: error" in text
    assert "summary.json" in text


def test_windows_source_runner_executes_all_release_relevant_gates() -> None:
    text = RUNNER.read_text(encoding="utf-8-sig")
    expected = (
        '"manifest"',
        '"openai-lock"',
        '"runtime-lock-coverage"',
        '"compileall"',
        '"ruff"',
        '"mypy"',
        '"pytest-core-shard-$ShardNumber"',
        '"pytest-gui"',
        '"coverage-report"',
        '"critical-coverage"',
        '"source-self-test"',
        '"checkout-final"',
    )
    for gate in expected:
        assert gate in text


def test_windows_source_runner_preserves_ambiguous_failure_diagnostics() -> None:
    text = RUNNER.read_text(encoding="utf-8-sig")
    assert "--maxfail=1" not in text
    assert '"--idle-timeout-seconds", "300"' in text
    assert '"--total-timeout-seconds", "1500"' in text
    assert '"--total-timeout-seconds", "900"' in text
    assert '"pytest-core-shard-$ShardNumber.xml"' in text
    assert '"--junitxml", (Join-Path $EvidenceRoot "pytest-gui.xml")' in text
    assert "$CoreShardCount = 4" in text
    assert "checkout-final.json" in text
    assert "summary.json" in text


def test_release_reuses_source_proof_only_when_called_from_main_ci() -> None:
    text = (WORKFLOW.parent / "windows-release-proof.yml").read_text(encoding="utf-8")
    assert "skip_source_tests:" in text
    assert "default: false" in text
    assert '$buildArgs += "-SkipTests"' in text
    assert "!inputs.skip_source_tests" in text

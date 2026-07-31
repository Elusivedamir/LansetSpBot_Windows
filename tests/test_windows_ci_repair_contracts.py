from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON_SHA = "5fda3b95a4ea91299a34e894583c3862153e4b97"
UPLOAD_ARTIFACT_SHA = "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
ATTEST_SHA = "0f67c3f4856b2e3261c31976d6725780e5e4c373"


def _workflow_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(WORKFLOWS.glob("*.yml"))
    )


def test_node24_action_updates_are_full_sha_pinned() -> None:
    text = _workflow_text()
    assert f"actions/checkout@{CHECKOUT_SHA} # v7.0.1" in text
    assert f"actions/setup-python@{SETUP_PYTHON_SHA} # v7.0.0" in text
    assert f"actions/upload-artifact@{UPLOAD_ARTIFACT_SHA} # v7.0.1" in text
    assert f"actions/attest-build-provenance@{ATTEST_SHA} # v4.1.1" in text

    obsolete = (
        "11d5960a326750d5838078e36cf38b85af677262",
        "a26af69be951a213d495a4c3e4e4022e16d87065",
        "ea165f8d65b6e75b540449e92b4886f43607fa02",
        "e8998f949152b193b063cb0ec769d69d929409be",
    )
    assert all(value not in text for value in obsolete)


def test_github_action_dependabot_prs_do_not_consume_windows_runners() -> None:
    guard = (
        "github.event_name != 'pull_request' || "
        "!startsWith(github.head_ref, 'dependabot/github_actions/')"
    )
    full_matrix = (WORKFLOWS / "windows-full-test-matrix.yml").read_text(
        encoding="utf-8"
    )
    release = (WORKFLOWS / "windows-release-proof.yml").read_text(
        encoding="utf-8"
    )
    assert guard in full_matrix
    assert release.count(guard) == 2


def test_dependabot_groups_github_action_updates_without_missing_labels() -> None:
    text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "package-ecosystem: github-actions" in text
    assert "open-pull-requests-limit: 1" in text
    assert "groups:" in text
    assert '          - "*"' in text
    assert "labels:" not in text


def test_dev_lock_covers_python_313_and_314_windows_wheels() -> None:
    text = (ROOT / "requirements-dev-windows-x64.lock").read_text(encoding="utf-8")
    expected_hashes = (
        # coverage 7.15.0 cp314 win_amd64
        "3bb3040e9f4bbe26fcb0cd7cc85ac63e630d3f3a9c74f027abf4caa27e706663",
        # librt 0.13.0 cp314 win_amd64
        "a3dfe4edf10e8ed7e55b026a8bfc2c2a8704218b659cd4bffdf604fab966dc39",
        # mypy 2.2.0 cp314 win_amd64
        "6fc0e98b95e31755ca06d89f75fafa7820fbb3ea2caace6d83cba17625cd0acb",
    )
    assert "CPython 3.13/3.14 x64 on Windows" in text
    assert all(f"--hash=sha256:{value}" in text for value in expected_hashes)


def test_core_pytest_is_sharded_with_independent_watchdogs() -> None:
    source_runner = (ROOT / "tools" / "run_windows_source_ci.ps1").read_text(
        encoding="utf-8-sig"
    )
    release_runner = (ROOT / "build" / "build_windows_x64.ps1").read_text(
        encoding="utf-8-sig"
    )
    for text in (source_runner, release_runner):
        assert "$CoreShardCount = 4" in text
        assert "pytest-core-shard-$ShardNumber.log" in text
        assert "pytest-core-shard-$ShardNumber.xml" in text
        assert '"--idle-timeout-seconds", "300"' in text
        assert '"--total-timeout-seconds", "1500"' in text

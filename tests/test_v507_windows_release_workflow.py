from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "windows-release-proof.yml"


def test_windows_release_proof_workflow_is_present_and_manual() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "name: Windows Release Proof" in text
    assert "workflow_dispatch:" in text
    assert "runs-on: windows-2022" in text
    assert "permissions:\n  contents: read" in text


def test_workflow_runs_the_full_release_script_without_cmd_pause() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert r'build\build_windows_x64.ps1' in text
    assert "BUILD_WINDOWS_X64.cmd" not in text
    assert "pytest, coverage, Ruff, Mypy, SQLCipher, PyInstaller" in text


def test_workflow_verifies_and_uploads_release_evidence() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "Release ZIP checksum mismatch" in text
    assert "SBOM does not contain the OpenAI SDK" in text
    assert "coverage.json" in text
    assert "actions/upload-artifact@v4" in text
    assert "if-no-files-found: error" in text


def test_user_build_wrapper_still_points_to_the_release_script() -> None:
    wrapper = (ROOT / "BUILD_WINDOWS_X64.cmd").read_text(encoding="utf-8")
    assert r"build\build_windows_x64.ps1" in wrapper
    assert "exit /b %MARLEN_EXIT%" in wrapper

def test_checkout_is_verified_before_ci_evidence_is_created() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    clean_check = text.index("git status --porcelain=v1 --untracked-files=all")
    proof_directory = text.index(
        'New-Item -ItemType Directory -Path "ci-proof" -Force'
    )
    assert clean_check < proof_directory
    assert text.count("Checkout is not clean before the proof run.") == 1

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "windows-release-proof.yml"
BUILD = ROOT / "build" / "build_windows_x64.ps1"


def test_windows_build_log_is_streamed_live_and_persisted() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "id: windows_build" in text
    assert '-File ".\\build\\build_windows_x64.ps1" 2>&1 |' in text
    assert 'Tee-Object -FilePath "dist\\ci-proof\\build.log"' in text
    assert '*> "dist\\ci-proof\\build.log"' not in text
    assert 'Get-Content -LiteralPath "dist\\ci-proof\\build.log"' not in text


def test_cancelled_run_uploads_partial_failure_evidence() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "if: ${{ failure() || cancelled() }}" in text
    assert "dist/ci-proof/**" in text


def test_release_build_announces_long_running_stages() -> None:
    text = BUILD.read_text(encoding="utf-8-sig")
    assert "function Write-BuildStage" in text
    assert "[LansetSpBot build][stage]" in text
    assert 'Write-BuildStage "Installing runtime dependencies"' in text
    assert 'Write-BuildStage "Running core pytest diagnostics"' in text
    assert 'Write-BuildStage "Running GUI pytest diagnostics in isolated process"' in text
    assert "coverage run --parallel-mode -m pytest" in text
    assert 'Write-BuildStage "Building Windows application with PyInstaller"' in text
    assert 'Write-BuildStage "Running packaged self-test"' in text

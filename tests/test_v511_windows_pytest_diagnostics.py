from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "build_windows_x64.ps1"
WATCHDOG = ROOT / "tools" / "pytest_ci_watchdog.py"


def test_windows_pytest_diagnostics_are_split_and_fail_fast() -> None:
    text = BUILD.read_text(encoding="utf-8-sig")

    assert 'Write-BuildStage "Running core pytest diagnostics"' in text
    assert '--ignore "tests/test_gui_v45.py" tests' in text
    assert 'Write-BuildStage "Running GUI pytest diagnostics in isolated process"' in text
    assert '"tests/test_gui_v45.py"' in text
    assert text.count("--maxfail=1") == 2
    assert text.count("--tb=long") == 2
    assert text.count("-p tools.pytest_ci_watchdog") == 2


def test_split_pytest_diagnostics_keep_independent_evidence() -> None:
    text = BUILD.read_text(encoding="utf-8-sig")

    assert 'Tee-Object -FilePath "ci-proof\\pytest-core.log"' in text
    assert 'Tee-Object -FilePath "ci-proof\\pytest-gui.log"' in text
    assert '--junitxml "ci-proof\\pytest-core.xml"' in text
    assert '--junitxml "ci-proof\\pytest-gui.xml"' in text
    assert "pytest-diagnostics-summary.txt" in text
    assert "core_exit=$coreTestsExit" in text
    assert "gui_exit=$guiTestsExit" in text


def test_watchdog_dumps_all_threads_and_exits_on_hung_test() -> None:
    text = WATCHDOG.read_text(encoding="utf-8")

    assert "TEST_TIMEOUT_SECONDS = 180" in text
    assert "COLLECTION_TIMEOUT_SECONDS = 300" in text
    assert "faulthandler.dump_traceback_later" in text
    assert "all_threads=True" in text
    assert "exit=True" in text
    assert "pytest_runtest_logstart" in text
    assert "pytest_runtest_logfinish" in text

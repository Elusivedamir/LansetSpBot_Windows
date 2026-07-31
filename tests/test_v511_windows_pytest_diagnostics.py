from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "build_windows_x64.ps1"
WATCHDOG = ROOT / "tools" / "pytest_ci_watchdog.py"


def test_windows_pytest_diagnostics_are_split_and_fail_fast() -> None:
    text = BUILD.read_text(encoding="utf-8-sig")

    assert 'Write-BuildStage "Running core pytest diagnostics in four file shards"' in text
    assert '$CoreShardCount = 4' in text
    assert 'Where-Object { $_.Name -ne "test_gui_v45.py" }' in text
    assert 'Write-BuildStage "Running GUI pytest diagnostics in isolated process"' in text
    assert '"tests/test_gui_v45.py"' in text
    assert text.count("tools\\run_ci_subprocess.py") == 2
    assert text.count("--tb=long") == 2
    assert text.count("tools.pytest_ci_watchdog") == 2
    assert "--maxfail=1" not in text
    assert '"--total-timeout-seconds", "1500"' in text
    assert "--total-timeout-seconds 900" in text
    assert '"--idle-timeout-seconds", "300"' in text


def test_split_pytest_diagnostics_keep_independent_evidence() -> None:
    text = BUILD.read_text(encoding="utf-8-sig")

    assert '"pytest-core-shard-$ShardNumber.log"' in text
    assert '--log (Join-Path $ProofRoot "pytest-gui.log")' in text
    assert '"pytest-core-shard-$ShardNumber.xml"' in text
    assert '--junitxml (Join-Path $ProofRoot "pytest-gui.xml")' in text
    assert '$coreShardResults += "core_shard_$ShardNumber=$ShardExit"' in text
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

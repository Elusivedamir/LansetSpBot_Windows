"""The hash-locked runtime graph must install on every advertised interpreter.

Found on a user's machine, not in this repository: the launcher accepted their
Python 3.14, pip fetched sqlcipher3-0.6.2-cp314-cp314-win_amd64.whl, and the
lock only carried the cp313 hashes. pip reports that as

    THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE
    ... someone may have tampered with them.

so a plain packaging gap looks like a supply-chain attack, and the application
cannot start at all on a documented-supported Python.
"""

from __future__ import annotations

import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools" / "check_lock_coverage.py"
LOCK = ROOT / "requirements-runtime.lock"


def _pypi_reachable() -> bool:
    try:
        urllib.request.urlopen("https://pypi.org/pypi/pyasn1/json", timeout=20).close()
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    return True


def _run_checker(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), *extra],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )


needs_pypi = pytest.mark.skipif(
    not _pypi_reachable(), reason="PyPI is unreachable from this machine"
)


@needs_pypi
def test_the_shipped_lock_installs_on_every_supported_python() -> None:
    completed = _run_checker()
    assert completed.returncode == 0, completed.stdout + completed.stderr


@needs_pypi
def test_the_check_rejects_a_lock_that_misses_an_interpreter(tmp_path: Path) -> None:
    """Guard the guard: a check that cannot fail proves nothing."""

    lines = LOCK.read_text(encoding="utf-8").splitlines(keepends=True)
    # Drop the cp314 sqlcipher3 wheel, reproducing the shipped defect exactly.
    crippled = [
        line
        for line in lines
        if "7de6133b19aec27b30698267cc2a0ea6e82c21d9a81d349cf0b480439fb549ac" not in line
    ]
    assert len(crippled) == len(lines) - 1, "the cp314 sqlcipher3 hash is missing"
    # The preceding hash line now needs its trailing continuation removed.
    text = "".join(crippled).replace(
        "--hash=sha256:9fb7109981583b631ac795e7e955d4bf78058f64b54c7f334ccc437adc322d4b \\\n",
        "--hash=sha256:9fb7109981583b631ac795e7e955d4bf78058f64b54c7f334ccc437adc322d4b\n",
    )
    broken = tmp_path / "broken.lock"
    broken.write_text(text, encoding="utf-8")

    completed = _run_checker("--lock", str(broken))
    assert completed.returncode == 1
    assert "sqlcipher3" in completed.stdout
    assert "cp314" in completed.stdout


def test_the_launcher_and_the_readme_agree_on_supported_versions() -> None:
    """The lock is checked against SUPPORTED_TAGS, so that list must match
    what the launcher actually accepts and what the README promises."""

    from tools.check_lock_coverage import SUPPORTED_TAGS

    launcher = (ROOT / "RUN_FROM_SOURCE_WINDOWS.ps1").read_text(encoding="utf-8")
    readme = (ROOT / "README.txt").read_text(encoding="utf-8")
    for tag in SUPPORTED_TAGS:
        version = f"3.{tag.removeprefix('cp3')}"
        assert f'"-{version}"' in launcher, f"the launcher does not probe {version}"
        assert version in readme, f"README.txt does not document {version}"


def test_the_release_build_runs_the_check() -> None:
    script = (ROOT / "build" / "build_windows_x64.ps1").read_text(encoding="utf-8")
    assert "check_lock_coverage.py" in script
    assert "Runtime lock does not cover" in script

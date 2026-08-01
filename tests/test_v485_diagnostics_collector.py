"""The first-run diagnostics report must be useful and safe to send.

Useful: it has to capture the failure, not just say that one happened.
Safe: it must never carry a credential, a session or the database itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "tools" / "collect_diagnostics.py"

PLANTED = {
    "proxy_password": "SuperSecret123",
    "api_hash": "0123456789abcdef0123456789abcdef",
    "openai_key": "sk-proj-ABCDEFGH12345678xyz",
    "phone": "+79991234567",
}


def _run_collector(tmp_path: Path, profile: Path, *extra: str) -> tuple[int, str]:
    output = tmp_path / "report.txt"
    environment = dict(os.environ)
    environment.pop("LANSETSPBOT_DATA_DIR", None)
    environment.update(
        {
            "MARLEN_DATA_DIR": str(profile),
            "APPDATA": str(
                tmp_path
                / "LansetAuditSensitiveUser42"
                / "AppData"
                / "Roaming"
            ),
            "QT_QPA_PLATFORM": "offscreen",
        }
    )
    completed = subprocess.run(
        [sys.executable, str(COLLECTOR), "--output", str(output), *extra],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        env=environment,
    )
    return completed.returncode, output.read_text(encoding="utf-8")


def _profile_with_secrets(tmp_path: Path) -> Path:
    profile = tmp_path / "profile"
    (profile / "logs").mkdir(parents=True)
    (profile / "sessions").mkdir(parents=True)
    (profile / "logs" / "marlen.log").write_text(
        "2026-01-01 00:00:00 ERROR a: "
        f"ProxyError: password={PLANTED['proxy_password']}; "
        f"api_hash='{PLANTED['api_hash']}'\n"
        "2026-01-01 00:00:01 INFO a: "
        f"phone={PLANTED['phone']} key={PLANTED['openai_key']}\n"
        r"2026-01-01 00:00:02 INFO a: session C:\Users\Ivan\Marlen\sessions\main.session"
        "\n",
        encoding="utf-8",
    )
    # Artefacts that must never appear in the report.
    (profile / "marlen.db").write_bytes(b"\x00\xff" * 2048)
    (profile / "sessions" / "main.session").write_bytes(b"TELEGRAM-SESSION-BYTES")
    (profile / ".secrets.json").write_bytes(b"LSPBV1\x00ENCRYPTED-SECRET-BLOB")
    return profile


def test_the_report_is_produced_even_when_the_app_cannot_start(tmp_path: Path) -> None:
    code, report = _run_collector(tmp_path, _profile_with_secrets(tmp_path))
    assert code == 0, "the collector must never fail because the app fails"
    assert "ENVIRONMENT" in report
    assert "DEPENDENCIES" in report
    assert "PROFILE LAYOUT" in report
    assert "FILE INTEGRITY" in report


def test_the_report_carries_no_planted_secret(tmp_path: Path) -> None:
    _, report = _run_collector(tmp_path, _profile_with_secrets(tmp_path))
    leaked = [name for name, value in PLANTED.items() if value in report]
    assert leaked == [], f"the diagnostics report leaked: {leaked}"
    assert "main.session" not in report
    assert "TELEGRAM-SESSION-BYTES" not in report
    assert "ENCRYPTED-SECRET-BLOB" not in report


def test_the_report_redacts_user_project_and_profile_paths(tmp_path: Path) -> None:
    profile = _profile_with_secrets(tmp_path)
    _, report = _run_collector(tmp_path, profile, "--skip-self-test")

    assert "LansetAuditSensitiveUser42" not in report
    assert str(profile) not in report
    assert str(ROOT) not in report
    assert "<APP_PROFILE>" in report
    assert "<APPDATA>" in report
    assert "<PROJECT_ROOT>" in report


def test_the_report_states_whether_the_database_is_encrypted_without_reading_it(
    tmp_path: Path,
) -> None:
    _, report = _run_collector(tmp_path, _profile_with_secrets(tmp_path))
    assert "database_encrypted:" in report
    # The fact is reported; the contents never are.
    assert "\x00\xff" not in report


def test_the_report_records_the_self_test_outcome(tmp_path: Path) -> None:
    """The traceback is the whole point when startup fails."""

    _, report = _run_collector(tmp_path, _profile_with_secrets(tmp_path))
    assert "STARTUP SELF-TEST" in report
    assert "exit_code:" in report
    assert "--- stdout ---" in report and "--- stderr ---" in report


def test_the_self_test_can_be_skipped(tmp_path: Path) -> None:
    _, report = _run_collector(
        tmp_path, _profile_with_secrets(tmp_path), "--skip-self-test"
    )
    assert "skipped on request" in report


def test_the_report_verifies_the_shipped_file_manifest(tmp_path: Path) -> None:
    _, report = _run_collector(
        tmp_path, _profile_with_secrets(tmp_path), "--skip-self-test"
    )
    assert "mismatched: 0" in report
    assert "missing   : 0" in report


def test_a_missing_profile_is_reported_rather_than_crashing(tmp_path: Path) -> None:
    code, report = _run_collector(
        tmp_path, tmp_path / "does-not-exist", "--skip-self-test"
    )
    assert code == 0
    assert "MISSING" in report


def test_external_text_is_withheld_when_the_sanitiser_is_unavailable(
    tmp_path: Path,
) -> None:
    """Without core.redaction the collector must quote nothing external.

    A project too broken to import is precisely when the report matters most,
    and precisely when it cannot be checked for credentials.
    """

    import importlib.util

    specification = importlib.util.spec_from_file_location(
        "collect_diagnostics_isolated", COLLECTOR
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    module._SANITIZE = None
    report = module.Report()
    report.raw_block(f"ProxyError: password={PLANTED['proxy_password']}")
    rendered = report.render()
    assert PLANTED["proxy_password"] not in rendered
    assert "withheld" in rendered


def test_the_windows_entry_point_calls_the_collector() -> None:
    batch = (ROOT / "3_COLLECT_DIAGNOSTICS.cmd").read_text(encoding="utf-8")
    assert "tools\\collect_diagnostics.py" in batch
    assert "pause" in batch, "the window must stay open so the user can read errors"
    # It must never need elevation or touch Windows security settings.
    lowered = batch.lower()
    for forbidden in ("runas", "defender", "add-mppreference", "del /s"):
        assert forbidden not in lowered


@pytest.mark.parametrize(
    "artefact", ["marlen.db", ".secrets.json", ".master-key.dpapi", "sessions"]
)
def test_the_never_collect_list_is_explicit(artefact: str) -> None:
    source = COLLECTOR.read_text(encoding="utf-8")
    assert artefact in source


def test_the_shipped_manifest_matches_the_shipped_files() -> None:
    """A manifest that has drifted proves nothing about a user's copy."""

    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "generate_manifest.py"), "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_manifest_lists_no_build_machine_cache() -> None:
    """Cache directories are absent from a fresh checkout, so listing them
    would make every clean copy look corrupted."""

    manifest = (ROOT / "SHA256SUMS.txt").read_text(encoding="utf-8")
    for cache in (".mypy_cache", ".ruff_cache", ".pytest_cache", "__pycache__"):
        assert cache not in manifest, f"{cache} must not be in SHA256SUMS.txt"

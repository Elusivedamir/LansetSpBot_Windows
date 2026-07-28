#!/usr/bin/env python3
"""Collect a shareable first-run diagnostics report.

The report answers "why did it not start" without ever exposing an account.
It records the environment, the resolved profile layout, the outcome of the
packaged self-test and the redacted application log.

Never collected: the SQLite database, Telegram session files, the local secret
store and the DPAPI-wrapped master key. Every collected line is passed through
core.redaction a second time, so the report stays safe to send even if a log
handler was misconfigured.

Usage:
    python tools/collect_diagnostics.py [--output PATH] [--skip-self-test]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SELF_TEST_TIMEOUT_SECONDS = 180
MAX_LOG_BYTES = 512 * 1024

# Anything that can identify or grant access to a Telegram account.
NEVER_COLLECT = (
    "marlen.db",
    ".secrets.json",
    ".master-key.dpapi",
    "sessions",
)


def _load_sanitizer() -> Callable[[str], str] | None:
    try:
        from core.redaction import sanitize_log_text
    except Exception:
        # A project that cannot even be imported is exactly the failure this
        # report exists to capture, so the report is still produced - but
        # without a sanitiser nothing external may be quoted into it.
        return None
    return sanitize_log_text


_SANITIZE = _load_sanitizer()
_WITHHELD = (
    "<withheld: core.redaction could not be imported, so this content "
    "cannot be checked for credentials>"
)


def _private_path_aliases() -> tuple[tuple[str, str], ...]:
    candidates = (
        (str(PROJECT_ROOT), "<PROJECT_ROOT>"),
        (os.environ.get("MARLEN_DATA_DIR", ""), "<APP_PROFILE>"),
        (os.environ.get("APPDATA", ""), "<APPDATA>"),
        (os.environ.get("USERPROFILE", ""), "<USER_PROFILE>"),
        (str(Path.home()), "<USER_PROFILE>"),
    )
    aliases: dict[str, tuple[str, str]] = {}
    for raw, replacement in candidates:
        prefix = str(raw or "").strip().rstrip("\\/")
        if not prefix or prefix in {".", "\\", "/"}:
            continue
        aliases.setdefault(prefix.casefold(), (prefix, replacement))
        alternate = (
            prefix.replace("\\", "/")
            if "\\" in prefix
            else prefix.replace("/", "\\")
        )
        if alternate != prefix:
            aliases.setdefault(alternate.casefold(), (alternate, replacement))
    # A profile can live inside APPDATA, which itself lives inside USERPROFILE.
    # Replace the most specific prefix before its parent.
    return tuple(sorted(aliases.values(), key=lambda item: len(item[0]), reverse=True))


def _redact_private_paths(text: str) -> str:
    result = str(text)
    for prefix, replacement in _private_path_aliases():
        result = re.sub(re.escape(prefix), replacement, result, flags=re.IGNORECASE)
    return result


def _redact(text: str) -> str:
    sanitized = (
        str(text)
        if _SANITIZE is None
        else "\n".join(_SANITIZE(line) for line in str(text).splitlines())
    )
    return _redact_private_paths(sanitized)


class Report:
    """Collects the report.

    ``line`` carries text this script composed itself; ``raw_block`` carries
    text that came from somewhere else - a log file, a subprocess. External
    text is only ever emitted when the sanitiser is available.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []

    def section(self, title: str) -> None:
        self._lines.append("")
        self._lines.append("=" * 72)
        self._lines.append(title)
        self._lines.append("=" * 72)

    def line(self, text: str = "") -> None:
        self._lines.append(_redact(text))

    def raw_block(self, text: str) -> None:
        if _SANITIZE is None:
            self._lines.append(_WITHHELD)
            return
        for entry in str(text).splitlines():
            self._lines.append(_redact(entry))

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


def _describe_environment(report: Report) -> None:
    report.section("ENVIRONMENT")
    report.line(f"generated_at   : {datetime.now(timezone.utc).isoformat()}")
    report.line(f"platform       : {platform.platform()}")
    report.line(f"os.name        : {os.name}")
    report.line(f"machine        : {platform.machine()}")
    report.line(f"python         : {sys.version.splitlines()[0]}")
    report.line(f"python_bits    : {8 * struct.calcsize('P')}")
    report.line(f"python_exe     : {sys.executable}")
    report.line(f"project_root   : {PROJECT_ROOT}")
    report.line(f"cwd            : {Path.cwd()}")
    report.line(f"frozen         : {bool(getattr(sys, 'frozen', False))}")
    for name in ("MARLEN_DATA_DIR", "APPDATA", "QT_QPA_PLATFORM", "QT_SCALE_FACTOR"):
        report.line(f"env {name:<14}: {os.environ.get(name, '<unset>')}")


def _describe_dependencies(report: Report) -> None:
    report.section("DEPENDENCIES")
    for module in (
        "PySide6",
        "telethon",
        "cryptography",
        "sqlcipher3",
        "socks",
        "openai",
    ):
        try:
            imported = __import__(module)
            version = getattr(imported, "__version__", "<no __version__>")
            report.line(f"{module:<14}: {version}")
        except Exception as exc:
            report.line(f"{module:<14}: NOT IMPORTABLE ({type(exc).__name__}: {exc})")
    try:
        from storage.sqlcipher_driver import SQLCIPHER_AVAILABLE, _DRIVER

        report.line(f"sqlcipher_used: {SQLCIPHER_AVAILABLE} ({_DRIVER.__name__})")
    except Exception as exc:
        report.line(f"sqlcipher_used: unknown ({type(exc).__name__}: {exc})")
    # Whether SQLCipher may lock its key pages here. False means the database is
    # still encrypted but key pages can reach the page file - and it is the
    # setting whose forced use crashed the process on some Windows machines.
    try:
        from core.secure_memory import secure_memory_available

        report.line(f"locked_memory : {secure_memory_available()}")
    except Exception as exc:
        report.line(f"locked_memory : unknown ({type(exc).__name__}: {exc})")


def _describe_profile(report: Report) -> None:
    report.section("PROFILE LAYOUT")
    try:
        from core.paths import APP_PATHS

        paths = APP_PATHS
    except Exception as exc:
        report.line(f"could not resolve profile paths: {type(exc).__name__}: {exc}")
        return

    for label, path in (
        ("root", paths.root),
        ("database", paths.database),
        ("logs", paths.logs),
        ("sessions", paths.sessions),
        ("backups", paths.backups),
    ):
        target = Path(path)
        try:
            if not target.exists():
                report.line(f"{label:<9}: MISSING  {target}")
                continue
            if target.is_dir():
                count = sum(1 for _ in target.iterdir())
                report.line(f"{label:<9}: dir, {count} entries  {target}")
            else:
                report.line(f"{label:<9}: file, {target.stat().st_size} bytes  {target}")
        except OSError as exc:
            report.line(f"{label:<9}: UNREADABLE ({exc})  {target}")

    # Encryption is reported as a yes/no fact; no content is ever read out.
    database = Path(paths.database)
    try:
        if database.is_file() and database.stat().st_size > 0:
            header = database.open("rb").read(16)
            plaintext = header == b"SQLite format 3\x00"
            report.line(
                f"database_encrypted: {not plaintext} "
                f"({'plaintext SQLite header' if plaintext else 'no plaintext header'})"
            )
    except OSError as exc:
        report.line(f"database_encrypted: unknown ({exc})")


def _verify_manifest(report: Report) -> None:
    report.section("FILE INTEGRITY (SHA256SUMS.txt)")
    manifest = PROJECT_ROOT / "SHA256SUMS.txt"
    if not manifest.is_file():
        report.line("SHA256SUMS.txt is missing")
        return
    mismatched: list[str] = []
    missing: list[str] = []
    checked = 0
    for entry in manifest.read_text(encoding="utf-8").splitlines():
        parts = entry.strip().split(None, 1)
        if len(parts) != 2:
            continue
        expected, relative = parts
        target = PROJECT_ROOT / relative
        if not target.is_file():
            missing.append(relative)
            continue
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        checked += 1
        if digest != expected:
            mismatched.append(relative)
    report.line(f"checked   : {checked}")
    report.line(f"missing   : {len(missing)}")
    report.line(f"mismatched: {len(mismatched)}")
    for relative in (missing + mismatched)[:40]:
        report.line(f"  {relative}")


def _run_self_test(report: Report) -> None:
    report.section("STARTUP SELF-TEST (no Telegram access)")
    command = [sys.executable, str(PROJECT_ROOT / "main.py"), "--self-test"]
    report.line(f"command: {' '.join(command)}")
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - fully internal command
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=SELF_TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        report.line(f"RESULT: TIMED OUT after {SELF_TEST_TIMEOUT_SECONDS}s")
        return
    except OSError as exc:
        report.line(f"RESULT: could not start ({exc})")
        return
    report.line(f"exit_code: {completed.returncode}")
    report.line(f"duration : {time.monotonic() - started:.1f}s")
    report.line("--- stdout ---")
    report.raw_block(completed.stdout or "<empty>")
    report.line("--- stderr ---")
    report.raw_block(completed.stderr or "<empty>")


def _collect_logs(report: Report) -> None:
    report.section("APPLICATION LOG (redacted)")
    try:
        from core.paths import APP_PATHS

        log_dir = Path(APP_PATHS.logs)
    except Exception as exc:
        report.line(f"could not resolve the log directory: {exc}")
        return
    if not log_dir.is_dir():
        report.line(f"no log directory yet: {log_dir}")
        return
    # glob() swallows a permission error and returns nothing, which reads as
    # "the application never logged" - the opposite of the truth when the
    # directory simply cannot be listed. The listing is done explicitly so an
    # unreadable directory is reported as unreadable.
    try:
        files = sorted(
            entry for entry in log_dir.iterdir() if entry.name.startswith("marlen.log")
        )
    except OSError as exc:
        report.line(f"LOG DIRECTORY UNREADABLE: {exc}")
        report.line(
            "The log exists but this process may not list the directory. "
            "Check its permissions before concluding anything from its absence."
        )
        return
    if not files:
        report.line("no marlen.log yet - the application has not logged anything")
        return
    for path in files:
        report.line("")
        report.line(f"--- {path.name} ({path.stat().st_size} bytes, tail) ---")
        try:
            data = path.read_bytes()[-MAX_LOG_BYTES:]
        except OSError as exc:
            report.line(f"unreadable: {exc}")
            continue
        report.raw_block(data.decode("utf-8", errors="replace"))


def _confirm_nothing_sensitive(report: Report, rendered: str) -> None:
    report.section("SAFETY CHECK")
    for name in NEVER_COLLECT:
        report.line(f"never collected: {name}")
    leaks = [
        marker
        for marker in ("BEGIN PRIVATE KEY", "LSPBV1\x00")
        if marker in rendered
    ]
    report.line(f"raw secret markers found in report: {leaks or 'none'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "lansetspbot-diagnostics.txt"),
        help="where to write the report",
    )
    parser.add_argument(
        "--skip-self-test",
        action="store_true",
        help="do not launch the startup self-test",
    )
    arguments = parser.parse_args()

    report = Report()
    report.line("LansetSpBot first-run diagnostics")
    report.line("Safe to share: credentials, sessions and the database are excluded.")

    _describe_environment(report)
    _describe_dependencies(report)
    _describe_profile(report)
    _verify_manifest(report)
    if not arguments.skip_self_test:
        _run_self_test(report)
    else:
        report.section("STARTUP SELF-TEST (no Telegram access)")
        report.line("skipped on request")
    _collect_logs(report)

    rendered = report.render()
    _confirm_nothing_sensitive(report, rendered)
    rendered = report.render()

    destination = Path(arguments.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8")
    print(f"Diagnostics written to: {destination}")
    print(f"Size: {destination.stat().st_size} bytes")
    print("Send this single file. It contains no credentials, session or database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

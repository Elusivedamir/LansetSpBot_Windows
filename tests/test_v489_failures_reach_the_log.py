"""A failure the operator can see must be reconstructible afterwards.

An operator reported "Ошибка подключения: OperationalError: database is locked"
and sent marlen.log. The log contained nothing about it: the authorization
worker emitted the message to the dialog and swallowed the exception, so the
only record of the failure was a screenshot.

The diagnostics report had the mirror-image problem - it printed "the
application has not logged anything" when the log directory merely could not
be listed, turning a permissions problem into an apparent absence of evidence.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_the_authorization_worker_has_a_logger() -> None:
    from gui import auth_worker

    assert isinstance(auth_worker.log, logging.Logger)
    assert auth_worker.log.name == "gui.auth_worker"


def test_an_unexpected_authorization_failure_is_logged_with_a_traceback(
    caplog, monkeypatch
) -> None:
    """The operator sees a dialog; the log must carry the traceback behind it."""

    from gui import auth_worker

    worker = auth_worker.TelegramAuthWorker.__new__(
        auth_worker.TelegramAuthWorker
    )
    emitted: list[str] = []

    class _Signal:
        @staticmethod
        def emit(text: str) -> None:
            emitted.append(text)

    monkeypatch.setattr(worker, "failed", _Signal(), raising=False)
    monkeypatch.setattr(
        worker, "_safe_error_text", lambda exc: f"{type(exc).__name__}: {exc}"
    )

    def explode(_coroutine):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(auth_worker.asyncio, "run", explode)
    monkeypatch.setattr(worker, "_run", lambda: None, raising=False)

    with caplog.at_level(logging.ERROR, logger="gui.auth_worker"):
        auth_worker.TelegramAuthWorker.run(worker)

    assert emitted, "the operator must still be told"
    records = [record for record in caplog.records if record.name == "gui.auth_worker"]
    assert records, "the failure never reached the log"
    logged = records[0].getMessage()
    assert "RuntimeError" in logged
    assert "database is locked" in logged
    assert "Traceback" in logged, "a traceback is what makes the failure diagnosable"


@pytest.mark.parametrize(
    "handler",
    [
        "Telegram не принял номер телефона",
        "Неверный код подтверждения",
        "Код подтверждения устарел",
        "Неверный пароль двухэтапной аутентификации",
        "Telegram не ответил вовремя",
    ],
)
def test_every_authorization_outcome_leaves_a_trace(handler: str) -> None:
    """Each user-visible branch must log, not only the catch-all."""

    source = (ROOT / "gui" / "auth_worker.py").read_text(encoding="utf-8")
    index = source.index(handler)
    preceding = source[:index]
    assert "log." in preceding[-400:], f"the branch for {handler!r} logs nothing"


def test_the_collector_reports_an_unreadable_log_directory(tmp_path: Path) -> None:
    """Reported by a real profile: the directory existed and could not be listed,
    and the report claimed the application had never logged anything."""

    source = (ROOT / "tools" / "collect_diagnostics.py").read_text(encoding="utf-8")
    assert "LOG DIRECTORY UNREADABLE" in source
    # The silent-glob path is what produced the wrong answer.
    collect = source[source.index("def _collect_logs") :]
    assert 'log_dir.glob("marlen.log*")' not in collect


def test_the_collector_still_runs_when_a_profile_directory_is_unreadable(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    (profile / "logs").mkdir(parents=True)
    (profile / "logs" / "marlen.log").write_text("hello\n", encoding="utf-8")
    output = tmp_path / "report.txt"
    import os

    environment = {
        **dict(os.environ),
        "MARLEN_DATA_DIR": str(profile),
        "QT_QPA_PLATFORM": "offscreen",
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "collect_diagnostics.py"),
            "--output",
            str(output),
            "--skip-self-test",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        env=environment,
    )
    assert completed.returncode == 0
    report = output.read_text(encoding="utf-8")
    assert "marlen.log" in report
    assert "has not logged anything" not in report

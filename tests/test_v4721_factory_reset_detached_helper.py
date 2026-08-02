from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from tests.conftest import open_project_database


def test_detached_helper_resets_only_after_parent_is_gone_and_rebuilds_schema(tmp_path):
    root = tmp_path / "Marlen"
    sessions = root / "sessions"
    logs = root / "logs"
    backups = root / "backups"
    sessions.mkdir(parents=True)
    logs.mkdir()
    backups.mkdir()
    (root / "marlen.db").write_bytes(b"old-invalid-database")
    (root / ".secrets.json").write_text(
        '{"telegram.api_hash":"secret"}', encoding="utf-8"
    )
    (sessions / "main.session").write_text("session", encoding="utf-8")
    (logs / "marlen.log").write_text("old-log", encoding="utf-8")
    (backups / "old.backup").write_text("backup", encoding="utf-8")

    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("LANSETSPBOT_DATA_DIR", None)
    environment["MARLEN_DATA_DIR"] = str(root)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "main.py"),
            "--factory-reset-helper",
            "0",
            "--factory-reset-no-relaunch",
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / ".secrets.json").exists()
    assert not (sessions / "main.session").exists()
    assert not (logs / "marlen.log").exists()
    assert not (backups / "old.backup").exists()

    with open_project_database(root / "marlen.db") as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "comment_campaigns" in tables
        assert "join_campaigns" in tables
        assert "settings" in tables
        assert "logs" in tables
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    marker = root / ".factory-reset-result.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert "пустая база" in payload["message"]


def test_scheduled_reset_success_closes_without_second_modal(monkeypatch):
    from gui.app import MarlenApp

    events: list[object] = []
    fake = SimpleNamespace(
        _factory_reset_helper_pid=None,
        _set_shutdown_progress_text=lambda text: events.append(("progress", text)),
        _finalize_quit=lambda: events.append("quit"),
        _close_shutdown_progress=lambda: events.append("close-progress"),
    )
    monkeypatch.setattr(
        "gui.app.QTimer.singleShot",
        lambda _delay, callback: callback(),
    )
    monkeypatch.setattr(
        "gui.app.QMessageBox.information",
        lambda *_args, **_kwargs: events.append("dialog"),
    )

    MarlenApp._finish_factory_reset_success(
        fake,
        SimpleNamespace(scheduled=True, helper_pid=321),
    )

    assert fake._factory_reset_helper_pid == 321
    assert events == [("progress", "Подготовка завершена. Закрытие LansetSpBot…"), "quit"]

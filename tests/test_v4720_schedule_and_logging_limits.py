from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import core.logging_setup as logging_setup
from core.campaign_schedule import generate_random_slots
from core.paths import AppPaths
from storage.database import Database
from storage.db_settings import (
    MAX_PERSISTENT_LOG_ENTRY_BYTES,
    PERSISTENT_LOG_BUDGET_BYTES,
)

UTC = timezone.utc


class _LowRng:
    def uniform(self, low, _high):
        return low


def test_comment_schedule_has_no_fixed_15_to_30_minute_rule():
    start = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    end = start + timedelta(hours=24)

    for limit in (1, 4, 40, 223, 1000):
        slots = generate_random_slots(start, end, limit, rng=_LowRng())
        interval = timedelta(hours=24) / limit
        assert len(slots) == limit
        assert slots[0] - start == interval * 0.10

    slots_40 = generate_random_slots(start, end, 40, rng=_LowRng())
    assert slots_40[0] - start < timedelta(minutes=15)


def test_persistent_activity_logs_are_pruned_to_five_mib(tmp_path):
    db = Database(tmp_path / "bounded-journal.db")
    payload = "Ж" * 4_000
    for index in range(800):
        db.insert_log("INFO", f"{index:04d}:{payload}")

    with db.get_connection() as conn:
        retained = int(
            conn.execute(
                """SELECT COALESCE(SUM(
                       length(CAST(level AS BLOB))
                     + length(CAST(message AS BLOB))
                     + length(CAST(created_at AS BLOB))
                     + 48), 0)
                   FROM logs"""
            ).fetchone()[0]
        )
        first_id = int(conn.execute("SELECT MIN(id) FROM logs").fetchone()[0])

    assert retained <= PERSISTENT_LOG_BUDGET_BYTES
    assert first_id > 1


def test_single_persistent_log_entry_is_utf8_bounded(tmp_path):
    db = Database(tmp_path / "bounded-entry.db")
    db.insert_log("INFO", "Я" * MAX_PERSISTENT_LOG_ENTRY_BYTES)
    row = db.get_logs(limit=1)[0]

    assert len(row["message"].encode("utf-8")) <= MAX_PERSISTENT_LOG_ENTRY_BYTES
    assert row["message"].endswith("… [обрезано]")


def test_file_logs_use_one_bounded_shareable_file_and_cleanup_old_rotations(
    monkeypatch, tmp_path
):
    paths = AppPaths(
        root=tmp_path,
        database=tmp_path / "marlen.db",
        logs=tmp_path / "logs",
        sessions=tmp_path / "sessions",
        backups=tmp_path / "backups",
    )
    paths.ensure()
    log_file = paths.logs / "marlen.log"
    # Tiny cap proves rollover without allocating the production 16 MiB file.
    monkeypatch.setattr(logging_setup, "FILE_LOG_SEGMENT_BYTES", 4096)
    monkeypatch.setattr(logging_setup, "FILE_LOG_RETAIN_BYTES", 3072)
    log_file.write_bytes(b"x" * 5000)
    for name in ("marlen.log.1", "marlen.log.2", "marlen.log.5"):
        (paths.logs / name).write_bytes(b"legacy")

    monkeypatch.setattr(logging_setup, "APP_PATHS", paths)
    root = logging.getLogger()
    handler = None
    try:
        logging_setup.setup_logging()
        handler = next(
            item
            for item in root.handlers
            if isinstance(item, RotatingFileHandler)
            and item.baseFilename == str(log_file)
        )
        assert handler.maxBytes == 4096
        assert handler.backupCount == 0
        assert logging_setup.FILE_LOG_BACKUP_COUNT == 0
        assert log_file.stat().st_size <= 4096
        assert list(paths.logs.glob("marlen.log.*")) == []

        logging.getLogger("single-file-test").warning("ONE_FILE_MARKER")
        handler.flush()
        assert "ONE_FILE_MARKER" in log_file.read_text(
            encoding="utf-8", errors="replace"
        )
    finally:
        if handler is not None:
            root.removeHandler(handler)
            handler.close()


def test_persistent_activity_is_mirrored_into_same_shareable_log(
    monkeypatch, tmp_path
):
    paths = AppPaths(
        root=tmp_path,
        database=tmp_path / "marlen.db",
        logs=tmp_path / "logs",
        sessions=tmp_path / "sessions",
        backups=tmp_path / "backups",
    )
    paths.ensure()
    monkeypatch.setattr(logging_setup, "APP_PATHS", paths)
    root = logging.getLogger()
    handler = None
    try:
        logging_setup.setup_logging()
        handler = next(
            item
            for item in root.handlers
            if isinstance(item, RotatingFileHandler)
            and item.baseFilename == str(paths.logs / "marlen.log")
        )
        db = Database(paths.database)
        db.insert_log("WARNING", "Проверка единого журнала", account_id=0)
        handler.flush()
        text = (paths.logs / "marlen.log").read_text(
            encoding="utf-8", errors="replace"
        )
        assert "[Живой журнал][account=0] Проверка единого журнала" in text
    finally:
        if handler is not None:
            root.removeHandler(handler)
            handler.close()


def test_live_activity_journal_adds_new_persistent_log_once():
    script = r"""
from PySide6.QtWidgets import QApplication
from gui.activity_panel import ActivityPanel

class Adapter:
    def __init__(self):
        self.rows = []
    def get_logs(self, level=None, limit=100):
        rows = self.rows
        if level:
            rows = [row for row in rows if row["level"] == level]
        return list(reversed(rows[-limit:]))
    def get_scheduler_error(self):
        return ""
    def get_comment_campaign_state(self):
        return None
    def get_join_campaign_state(self):
        return None

app = QApplication([])
adapter = Adapter()
panel = ActivityPanel(adapter)
panel.timer.stop()
adapter.rows.append({"id": 1, "level": "WARNING", "message": "Проверка журнала"})
panel.refresh()
panel.refresh()
app.processEvents()
text = panel.feed.toPlainText()
assert text.count("[WARNING] Проверка журнала") == 1
assert panel.feed.maximumBlockCount() == 500
panel.deleteLater()
app.processEvents()
"""
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

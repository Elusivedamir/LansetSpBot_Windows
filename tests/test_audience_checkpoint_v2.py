from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from storage.audience_checkpoint import (
    discard_audience_task,
    find_resumable_audience_task,
    restart_audience_task,
    resume_audience_task,
)


class _Database:
    def __init__(self, path):
        self.path = path
        with self.get_connection() as conn:
            conn.execute(
                """CREATE TABLE tasks(
                       id INTEGER PRIMARY KEY,
                       account_id INTEGER NOT NULL,
                       type TEXT NOT NULL,
                       payload TEXT NOT NULL,
                       status TEXT NOT NULL,
                       progress INTEGER NOT NULL DEFAULT 0,
                       status_text TEXT,
                       error TEXT,
                       not_before TEXT,
                       created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                       updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                   )"""
            )

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _decode_task_payload(payload):
        return json.loads(payload or "{}")

    @staticmethod
    def _validated_payload_json(payload):
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def add_task(self, payload, *, status="paused", progress=37, error="crash"):
        with self.get_connection() as conn:
            conn.execute(
                """INSERT INTO tasks(
                       id, account_id, type, payload, status, progress, error
                   ) VALUES(7, 101, 'parse_audience', ?, ?, ?, ?)""",
                (self._validated_payload_json(payload), status, progress, error),
            )

    def task(self):
        with self.get_connection() as conn:
            return dict(conn.execute("SELECT * FROM tasks WHERE id=7").fetchone())


def _checkpoint(tmp_path):
    output = (tmp_path / "audience.txt").resolve()
    temp = output.with_name(".audience.txt.7.part")
    temp.write_text("@Alice\n", encoding="utf-8")
    return output, temp, {
        "version": 2,
        "task_id": 7,
        "account_id": 101,
        "source": {"link": "@group"},
        "source_title": "Group",
        "output_path": str(output),
        "temp_path": str(temp),
        "filters": {
            "exclude_admins": False,
            "exclude_scam_fake": False,
            "activity_days": 0,
        },
        "offset": 200,
        "file_size": temp.stat().st_size,
        "counters": {"scanned": 200, "saved": 1},
        "awaiting_user_choice": True,
        "resume_approved": False,
    }


def test_resume_preserves_checkpoint_progress_and_partial_file(tmp_path):
    database = _Database(tmp_path / "checkpoint.db")
    output, temp, checkpoint = _checkpoint(tmp_path)
    payload = {
        "account_id": 101,
        "source": {"link": "@group"},
        "output_path": str(output),
        "_audience_checkpoint": checkpoint,
    }
    database.add_task(payload)

    found = find_resumable_audience_task(database, account_id=101)
    assert found is not None
    assert found["checkpoint"]["offset"] == 200
    assert resume_audience_task(database, 7)

    row = database.task()
    updated = json.loads(row["payload"])
    assert row["status"] == "pending"
    assert row["progress"] == 37
    assert temp.exists()
    assert updated["_audience_checkpoint"]["resume_approved"] is True


def test_restart_clears_checkpoint_partial_file_and_progress(tmp_path):
    database = _Database(tmp_path / "restart.db")
    output, temp, checkpoint = _checkpoint(tmp_path)
    database.add_task(
        {
            "account_id": 101,
            "source": {"link": "@group"},
            "output_path": str(output),
            "_audience_checkpoint": checkpoint,
        }
    )

    assert restart_audience_task(database, 7)
    row = database.task()
    assert row["status"] == "pending"
    assert row["progress"] == 0
    assert "_audience_checkpoint" not in json.loads(row["payload"])
    assert not temp.exists()


def test_discard_cancels_task_and_deletes_partial_file(tmp_path):
    database = _Database(tmp_path / "discard.db")
    output, temp, checkpoint = _checkpoint(tmp_path)
    database.add_task(
        {
            "account_id": 101,
            "source": {"link": "@group"},
            "output_path": str(output),
            "_audience_checkpoint": checkpoint,
        }
    )

    assert discard_audience_task(database, 7)
    row = database.task()
    assert row["status"] == "cancelled"
    assert "_audience_checkpoint" not in json.loads(row["payload"])
    assert not temp.exists()


def test_normal_pending_deferred_task_is_not_offered_as_crash_recovery(tmp_path):
    database = _Database(tmp_path / "pending.db")
    output, _temp, checkpoint = _checkpoint(tmp_path)
    database.add_task(
        {
            "account_id": 101,
            "source": {"link": "@group"},
            "output_path": str(output),
            "_audience_checkpoint": checkpoint,
        },
        status="pending",
        error=None,
    )
    assert find_resumable_audience_task(database, account_id=101) is None

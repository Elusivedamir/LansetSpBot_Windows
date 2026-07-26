from __future__ import annotations

import random
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from services.api import ServiceAPI
from storage.database import Database
from storage.migrations.comment_delivery_context_v21 import (
    migrate_comment_delivery_context_v21,
)
from tests.conftest import open_project_database, project_row_factory


def _bind_running_slot(
    db: Database, campaign_id: int, slot_id: int, account_id: int
) -> int:
    task_id = db.insert_task(
        "auto_comment_slot",
        {"account_id": account_id, "campaign_id": campaign_id, "slot_id": slot_id},
    )
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE comment_schedule SET status='running', task_id=? WHERE id=?",
            (task_id, slot_id),
        )
    return task_id


def test_editing_ten_comment_fields_preserves_unfinished_bag_cycle(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bag-edit.db")
    account_id = 701
    original = ["A", "B", "C"]
    edited = ["A", "B", "C", "D"]
    db.set_setting("telegram.account_id", account_id)
    db.save_account_comment_profile(original, visible_count=10, account_id=account_id)
    campaign = db.create_comment_campaign(
        original,
        daily_limit=4,
        slot_count=4,
        account_id=account_id,
    )
    schedule = db.get_comment_schedule(campaign["id"])

    first_task = _bind_running_slot(db, campaign["id"], schedule[0]["id"], account_id)
    first = db.reserve_comment_variant_for_slot(
        schedule[0]["id"],
        first_task,
        account_id=account_id,
        variants=original,
        rng=random.Random(17),
    )["text"]

    db.save_account_comment_profile(edited, visible_count=10, account_id=account_id)

    following: list[str] = []
    for slot in schedule[1:4]:
        task_id = _bind_running_slot(db, campaign["id"], slot["id"], account_id)
        following.append(
            db.reserve_comment_variant_for_slot(
                slot["id"],
                task_id,
                account_id=account_id,
                variants=edited,
                rng=random.Random(29),
            )["text"]
        )

    assert first not in following
    assert set(following) == set(edited) - {first}


def test_comment_delivery_key_uses_immutable_campaign_source_post(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "delivery-context.db")
    db.set_setting("telegram.account_id", 702)

    assert db.reserve_comment_delivery(
        10,
        20,
        linked_chat_id=30,
        campaign_id=1,
        action_type="campaign_comment",
        account_id=702,
        text="first",
    )
    assert not db.reserve_comment_delivery(
        10,
        20,
        linked_chat_id=30,
        campaign_id=1,
        action_type="campaign_comment",
        account_id=702,
        text="duplicate",
    )
    assert db.has_commented(
        10,
        20,
        linked_chat_id=30,
        campaign_id=1,
        action_type="campaign_comment",
        account_id=702,
    )
    assert db.has_commented(
        10,
        20,
        linked_chat_id=30,
        campaign_id=2,
        action_type="campaign_comment",
        account_id=702,
    )
    assert not db.reserve_comment_delivery(
        10,
        20,
        linked_chat_id=30,
        campaign_id=2,
        action_type="campaign_comment",
        account_id=702,
        text="cross-campaign duplicate",
    )

    assert not db.reserve_comment_delivery(
        10,
        20,
        linked_chat_id=30,
        campaign_id=2,
        action_type="campaign_comment",
        account_id=702,
        text="new campaign",
    )
    assert not db.reserve_comment_delivery(
        10,
        20,
        linked_chat_id=40,
        campaign_id=1,
        action_type="campaign_comment",
        account_id=702,
        text="new discussion",
    )
    assert not db.reserve_comment_delivery(
        10,
        20,
        linked_chat_id=30,
        campaign_id=1,
        action_type="manual_comment",
        account_id=702,
        text="different action",
    )

    with db.get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM comment_deliveries WHERE account_id=702"
        ).fetchone()[0]
    assert count == 1


def test_v21_delivery_migration_preserves_v20_receipt_context(tmp_path: Path) -> None:
    path = tmp_path / "delivery-v20.db"
    conn = open_project_database(path)
    try:
        conn.executescript(
            """
            CREATE TABLE migrations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL UNIQUE,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE account_comment_templates(
                account_id INTEGER PRIMARY KEY,
                visible_count INTEGER NOT NULL DEFAULT 5
            );
            CREATE TABLE comment_deliveries(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL DEFAULT 0,
                channel_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                linked_chat_id INTEGER,
                comment_message_id INTEGER,
                text TEXT,
                status TEXT NOT NULL DEFAULT 'sending',
                error TEXT,
                reserved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_id, channel_id, post_id)
            );
            INSERT INTO migrations(version) VALUES(20);
            INSERT INTO account_comment_templates(account_id, visible_count)
            VALUES(702, 5);
            INSERT INTO comment_deliveries(
                account_id, channel_id, post_id, linked_chat_id,
                comment_message_id, text, status
            ) VALUES(702, 10, 20, 30, 40, 'saved', 'sent');
            PRAGMA user_version=20;
            """
        )
        conn.commit()
    finally:
        conn.close()

    migrate_comment_delivery_context_v21(
        path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5_000
    )

    conn = open_project_database(path)
    conn.row_factory = project_row_factory()
    try:
        row = conn.execute(
            "SELECT * FROM comment_deliveries WHERE account_id=702"
        ).fetchone()
        assert row is not None
        assert row["campaign_id"] == 0
        assert row["action_type"] == "comment"
        assert row["channel_id"] == 10
        assert row["post_id"] == 20
        assert row["linked_chat_id"] == 30
        assert row["comment_message_id"] == 40
        assert row["status"] == "sent"
        assert (
            conn.execute(
                "SELECT visible_count FROM account_comment_templates WHERE account_id=702"
            ).fetchone()[0]
            == 10
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 21
    finally:
        conn.close()


def test_sqlite_maintenance_runs_off_gui_thread_and_timer_can_run_again(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])

    class ProbeDatabase(Database):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.maintenance_threads: list[int] = []
            self.cleanup_threads: list[int] = []

        def run_daily_maintenance(self):
            self.maintenance_threads.append(threading.get_ident())
            time.sleep(0.15)
            return {"runs": len(self.maintenance_threads)}

        def close_thread_connection(self) -> None:
            self.cleanup_threads.append(threading.get_ident())
            super().close_thread_connection()

    db = ProbeDatabase(tmp_path / "maintenance-thread.db")
    api = ServiceAPI(db)
    assert api.wait_for_secret_migration(2_000)
    main_thread = threading.get_ident()

    started = time.monotonic()
    api._run_daily_maintenance()
    assert time.monotonic() - started < 0.05

    deadline = time.monotonic() + 3.0
    while api._maintenance_job is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert api._maintenance_job is None
    assert db.maintenance_threads == [db.cleanup_threads[-1]]
    assert db.maintenance_threads[0] != main_thread

    api._maintenance_timer.timeout.emit()
    deadline = time.monotonic() + 3.0
    while api._maintenance_job is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert len(db.maintenance_threads) == 2
    assert api._maintenance_timer.interval() == 60 * 60 * 1000

    api.prepare_shutdown()
    QThreadPool.globalInstance().waitForDone(2_000)
    app.processEvents()
    db.close()

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.comment_service import CommentService
from storage.database import Database
from storage.migrations.comment_variants_v20 import migrate_comment_variants_v20
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


def test_legacy_profile_is_normalized_to_ten_fields_without_data_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-v19.db"
    conn = open_project_database(path)
    try:
        conn.executescript(
            """
            CREATE TABLE settings(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                value TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE migrations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL UNIQUE,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE comment_templates(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                text_1 TEXT, text_2 TEXT, text_3 TEXT, text_4 TEXT, text_5 TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE comment_schedule(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                slot_index INTEGER NOT NULL,
                scheduled_at DATETIME NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                task_id INTEGER,
                channel_id INTEGER,
                post_id INTEGER,
                executed_at DATETIME,
                result TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO settings(key,value) VALUES('telegram.account_id','777');
            INSERT INTO comment_templates(name,text_1,text_2,text_3,text_4,text_5)
            VALUES('main','one','two',NULL,'four','five');
            INSERT INTO migrations(version) VALUES(19);
            PRAGMA user_version=19;
            """
        )
        conn.commit()
    finally:
        conn.close()

    migrate_comment_variants_v20(
        path,
        sqlite_timeout_seconds=30.0,
        busy_timeout_ms=30_000,
    )

    conn = open_project_database(path)
    conn.row_factory = project_row_factory()
    try:
        row = conn.execute(
            "SELECT * FROM account_comment_templates WHERE account_id=777"
        ).fetchone()
        columns = {
            str(item[1]) for item in conn.execute("PRAGMA table_info(comment_schedule)")
        }
        assert row is not None
        assert row["visible_count"] == 10
        assert [row[f"text_{index}"] for index in range(1, 11)] == [
            "one",
            "two",
            "",
            "four",
            "five",
            None,
            None,
            None,
            None,
            None,
        ]
        assert {"selected_text", "selected_variant_index"}.issubset(columns)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 20
    finally:
        conn.close()


def test_profiles_are_account_scoped_and_import_previous_copies_only_texts(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "profiles.db")
    db.set_setting("telegram.account_id", 101)
    source_comments = [f"a-{index}" for index in range(1, 11)]
    db.save_account_comment_profile(
        source_comments,
        visible_count=10,
        account_id=101,
    )
    db.get_account_comment_profile(101, touch=True)

    db.set_setting("telegram.account_id", 202)
    blank = db.get_account_comment_profile(202, touch=True)
    assert blank["comments"] == [""] * 10

    imported = db.import_previous_account_comment_profile(account_id=202)
    assert imported is not None
    assert imported["source_account_id"] == 101
    assert imported["comments"] == source_comments

    profile_202 = db.get_account_comment_profile(202)
    profile_101 = db.get_account_comment_profile(101)
    assert profile_202["comments"] == profile_101["comments"] == source_comments
    assert profile_202["bag_order_json"] == "[]"
    assert profile_202["bag_position"] == 0

    db.save_account_comment_profile(["b-1", "b-2"], visible_count=2, account_id=202)
    assert db.get_account_comment_profile(101)["comments"][0] == "a-1"
    assert db.get_account_comment_profile(202)["comments"][:2] == ["b-1", "b-2"]


def test_shuffled_bag_uses_every_variant_and_persists_slot_selection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bag.db"
    db = Database(path)
    account_id = 303
    variants = ["alpha", "beta", "gamma"]
    db.set_setting("telegram.account_id", account_id)
    db.save_account_comment_profile(variants, visible_count=3, account_id=account_id)
    campaign = db.create_comment_campaign(
        variants,
        daily_limit=7,
        slot_count=7,
        account_id=account_id,
    )
    schedule = db.get_comment_schedule(campaign["id"])
    rng = random.Random(51)
    selected: list[str] = []

    first = schedule[0]
    first_task = _bind_running_slot(db, campaign["id"], first["id"], account_id)
    reservation = db.reserve_comment_variant_for_slot(
        first["id"],
        first_task,
        account_id=account_id,
        variants=variants,
        rng=rng,
    )
    selected.append(reservation["text"])
    db.close()

    reopened = Database(path)
    repeated = reopened.reserve_comment_variant_for_slot(
        first["id"],
        first_task,
        account_id=account_id,
        variants=variants,
        rng=random.Random(999),
    )
    assert repeated["reused"] is True
    assert repeated["text"] == selected[0]

    for slot in schedule[1:]:
        task_id = _bind_running_slot(reopened, campaign["id"], slot["id"], account_id)
        item = reopened.reserve_comment_variant_for_slot(
            slot["id"],
            task_id,
            account_id=account_id,
            variants=variants,
            rng=rng,
        )
        selected.append(item["text"])

    assert set(selected[:3]) == set(variants)
    assert set(selected[3:6]) == set(variants)
    assert all(left != right for left, right in zip(selected, selected[1:]))
    persisted = reopened.get_comment_schedule(campaign["id"])
    assert [row["selected_text"] for row in persisted] == selected


@pytest.mark.asyncio
async def test_delivery_ledger_contains_selected_text_before_telegram_rpc(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "delivery.db")
    db.set_setting("telegram.account_id", 404)
    db.insert_channel(
        {
            "account_id": 404,
            "channel_id": 10,
            "title": "Target",
            "target_kind": "channel",
            "comment_mode": "linked_discussion",
            "linked_chat_id": 30,
            "link_status": "Связано",
        }
    )

    class _Telegram:
        async def send_comment(
            self,
            channel_id,
            post_id,
            text,
            *,
            reply_to=None,
            linked_chat_id=None,
        ):
            with db.get_connection() as conn:
                row = conn.execute(
                    """SELECT status, text FROM comment_deliveries
                       WHERE account_id=? AND channel_id=? AND post_id=?""",
                    (404, channel_id, post_id),
                ).fetchone()
            assert row is not None
            assert row["status"] == "sending"
            assert row["text"] == "chosen-text"
            return SimpleNamespace(id=9001, sender_id=404, date="2026-07-16")

    service = CommentService(_Telegram(), db=db)
    result = await service.ensure_and_send_comment(
        channel_id=10,
        post_message_id=20,
        linked_chat_id=30,
        reply_to=40,
        membership_ready=True,
        text="chosen-text",
        account_id=404,
    )
    assert result.id == 9001
    with db.get_connection() as conn:
        receipt = conn.execute(
            """SELECT status, text FROM comment_deliveries
               WHERE account_id=404 AND channel_id=10 AND post_id=20"""
        ).fetchone()
    assert receipt["status"] == "sent"
    assert receipt["text"] == "chosen-text"


def test_commenting_view_always_shows_ten_fields_and_imports_all_ten() -> None:
    from PySide6.QtWidgets import QApplication

    from gui.views.commenting_view import CommentingView

    app = QApplication.instance() or QApplication([])

    class _Adapter:
        def __init__(self) -> None:
            self.profile = {
                "account_id": 2,
                "visible_count": 5,
                "comments": ["one", "two", "", "four", "five"] + [""] * 5,
            }

        def get_comment_daily_limit(self):
            return 40

        def get_comment_profile(self, account_id=None):
            del account_id
            return dict(self.profile)

        def get_comment_campaign_state(self):
            return None

        def get_channels(self):
            return []

        def get_commenting_channels(self):
            return []

        def get_comment_history(self, **_kwargs):
            return []

        def import_previous_comment_profile(self, account_id=None):
            del account_id
            return {
                "source_account_id": 1,
                "account_id": 2,
                "visible_count": 10,
                "comments": [f"import-{index}" for index in range(1, 11)],
            }

    view = CommentingView(_Adapter())
    assert len(view.editors) == 10
    assert view.add_variant_button.isHidden() is True
    assert view.delete_variant_button.isHidden() is True
    view.add_comment_variant()
    view.delete_selected_variant()
    assert len(view.editors) == 10

    view.import_previous_account_comments()
    app.processEvents()
    assert len(view.editors) == 10
    assert [editor.toPlainText() for editor in view.editors] == [
        f"import-{index}" for index in range(1, 11)
    ]
    assert "аккаунта 1" in view.save_status.text()
    view.deleteLater()
    app.processEvents()

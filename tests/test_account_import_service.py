from __future__ import annotations

import pytest

from services.account_import_service import AccountImportService
from storage.database import Database
from storage.db_common import DatabaseError


def _register(database: Database, account_id: int) -> None:
    database.register_telegram_account(
        telegram_account_id=account_id,
        session_name=f"account_{account_id}",
        display_name=f"Account {account_id}",
        authorized=True,
    )


def test_channel_import_resets_session_derived_fields(tmp_path) -> None:
    database = Database(tmp_path / "import.db")
    _register(database, 101)
    _register(database, 202)
    database.insert_channel(
        {
            "account_id": 101,
            "channel_id": -100123,
            "username": "source_channel",
            "title": "Source channel",
            "target_kind": "channel",
            "comment_mode": "channel_post",
            "linked_chat_id": -100999,
            "linked_chat_title": "Old discussion",
            "link_status": "Связано · участие подтверждено",
            "access_hash": 987654321,
            "peer_type": "channel",
        }
    )

    result = AccountImportService(database).import_channels(
        source_account_id=101,
        target_account_id=202,
    )

    assert result == {"imported": 1, "existing": 0, "skipped": 0}
    rows = database.get_channels(account_id=202)
    assert len(rows) == 1
    row = rows[0]
    assert row["channel_id"] == -100123
    assert row["username"] == "source_channel"
    assert row["comment_mode"] == "pending"
    assert row["linked_chat_id"] is None
    assert row["linked_chat_title"] is None
    assert row["access_hash"] is None
    assert row["peer_type"] is None
    assert row["link_checked_at"] is None
    assert "требуется повторная проверка" in row["link_status"].casefold()


def test_account_import_rejects_self_import(tmp_path) -> None:
    database = Database(tmp_path / "self.db")
    _register(database, 101)
    importer = AccountImportService(database)
    with pytest.raises(DatabaseError, match="него же"):
        importer.import_channels(source_account_id=101, target_account_id=101)
    with pytest.raises(DatabaseError, match="него же"):
        importer.import_comments(source_account_id=101, target_account_id=101)


def test_comment_import_preserves_slot_order_and_visible_count(tmp_path) -> None:
    database = Database(tmp_path / "comments.db")
    _register(database, 101)
    _register(database, 202)
    database.save_account_comment_profile(
        ["Первый", "Второй", "Третий", "", "", "", "", "", "", ""],
        visible_count=3,
        account_id=101,
    )
    with database.get_connection() as conn:
        # The current UI repository normalizes visible_count to ten when saving;
        # emulate a persisted source profile with an explicit configured count.
        conn.execute(
            """UPDATE account_comment_templates
               SET visible_count=3, bag_order_json='[2,1,0]', bag_position=2,
                   last_variant_index=1
               WHERE account_id=101"""
        )

    result = AccountImportService(database).import_comments(
        source_account_id=101,
        target_account_id=202,
        mode="replace",
    )

    assert result["visible_count"] == 3
    with database.get_connection() as conn:
        row = conn.execute(
            """SELECT visible_count, text_1, text_2, text_3,
                      bag_order_json, bag_position, last_variant_index
               FROM account_comment_templates WHERE account_id=202"""
        ).fetchone()
    assert row["visible_count"] == 3
    assert [row["text_1"], row["text_2"], row["text_3"]] == [
        "Первый",
        "Второй",
        "Третий",
    ]
    assert row["bag_order_json"] == "[]"
    assert row["bag_position"] == 0
    assert row["last_variant_index"] is None

from __future__ import annotations

from core.config import (
    DEFAULT_POST_JOIN_DELAY_MAX_SECONDS,
    DEFAULT_POST_JOIN_DELAY_MIN_SECONDS,
)
from storage.database import Database


def test_comment_join_delay_defaults_are_small_and_randomizable():
    assert DEFAULT_POST_JOIN_DELAY_MIN_SECONDS == 15
    assert DEFAULT_POST_JOIN_DELAY_MAX_SECONDS == 30


def test_join_event_database_falls_back_to_current_telegram_account(tmp_path):
    db = Database(tmp_path / "join-account.db")
    db.set_setting("telegram.account_id", 777001)

    db.record_join_event(-1001234567890, "joined")

    with db.get_connection() as conn:
        row = conn.execute(
            """SELECT linked_chat_id, result, account_id
               FROM join_events ORDER BY id DESC LIMIT 1"""
        ).fetchone()

    assert row is not None
    assert int(row["linked_chat_id"]) == -1001234567890
    assert row["result"] == "joined"
    assert int(row["account_id"]) == 777001


def test_join_guard_does_not_charge_unowned_rows_to_an_account(tmp_path):
    db = Database(tmp_path / "legacy-null-join.db")
    db.set_setting("telegram.account_id", 99001)
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO join_events(linked_chat_id, joined_at, result, account_id)
               VALUES(-1001, CURRENT_TIMESTAMP, 'joined', NULL)"""
        )

    guard = db.get_join_guard(
        max_joins=1,
        min_interval_seconds=1,
        window_seconds=3600,
        account_id=99001,
    )

    assert guard["joined_count"] == 0
    assert guard["allowed"] is True

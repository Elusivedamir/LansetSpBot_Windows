from __future__ import annotations

from pathlib import Path

from storage.database import Database


def _register(db: Database, account_id: int, name: str) -> None:
    db.register_telegram_account(
        telegram_account_id=account_id,
        session_name=f"account_{account_id}",
        display_name=name,
        authorized=True,
    )


def test_v38_schema_and_floodwait_escalation_are_account_scoped(tmp_path: Path) -> None:
    db = Database(tmp_path / "safety.db")
    try:
        _register(db, 101, "A")
        _register(db, 202, "B")
        assert db.get_version() == 38
        first = db.record_account_flood_wait_safety(
            account_id=101, code="flood_wait_deferred", wait_seconds=60, source_task_id=11)
        assert first["mode"] == "conservative"
        second = db.record_account_flood_wait_safety(
            account_id=101, code="flood_wait_deferred", wait_seconds=60, source_task_id=12)
        assert second["mode"] == "protective"
        assert second["adaptive_level"] == "soft_protective"
        untouched = db.get_account_safety_state(202)
        assert untouched["mode"] == "normal"
    finally:
        db.close_thread_connection()


def test_conservative_reservations_reduce_load_without_defer_budget(tmp_path: Path) -> None:
    db = Database(tmp_path / "pacing.db")
    try:
        _register(db, 101, "A")
        db.record_account_flood_wait_safety(
            account_id=101, code="flood_wait_deferred", wait_seconds=1, source_task_id=1)
        task_one = db.insert_task("warmup_step", {"account_id": 101})
        first = db.reserve_account_safety_task(
            account_id=101, task_id=task_one, task_type="warmup_step")
        assert first["action"] == "allow"
        assert first["effective_gap_seconds"] >= 600
        repeated = db.reserve_account_safety_task(
            account_id=101, task_id=task_one, task_type="warmup_step")
        assert repeated["action"] == "allow"
        assert repeated["idempotent"] is True
        task_two = db.insert_task("warmup_step", {"account_id": 101})
        postponed = db.reserve_account_safety_task(
            account_id=101, task_id=task_two, task_type="warmup_step")
        assert postponed["action"] == "postpone"
        first_rpc = db.reserve_account_safety_request(
            account_id=101, request_name="SendMessageRequest", spacing_seconds=75)
        second_rpc = db.reserve_account_safety_request(
            account_id=101, request_name="SendMessageRequest", spacing_seconds=75)
        assert first_rpc["action"] == "allow"
        assert second_rpc["action"] == "wait"
    finally:
        db.close_thread_connection()


def test_protective_recovery_is_stepwise_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "recovery.db")
    try:
        _register(db, 101, "A")
        for task_id in (1, 2):
            db.record_account_flood_wait_safety(
                account_id=101, code="flood_wait_deferred", wait_seconds=1, source_task_id=task_id)
        with db.get_connection() as conn:
            conn.execute("UPDATE account_safety_state SET recovery_not_before='2000-01-01 00:00:00' WHERE account_id=101")
        recovered = db.get_account_safety_state(101)
        assert recovered["adaptive_level"] == "conservative"
        assert recovered["mode"] == "conservative"
        assert recovered["recovery_remaining_seconds"] > 0
    finally:
        db.close_thread_connection()
def test_missing_account_safety_reservation_does_not_create_orphan_state(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "missing-account.db")
    try:
        decision = db.reserve_account_safety_task(
            account_id=999,
            task_id=1,
            task_type="link_channels",
        )
        assert decision["action"] == "block"
        assert decision["reason_code"] == "account_missing"
        with db.get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM account_safety_state WHERE account_id=999"
            ).fetchone()[0]
        assert count == 0
    finally:
        db.close_thread_connection()

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from storage.database import Database
from storage.db_settings import (
    PERSISTENT_LOG_BUDGET_BYTES,
    _PERSISTENT_LOG_PRUNE_TARGET_BYTES,
)


def _actual_retained_bytes(db: Database) -> int:
    with db.get_connection() as conn:
        return int(
            conn.execute(
                """SELECT COALESCE(SUM(
                       length(CAST(level AS BLOB))
                     + length(CAST(message AS BLOB))
                     + length(CAST(created_at AS BLOB))
                     + 48), 0)
                   FROM logs"""
            ).fetchone()[0]
            or 0
        )


def _cached_retained_bytes(db: Database) -> int:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='internal.logs.retained_bytes'"
        ).fetchone()
        return int(row[0] if row is not None else 0)


def test_activity_log_budget_is_five_mib_with_four_mib_prune_target():
    assert PERSISTENT_LOG_BUDGET_BYTES == 5 * 1024 * 1024
    assert _PERSISTENT_LOG_PRUNE_TARGET_BYTES == 4 * 1024 * 1024


def test_multithreaded_log_counter_remains_exact_and_bounded(tmp_path):
    db = Database(tmp_path / "concurrent-logs.db")
    workers = 8
    entries_per_worker = 160
    payload = "Ж" * 3_000

    def write_batch(worker_index: int) -> None:
        for row_index in range(entries_per_worker):
            db.insert_log(
                "INFO",
                f"worker={worker_index}; row={row_index}; {payload}",
            )
        db.close_thread_connection()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(write_batch, range(workers)))

    actual = _actual_retained_bytes(db)
    cached = _cached_retained_bytes(db)

    assert actual == cached
    assert actual <= PERSISTENT_LOG_BUDGET_BYTES
    # A prune cycle removes a meaningful batch instead of hovering at 5 MiB.
    assert actual >= _PERSISTENT_LOG_PRUNE_TARGET_BYTES - 128 * 1024

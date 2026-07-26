from __future__ import annotations

from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path


def migrate_comment_cadence_v17(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Persist an immutable per-slot cadence for comment campaigns.

    Before v17 the cadence was recalculated from ``ends_at - started_at``.
    Because ``ends_at`` is deliberately extended after pauses, every subsequent
    redistribution could multiply the interval. Existing campaigns are migrated
    to the public daily-limit contract (24 hours / daily_limit); newly created
    campaigns store their configured initial window exactly.
    """

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")

        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='comment_campaigns'"
        ).fetchone()
        if table_exists is not None:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(comment_campaigns)")
            }
            column_added = "cadence_seconds" not in columns
            if column_added:
                conn.execute(
                    "ALTER TABLE comment_campaigns "
                    "ADD COLUMN cadence_seconds REAL NOT NULL DEFAULT 2160.0"
                )

            # SQLite fills every pre-existing row with the declared DEFAULT
            # when ADD COLUMN is used. Therefore a newly added column must be
            # backfilled unconditionally; filtering only NULL/<=0 would leave
            # all old non-40 campaigns at the 40/day default (2160 seconds).
            where_clause = (
                ""
                if column_added
                else "WHERE cadence_seconds IS NULL OR cadence_seconds <= 0"
            )
            conn.execute(
                f"""UPDATE comment_campaigns
                    SET cadence_seconds = 86400.0 /
                        CASE
                            WHEN daily_limit IS NULL OR daily_limit < 1 THEN 40
                            WHEN daily_limit > 1000 THEN 1000
                            ELSE daily_limit
                        END
                    {where_clause}"""
            )
        conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(17)")
        conn.execute("PRAGMA user_version = 17")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

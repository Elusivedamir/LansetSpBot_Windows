from __future__ import annotations

from typing import TYPE_CHECKING

import json
from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path

from core.config import DEFAULT_COMMENT_VARIANTS, MAX_COMMENT_VARIANTS

if TYPE_CHECKING:  # pragma: no cover - typing only
    # ``sqlite3`` is bound to the SQLCipher DBAPI proxy object, not to a
    # module, so its DBAPI classes are imported from the standard library
    # for annotations. The two drivers are DBAPI-compatible.
    from sqlite3 import Connection as SQLiteConnection



def _current_account_id(conn: SQLiteConnection) -> int:
    row = conn.execute(
        "SELECT value FROM settings WHERE key='telegram.account_id'"
    ).fetchone()
    try:
        return max(0, int(row[0] or 0)) if row is not None else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _slots_from_json(raw: object) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        parsed = []
    values = [str(item or "").strip() for item in list(parsed)[:MAX_COMMENT_VARIANTS]]
    values += [""] * (MAX_COMMENT_VARIANTS - len(values))
    return values


def migrate_comment_variants_v20(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Add the ten-field per-account template model and durable shuffled bag."""

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS account_comment_templates(
                   account_id INTEGER PRIMARY KEY,
                   visible_count INTEGER NOT NULL DEFAULT 10
                       CHECK(visible_count BETWEEN 1 AND 10),
                   text_1 TEXT, text_2 TEXT, text_3 TEXT, text_4 TEXT, text_5 TEXT,
                   text_6 TEXT, text_7 TEXT, text_8 TEXT, text_9 TEXT, text_10 TEXT,
                   bag_fingerprint TEXT NOT NULL DEFAULT '',
                   bag_order_json TEXT NOT NULL DEFAULT '[]',
                   bag_position INTEGER NOT NULL DEFAULT 0 CHECK(bag_position >= 0),
                   last_variant_index INTEGER,
                   last_used_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comment_templates_last_used "
            "ON account_comment_templates(last_used_at DESC, updated_at DESC)"
        )

        existing_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "comment_schedule" in existing_tables:
            schedule_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(comment_schedule)")
            }
            if "selected_text" not in schedule_columns:
                conn.execute(
                    "ALTER TABLE comment_schedule ADD COLUMN selected_text TEXT"
                )
            if "selected_variant_index" not in schedule_columns:
                conn.execute(
                    "ALTER TABLE comment_schedule "
                    "ADD COLUMN selected_variant_index INTEGER"
                )

        current_account_id = _current_account_id(conn)
        legacy = None
        if "comment_templates" in existing_tables:
            legacy = conn.execute(
                """SELECT text_1, text_2, text_3, text_4, text_5
                   FROM comment_templates WHERE name='main'
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        if legacy is not None:
            legacy_values = [
                str(legacy[f"text_{index}"] or "") for index in range(1, 6)
            ]
            conn.execute(
                """INSERT OR IGNORE INTO account_comment_templates(
                       account_id, visible_count,
                       text_1, text_2, text_3, text_4, text_5,
                       last_used_at, updated_at)
                   VALUES(?, 10, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (current_account_id, *legacy_values),
            )

        # Historical campaigns already carry their immutable text snapshot. Use
        # the newest one to seed accounts that were not the currently selected
        # session during migration.
        account_rows = []
        if "comment_campaigns" in existing_tables:
            campaign_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(comment_campaigns)")
            }
            if {"account_id", "comments_json"}.issubset(campaign_columns):
                account_rows = conn.execute(
                    """SELECT c.account_id, c.comments_json
                       FROM comment_campaigns c
                       JOIN (
                           SELECT account_id, MAX(id) AS max_id
                           FROM comment_campaigns GROUP BY account_id
                       ) latest ON latest.max_id=c.id
                       WHERE c.account_id IS NOT NULL"""
                ).fetchall()
        for row in account_rows:
            account_id = max(0, int(row["account_id"] or 0))
            exists = conn.execute(
                "SELECT 1 FROM account_comment_templates WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if exists is not None:
                continue
            slots = _slots_from_json(row["comments_json"])
            active_count = sum(1 for value in slots if value)
            visible_count = max(
                DEFAULT_COMMENT_VARIANTS,
                min(MAX_COMMENT_VARIANTS, active_count or DEFAULT_COMMENT_VARIANTS),
            )
            conn.execute(
                """INSERT INTO account_comment_templates(
                       account_id, visible_count,
                       text_1, text_2, text_3, text_4, text_5,
                       text_6, text_7, text_8, text_9, text_10,
                       last_used_at, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (account_id, visible_count, *[value or None for value in slots]),
            )

        conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(20)")
        conn.execute("PRAGMA user_version = 20")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

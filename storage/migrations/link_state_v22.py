from __future__ import annotations

import json
from storage.sqlcipher_driver import dbapi as sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


def _task_account_id(payload: Any) -> int:
    try:
        decoded = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0
    if not isinstance(decoded, dict):
        return 0
    try:
        return max(0, int(decoded.get("account_id") or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def migrate_link_state_v22(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Persist one-time link checks and account-wide Telegram RPC cooldowns."""

    conn = sqlite3.connect(
        str(path),
        timeout=sqlite_timeout_seconds,
        isolation_level=None,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")

        channel_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(channels)")
        }
        if "link_checked_at" not in channel_columns:
            conn.execute("ALTER TABLE channels ADD COLUMN link_checked_at DATETIME")

        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_channels_link_pending
               ON channels(account_id, link_checked_at, target_kind, id)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS account_rpc_cooldowns(
                   account_id INTEGER PRIMARY KEY,
                   next_allowed_at DATETIME NOT NULL,
                   code TEXT NOT NULL DEFAULT 'flood_wait_deferred',
                   source_task_id INTEGER,
                   updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_account_rpc_cooldowns_due
               ON account_rpc_cooldowns(next_allowed_at)"""
        )

        # Existing rows with a final link/classification result were already checked
        # before this migration. Preserve that work so upgrading does not trigger a
        # new full pass through Telegram.
        conn.execute(
            """UPDATE channels
               SET link_checked_at=COALESCE(last_sync_at, created_at, CURRENT_TIMESTAMP)
               WHERE link_checked_at IS NULL
                 AND link_status IS NOT NULL
                 AND TRIM(link_status)<>''
                 AND LOWER(TRIM(link_status)) NOT IN ('не проверено', 'pending')"""
        )

        # Older builds could create several active link tasks for one account.
        # Keep the most advanced task (oldest on ties) and cancel the duplicates.
        table_names = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "tasks" in table_names:
            task_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")
            }
            required_task_columns = {
                "id",
                "type",
                "payload",
                "status",
                "progress",
            }
            if required_task_columns.issubset(task_columns):
                rows = conn.execute(
                    """SELECT id, payload, status, progress
                       FROM tasks
                       WHERE type='link_channels'
                         AND status IN ('pending','running','processing','paused')
                       ORDER BY id ASC"""
                ).fetchall()
                grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
                for row in rows:
                    account_id = _task_account_id(row["payload"])
                    if account_id > 0:
                        grouped[account_id].append(row)

                can_cancel_duplicates = {
                    "status_text",
                    "error",
                    "not_before",
                    "updated_at",
                }.issubset(task_columns)
                if can_cancel_duplicates:
                    for account_id, account_rows in grouped.items():
                        if len(account_rows) <= 1:
                            continue
                        keep = max(
                            account_rows,
                            key=lambda row: (
                                max(0, int(row["progress"] or 0)),
                                -int(row["id"]),
                            ),
                        )
                        keep_id = int(keep["id"])
                        for row in account_rows:
                            task_id = int(row["id"])
                            if task_id == keep_id:
                                continue
                            conn.execute(
                                """UPDATE tasks
                                   SET status='cancelled', status_text=NULL,
                                       error=?, not_before=NULL,
                                       updated_at=CURRENT_TIMESTAMP
                                   WHERE id=?
                                     AND status IN (
                                         'pending','running','processing','paused'
                                     )""",
                                (
                                    "duplicate_link_task_merged: сохранена "
                                    "единственная задача "
                                    f"#{keep_id} для аккаунта {account_id}",
                                    task_id,
                                ),
                            )

        conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(22)")
        conn.execute("PRAGMA user_version = 22")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

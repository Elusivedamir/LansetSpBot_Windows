from __future__ import annotations

from pathlib import Path

from storage.sqlcipher_driver import dbapi as sqlite3


def migrate_warmup_workflows_v36(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Create durable account-pair warmup workflow tables."""

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(
            """
            BEGIN IMMEDIATE;

            CREATE TABLE IF NOT EXISTS warmup_pairs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_a_id INTEGER NOT NULL,
                account_b_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'running'
                    CHECK(status IN ('running','paused','completed','archived')),
                week_number INTEGER NOT NULL DEFAULT 1 CHECK(week_number >= 1),
                profile_seed TEXT NOT NULL,
                day_order TEXT NOT NULL,
                dialogue_windows INTEGER NOT NULL CHECK(dialogue_windows BETWEEN 1 AND 8),
                reply_min_seconds INTEGER NOT NULL CHECK(reply_min_seconds >= 1),
                reply_max_seconds INTEGER NOT NULL CHECK(reply_max_seconds >= reply_min_seconds),
                typing_min_seconds INTEGER NOT NULL CHECK(typing_min_seconds BETWEEN 1 AND 30),
                typing_max_seconds INTEGER NOT NULL CHECK(typing_max_seconds >= typing_min_seconds),
                group_visits_per_day INTEGER NOT NULL CHECK(group_visits_per_day BETWEEN 0 AND 8),
                posts_min INTEGER NOT NULL CHECK(posts_min BETWEEN 1 AND 20),
                posts_max INTEGER NOT NULL CHECK(posts_max >= posts_min),
                reaction_probability_percent INTEGER NOT NULL
                    CHECK(reaction_probability_percent BETWEEN 0 AND 100),
                private_reaction_probability_percent INTEGER NOT NULL
                    CHECK(private_reaction_probability_percent BETWEEN 0 AND 100),
                active_start_hour INTEGER NOT NULL CHECK(active_start_hour BETWEEN 0 AND 23),
                active_end_hour INTEGER NOT NULL CHECK(active_end_hour BETWEEN 1 AND 24),
                owner_token_a TEXT NOT NULL,
                owner_token_b TEXT NOT NULL,
                current_step INTEGER NOT NULL DEFAULT 0 CHECK(current_step >= 0),
                total_steps INTEGER NOT NULL DEFAULT 0 CHECK(total_steps >= 0),
                last_message_id INTEGER,
                last_sender_account_id INTEGER,
                last_error TEXT,
                started_at DATETIME NOT NULL,
                ends_at DATETIME NOT NULL,
                completed_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(account_a_id <> account_b_id),
                FOREIGN KEY(account_a_id)
                    REFERENCES telegram_accounts(telegram_account_id) ON DELETE CASCADE,
                FOREIGN KEY(account_b_id)
                    REFERENCES telegram_accounts(telegram_account_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_warmup_pairs_status
                ON warmup_pairs(status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_warmup_pairs_accounts
                ON warmup_pairs(account_a_id, account_b_id, status);

            CREATE TABLE IF NOT EXISTS warmup_accounts(
                account_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'available'
                    CHECK(status IN ('available','active','completed','transferred')),
                active_pair_id INTEGER,
                weeks_completed INTEGER NOT NULL DEFAULT 0 CHECK(weeks_completed >= 0),
                transferred_at DATETIME,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(account_id)
                    REFERENCES telegram_accounts(telegram_account_id) ON DELETE CASCADE,
                FOREIGN KEY(active_pair_id)
                    REFERENCES warmup_pairs(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_warmup_accounts_state
                ON warmup_accounts(status, active_pair_id);

            CREATE TABLE IF NOT EXISTS warmup_groups(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_ref TEXT NOT NULL UNIQUE,
                title TEXT,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS warmup_group_accounts(
                group_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                membership_state TEXT NOT NULL DEFAULT 'unknown'
                    CHECK(membership_state IN (
                        'unknown','joined','requested','unavailable','blocked'
                    )),
                last_read_message_id INTEGER,
                last_reacted_message_id INTEGER,
                last_visited_at DATETIME,
                last_error TEXT,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(group_id, account_id),
                FOREIGN KEY(group_id) REFERENCES warmup_groups(id) ON DELETE CASCADE,
                FOREIGN KEY(account_id)
                    REFERENCES telegram_accounts(telegram_account_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_warmup_group_accounts_visit
                ON warmup_group_accounts(account_id, last_visited_at);

            CREATE TABLE IF NOT EXISTS warmup_steps(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id INTEGER NOT NULL,
                week_number INTEGER NOT NULL CHECK(week_number >= 1),
                sequence_no INTEGER NOT NULL CHECK(sequence_no >= 1),
                day_number INTEGER NOT NULL CHECK(day_number BETWEEN 1 AND 7),
                scenario_key TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN (
                    'ensure_contact','message','private_reaction','group_visit'
                )),
                actor_account_id INTEGER NOT NULL,
                target_account_id INTEGER,
                message_text TEXT,
                typing_seconds INTEGER NOT NULL DEFAULT 0 CHECK(typing_seconds BETWEEN 0 AND 30),
                reply_to_previous INTEGER NOT NULL DEFAULT 0 CHECK(reply_to_previous IN (0,1)),
                posts_to_read INTEGER NOT NULL DEFAULT 0 CHECK(posts_to_read BETWEEN 0 AND 20),
                should_react INTEGER NOT NULL DEFAULT 0 CHECK(should_react IN (0,1)),
                scheduled_at DATETIME NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN (
                        'pending','running','done','skipped','failed','uncertain','cancelled'
                    )),
                queue_task_id INTEGER,
                telegram_message_id INTEGER,
                result_text TEXT,
                started_at DATETIME,
                completed_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(pair_id, week_number, sequence_no),
                FOREIGN KEY(pair_id) REFERENCES warmup_pairs(id) ON DELETE CASCADE,
                FOREIGN KEY(actor_account_id)
                    REFERENCES telegram_accounts(telegram_account_id) ON DELETE CASCADE,
                FOREIGN KEY(target_account_id)
                    REFERENCES telegram_accounts(telegram_account_id) ON DELETE CASCADE,
                FOREIGN KEY(queue_task_id) REFERENCES tasks(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_warmup_steps_due
                ON warmup_steps(pair_id, week_number, status, scheduled_at, sequence_no);
            CREATE INDEX IF NOT EXISTS idx_warmup_steps_actor
                ON warmup_steps(actor_account_id, status, scheduled_at);
            """
        )
        conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(36)")
        conn.execute("PRAGMA user_version = 36")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

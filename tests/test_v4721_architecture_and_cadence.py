from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.campaign_schedule import from_db_time
from storage.database import Database
from storage.migrations.comment_cadence_v17 import migrate_comment_cadence_v17
from tests.conftest import open_project_database

UTC = timezone.utc


def test_repeated_redistribution_keeps_original_slider_cadence(tmp_path: Path) -> None:
    db = Database(tmp_path / "cadence.db")
    start = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    campaign = db.create_comment_campaign(
        ["hello"],
        daily_limit=40,
        slot_count=40,
        duration_hours=24,
        continuous=False,
        start_at=start,
        rng=random.Random(1),
    )
    campaign_id = int(campaign["id"])

    first_resume = start + timedelta(hours=24, minutes=1)
    assert (
        db.redistribute_pending_comment_slots(
            campaign_id, now=first_resume, force=True, rng=random.Random(2)
        )
        == 40
    )
    first = db.get_comment_campaign(campaign_id)
    first_end = from_db_time(first["ends_at"])
    assert first_end is not None
    assert first_end - first_resume == timedelta(hours=24)
    assert float(first["cadence_seconds"]) == pytest.approx(2160.0)

    second_resume = first_end + timedelta(minutes=1)
    assert (
        db.redistribute_pending_comment_slots(
            campaign_id, now=second_resume, force=True, rng=random.Random(3)
        )
        == 40
    )
    second = db.get_comment_campaign(campaign_id)
    second_end = from_db_time(second["ends_at"])
    assert second_end is not None

    # The old implementation recalculated from the already extended
    # ends_at-started_at window and doubled this horizon to roughly 48 hours.
    assert second_end - second_resume == timedelta(hours=24)
    assert float(second["cadence_seconds"]) == pytest.approx(2160.0)


def test_v16_campaigns_receive_persistent_daily_cadence(tmp_path: Path) -> None:
    path = tmp_path / "v16-cadence.db"
    conn = open_project_database(path)
    try:
        conn.executescript(
            """
            CREATE TABLE migrations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL UNIQUE,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE comment_campaigns(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                started_at DATETIME NOT NULL,
                ends_at DATETIME NOT NULL,
                daily_limit INTEGER NOT NULL,
                continuous INTEGER NOT NULL,
                comments_json TEXT NOT NULL
            );
            INSERT INTO comment_campaigns(
                status, started_at, ends_at, daily_limit, continuous, comments_json
            ) VALUES
                ('running', '2026-07-14 00:00:00', '2026-07-16 00:00:00',
                 1, 1, '["one"]'),
                ('running', '2026-07-14 00:00:00', '2026-07-16 00:00:00',
                 40, 1, '["forty"]'),
                ('running', '2026-07-14 00:00:00', '2026-07-16 00:00:00',
                 223, 1, '["dense"]'),
                ('running', '2026-07-14 00:00:00', '2026-07-16 00:00:00',
                 1000, 1, '["thousand"]');
            INSERT INTO migrations(version) VALUES(16);
            PRAGMA user_version=16;
            """
        )
        conn.commit()
    finally:
        conn.close()

    migrate_comment_cadence_v17(path, sqlite_timeout_seconds=5.0, busy_timeout_ms=5_000)

    conn = open_project_database(path)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(comment_campaigns)")
        }
        cadences = conn.execute(
            "SELECT daily_limit, cadence_seconds FROM comment_campaigns ORDER BY id"
        ).fetchall()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    assert "cadence_seconds" in columns
    assert cadences == pytest.approx(
        [
            (1, 86_400.0),
            (40, 2_160.0),
            (223, 86_400.0 / 223),
            (1000, 86.4),
        ]
    )
    assert version == 17


def test_split_facades_keep_the_established_public_imports() -> None:
    from services.api import ServiceAPI
    from services.telegram_service import LatestPostResult, TelegramService
    from storage.db_comment_campaigns import CommentCampaignRepositoryMixin
    from storage.db_join_campaigns import JoinCampaignRepositoryMixin
    from storage.db_schema import DatabaseSchemaMixin
    from workers.handlers.comment_slot import (
        CommentSlotPhase,
        create_comment_slot_handler,
    )

    assert hasattr(CommentCampaignRepositoryMixin, "reconcile_comment_schedule")
    assert hasattr(JoinCampaignRepositoryMixin, "finalize_join_slot_outcome")
    assert hasattr(DatabaseSchemaMixin, "run_migrations")
    assert hasattr(ServiceAPI, "start_comment_campaign")
    assert hasattr(TelegramService, "get_latest_post_for_commenting")
    assert LatestPostResult(status="no_post").status == "no_post"
    assert callable(create_comment_slot_handler)
    assert CommentSlotPhase.SEND_CONFIRMED > CommentSlotPhase.SEND_STARTED

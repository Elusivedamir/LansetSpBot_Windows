from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from core.openai_settings import SOURCE_PREWRITTEN
from services.api_parts.comments import CommentCampaignAPIMixin
from storage.comment_campaigns.history import CommentHistoryMixin
from workers.comment_slot.policies import (
    CommentDispatchPolicy,
    evaluate_comment_dispatch_policies,
)

ROOT = Path(__file__).resolve().parents[1]


class _PreviewDatabase:
    def __init__(self) -> None:
        self.mutations: list[str] = []

    def get_settings(self, prefix=None):
        assert prefix == "openai."
        return {}

    def get_setting(self, key, default=None):
        if key == "telegram.account_id":
            return 123
        return default

    def get_account_restriction(self, account_id):
        assert int(account_id) == 123
        return {"active": False}

    def get_active_comment_campaign(self):
        return None

    def get_active_join_campaign(self):
        return None

    def count_channels_for_commenting(self, *, cooldown_hours):
        return 7 if int(cooldown_hours) == 0 else 5

    def get_telegram_account(self, account_id):
        assert int(account_id) == 123
        return {
            "display_name": "Audit account",
            "authorized": True,
            "stopped": False,
            "runtime_state": "connected",
        }

    def get_account_settings(self, account_id, prefix=None):
        assert int(account_id) == 123
        assert prefix == "telegram."
        return {
            "telegram.proxy_enabled": "1",
            "telegram.proxy_type": "SOCKS5",
            "telegram.proxy_host": "127.0.0.1",
            "telegram.proxy_port": "1080",
        }

    def __getattr__(self, name):
        if name.startswith(("create_", "insert_", "set_", "save_", "reserve_")):
            def mutation(*_args, **_kwargs):
                self.mutations.append(name)
                raise AssertionError(f"preview performed mutation: {name}")
            return mutation
        raise AttributeError(name)


class _PreviewAPI(CommentCampaignAPIMixin):
    COMMENT_CHANNEL_COOLDOWN_HOURS = 24
    campaign_hours = 24

    def __init__(self) -> None:
        self.database = _PreviewDatabase()

    def get_comment_daily_limit(self):
        return 40


def test_campaign_preview_is_read_only_and_reports_effective_plan() -> None:
    api = _PreviewAPI()

    preview = api.preview_comment_campaign(
        ["one", "one", "two"],
        continuous=True,
        daily_limit=6,
        comment_source=SOURCE_PREWRITTEN,
    )

    assert preview["account_id"] == 123
    assert preview["session_ready"] is True
    assert preview["proxy_ready"] is True
    assert preview["proxy_endpoint"] == "127.0.0.1:1080"
    assert preview["linked_channel_count"] == 7
    assert preview["eligible_channel_count"] == 5
    assert preview["requested_daily_limit"] == 6
    assert preview["planned_count"] == 5
    assert preview["telegram_mutation_count"] == 5
    assert preview["planned_join_count"] == 0
    assert preview["comment_variant_count"] == 2
    assert api.database.mutations == []


def test_dispatch_policy_pipeline_short_circuits_in_order() -> None:
    calls: list[str] = []

    def accepted() -> bool:
        calls.append("accepted")
        return True

    def rejected() -> bool:
        calls.append("rejected")
        return False

    def must_not_run() -> bool:
        calls.append("late")
        raise AssertionError("policy pipeline did not short-circuit")

    assert (
        evaluate_comment_dispatch_policies(
            (
                CommentDispatchPolicy("accepted", accepted),
                CommentDispatchPolicy("rejected", rejected),
                CommentDispatchPolicy("late", must_not_run),
            )
        )
        is False
    )
    assert calls == ["accepted", "rejected"]


class _HistoryRepository(CommentHistoryMixin):
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE comment_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
                task_id INTEGER, campaign_id INTEGER, slot_id INTEGER,
                channel_id INTEGER, post_id INTEGER, comment_text TEXT,
                sent_at TEXT, status TEXT
            );
            CREATE TABLE comment_deliveries(
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
                campaign_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL, comment_message_id INTEGER, status TEXT
            );
            CREATE TABLE comment_campaigns(
                id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL
            );
            CREATE TABLE comment_schedule(
                id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL, slot_index INTEGER,
                task_id INTEGER, channel_id INTEGER, post_id INTEGER, selected_text TEXT,
                executed_at TEXT, result TEXT, status TEXT
            );
            """
        )

    @contextmanager
    def get_connection(self):
        yield self.conn

    def get_setting(self, _key, default=None):
        return default


def test_campaign_history_exposes_confirmed_telegram_message_id() -> None:
    repo = _HistoryRepository()
    repo.conn.execute(
        """INSERT INTO comment_history(
               account_id, task_id, campaign_id, slot_id, channel_id, post_id,
               comment_text, sent_at, status
           ) VALUES(123, 10, 7, 1, 555, 42, 'hello', '2026-08-13', 'sent')"""
    )
    repo.conn.execute(
        """INSERT INTO comment_deliveries(
               account_id, campaign_id, channel_id, post_id, comment_message_id, status
           ) VALUES(123, 7, 555, 42, 9001, 'sent')"""
    )
    repo.conn.commit()

    rows = repo.get_comment_history(campaign_id=7, account_id=123, limit=10)

    assert len(rows) == 1
    assert rows[0]["comment_message_id"] == 9001


def test_gui_preview_precedes_campaign_start_and_receipt_id_is_visible() -> None:
    campaign_source = (
        ROOT / "gui/views/commenting_parts/campaign.py"
    ).read_text(encoding="utf-8")
    start = campaign_source[
        campaign_source.index("    def start_campaign(") : campaign_source.index(
            "    def pause_campaign("
        )
    ]
    assert "preview_comment_campaign(" in start
    assert "QMessageBox.question(" in start
    assert start.index("preview_comment_campaign(") < start.index(
        "start_comment_campaign("
    )
    assert "comment_message_id" in campaign_source

    view_source = (ROOT / "gui/views/commenting_view.py").read_text(encoding="utf-8")
    assert "QTableWidget(0, 5)" in view_source
    assert '"ID комментария"' in view_source

    runner_source = (ROOT / "workers/comment_slot/runner.py").read_text(
        encoding="utf-8"
    )
    assert "evaluate_comment_dispatch_policies(" in runner_source

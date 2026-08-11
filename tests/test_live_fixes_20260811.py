from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.config import (
    DEFAULT_LINK_CHECK_DELAY_MAX_SECONDS,
    DEFAULT_LINK_CHECK_DELAY_MIN_SECONDS,
)
from gui.theme import TELEGRAM_PREMIUM_QSS
from gui.views.account_parts.auth_flow import AccountViewAuthFlowMixin
from gui.views.audience_parser_view import AudienceParserView
from gui.views.commenting_parts.profile import CommentingProfileMixin
from gui.gui_service_adapter import GUIServiceAdapter
from gui.views.warmup_view import WarmupView
from storage.database import Database
from workers.queue_task_decisions import TaskExecutionContext
from workers.queue_worker import QueueWorker

ROOT = Path(__file__).resolve().parents[1]


class _Widget:
    def __init__(self, value=None):
        self.value = value
        self.visible = True
        self.enabled = None
        self.text = ""

    def currentData(self):
        return self.value

    def setProperty(self, *_args):
        return None

    def style(self):
        return self

    def unpolish(self, *_args):
        return None

    def polish(self, *_args):
        return None

    def update(self):
        return None

    def setVisible(self, value):
        self.visible = bool(value)

    def setText(self, value):
        self.text = str(value)

    def setEnabled(self, value):
        self.enabled = bool(value)


def test_live_01_comment_source_does_not_persist_without_account():
    saved = []

    class Harness(CommentingProfileMixin):
        pass

    view = Harness()
    view.comment_source_combo = _Widget("prepared")
    view.comments_title = _Widget()
    view.variant_count_label = _Widget()
    view.variant_rows = [_Widget()]
    view.import_previous_button = _Widget()
    view.save_comments_button = _Widget()
    view.save_status = _Widget()
    view.openai_card = _Widget()
    view._loading_openai_settings = False
    view._current_account_id = lambda: 0
    view.adapter = SimpleNamespace(
        save_openai_configuration=lambda value: saved.append(dict(value))
    )

    view._on_comment_source_changed()

    assert saved == []


def test_live_02_deferred_link_completion_is_not_reported_as_completed():
    class DB:
        @staticmethod
        def set_done(_task_id):
            return False

        @staticmethod
        def get_task(_task_id):
            return {
                "status": "pending",
                "not_before": "2026-08-11 12:00:00",
            }

    completed = []
    failed = []
    worker = SimpleNamespace(
        get_db=lambda: DB(),
        processed_count=0,
        failed_count=0,
        task_completed=SimpleNamespace(emit=lambda task_id: completed.append(task_id)),
        task_failed=SimpleNamespace(emit=lambda task_id, message: failed.append((task_id, message))),
    )

    async def handler(_task):
        return None

    context = TaskExecutionContext(
        task_id=77,
        task_type="link_channels",
        handler=handler,
        payload={},
        column_account_id=101,
        payload_account_id=101,
        account_id=101,
    )

    QueueWorker._persist_successful_task(worker, context)

    assert worker.processed_count == 0
    assert completed == []
    assert failed == []


def test_live_03_link_pacing_and_messagebox_contrast_contracts():
    assert DEFAULT_LINK_CHECK_DELAY_MIN_SECONDS == 105
    assert DEFAULT_LINK_CHECK_DELAY_MAX_SECONDS == 135
    for selector in (
        "QMessageBox {",
        "QMessageBox QLabel",
        "QMessageBox QPushButton",
    ):
        assert selector in TELEGRAM_PREMIUM_QSS


def test_live_04_add_account_runtime_text_keeps_information_card_static():
    status = _Widget()
    details = _Widget()
    status.text = "Добавление Telegram-аккаунта"
    details.text = "Заполните API ID, API Hash, телефон и отдельный proxy при необходимости"
    view = SimpleNamespace(
        _adding_account=True,
        _reauthorizing_account_id=0,
        status_label=status,
        account_label=details,
    )

    AccountViewAuthFlowMixin._set_auth_runtime_text(
        view, "Подключение к Telegram…", "runtime event"
    )

    assert status.text == "Добавление Telegram-аккаунта"
    assert details.text.startswith("Заполните API ID")


def test_live_05_parser_lists_all_authorized_accounts_even_when_one_has_campaign():
    class Adapter:
        @staticmethod
        def get_warmup_overview():
            return {
                "accounts": [
                    {
                        "telegram_account_id": 101,
                        "display_name": "A",
                        "authorized": True,
                        "campaign_active": True,
                    },
                    {
                        "telegram_account_id": 202,
                        "display_name": "B",
                        "authorized": True,
                        "campaign_active": False,
                    },
                ],
                "pairs": [],
            }

        @staticmethod
        def get_comment_campaign_state(*, account_id):
            if account_id == 101:
                return {"status": "running"}
            return None

        @staticmethod
        def get_join_campaign_state(*, account_id):
            return None

    class Harness:
        _campaign_state_for = staticmethod(AudienceParserView._campaign_state_for)
        _pair_status_map = staticmethod(AudienceParserView._pair_status_map)
        _account_identity = staticmethod(AudienceParserView._account_identity)
        _workflow_accounts = AudienceParserView._workflow_accounts

        def __init__(self):
            self.adapter = Adapter()

    rows = Harness()._workflow_accounts()

    assert {int(row["_account_id"]) for row in rows} == {101, 202}


def test_live_06_campaign_history_reconstructs_finalized_slot_when_history_row_missing(tmp_path):
    db = Database(tmp_path / "live-history.db")
    db.register_telegram_account(
        telegram_account_id=101,
        session_name="account_101",
        display_name="Live Test",
        authorized=True,
    )
    db.select_telegram_account(101)
    campaign = db.create_comment_campaign(
        ["test"],
        daily_limit=1,
        slot_count=1,
        continuous=False,
        account_id=101,
    )
    slot = db.get_comment_schedule(campaign["id"])[0]
    with db.get_connection() as conn:
        conn.execute(
            """UPDATE comment_schedule
               SET status='skipped', channel_id=5001, post_id=7001,
                   selected_text='test', result='slot_skipped_no_discussion',
                   executed_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (int(slot["id"]),),
        )

    rows = db.get_comment_history(
        campaign_id=int(campaign["id"]),
        account_id=101,
        limit=10,
    )

    assert len(rows) == 1
    assert int(rows[0]["slot_id"]) == int(slot["id"])
    assert rows[0]["status"] == "slot_skipped_no_discussion"


def test_live_07_warmup_ab_keeps_active_pair_accounts_visible_after_restart():
    accounts = [
        {
            "telegram_account_id": 101,
            "display_name": "A",
            "authorized": True,
            "stopped": False,
            "active_pair_id": 7,
        },
        {
            "telegram_account_id": 202,
            "display_name": "B",
            "authorized": True,
            "stopped": False,
            "active_pair_id": 7,
        },
        {
            "telegram_account_id": 303,
            "display_name": "Stopped",
            "authorized": True,
            "stopped": True,
            "active_pair_id": None,
        },
    ]

    visible = WarmupView._warmup_accounts_for_selectors(accounts)

    assert [int(item["telegram_account_id"]) for item in visible] == [101, 202, 303]
    assert "в связке #7" in WarmupView._warmup_choice_label(visible[0])
    assert "остановлен" in WarmupView._warmup_choice_label(visible[2])
    assert WarmupView._warmup_account_creatable(visible[0]) is False
    assert WarmupView._warmup_account_creatable(visible[2]) is False


def test_live_08_load_groups_stays_enabled_with_cached_groups():
    class Combo:
        def __init__(self):
            self.items = []

        def clear(self):
            self.items.clear()

        def addItem(self, text, data):
            self.items.append((text, data))

    view = SimpleNamespace(
        _source_guard=False,
        group_combo=Combo(),
        load_groups_button=_Widget(),
        groups_status=_Widget(),
        _account_id=101,
        current_task_id=None,
    )

    AudienceParserView._replace_groups(
        view,
        [{"peer_id": 1, "title": "Group", "username": ""}],
        loaded=True,
        syncing=False,
    )

    assert view.load_groups_button.enabled is True


def test_warmup_backend_campaign_exclusion_guard_is_preserved():
    source = (ROOT / "storage" / "db_account_activity.py").read_text(encoding="utf-8")
    assert "_active_campaign_in_transaction" in source
    assert "CAMPAIGN_WARMUP_CONFLICT_MESSAGE" in source
    assert "acquire_account_activity_lease" in source


def test_live_09_warmup_buttons_derive_from_durable_overview_not_busy_flag_only():
    create = _Widget()
    load = _Widget()
    manual = _Widget()
    view = SimpleNamespace(
        _busy=False,
        _overview={
            "accounts": [
                {
                    "telegram_account_id": 101,
                    "authorized": True,
                    "stopped": False,
                    "active_pair_id": 7,
                },
                {
                    "telegram_account_id": 202,
                    "authorized": True,
                    "stopped": False,
                    "active_pair_id": 7,
                },
            ]
        },
        account_a=_Widget(101),
        account_b=_Widget(202),
        create_button=create,
        load_groups_button=load,
        add_group_button=manual,
    )

    WarmupView._set_busy(view, False)

    assert create.enabled is False
    assert load.enabled is True
    assert manual.enabled is True

    WarmupView._set_busy(view, True)
    assert create.enabled is False
    assert load.enabled is False
    assert manual.enabled is False


def test_live_12_warmup_selector_snapshot_keeps_group_loading_available():
    create = _Widget()
    load = _Widget()
    manual = _Widget()
    selector_accounts = [
        {
            "telegram_account_id": 101,
            "authorized": True,
            "stopped": False,
            "active_pair_id": 7,
        },
        {
            "telegram_account_id": 202,
            "authorized": True,
            "stopped": False,
            "active_pair_id": 7,
        },
    ]
    view = SimpleNamespace(
        _busy=False,
        _overview={"accounts": []},
        _selector_accounts=selector_accounts,
        account_a=_Widget(101),
        account_b=_Widget(202),
        create_button=create,
        load_groups_button=load,
        add_group_button=manual,
    )

    WarmupView._set_busy(view, False)

    assert create.enabled is False
    assert load.enabled is True
    assert manual.enabled is True


def test_live_10_warmup_mutation_finish_always_requests_authoritative_refresh():
    events = []
    view = SimpleNamespace(
        _set_busy=lambda active: events.append(("busy", bool(active))),
        refresh=lambda *, force=False: events.append(("refresh", bool(force))),
    )

    WarmupView._finish_mutation(view, refresh_after=True)

    assert events == [("busy", False), ("refresh", True)]


def test_live_11_adapter_exposes_synced_warmup_group_population():
    calls = []

    class API:
        @staticmethod
        def populate_warmup_groups_from_synced(account_id):
            calls.append(int(account_id))
            return {"selected_count": 3}

    result = GUIServiceAdapter(API()).populate_warmup_groups_from_synced(101)

    assert result == {"selected_count": 3}
    assert calls == [101]

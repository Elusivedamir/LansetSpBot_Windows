from __future__ import annotations

import inspect
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.exceptions import DeferredTelegramError
from gui.gui_service_adapter import GUIServiceAdapter
from gui.views.account_view import AccountView
from gui.views.channels_view import ChannelsView
from services.api_parts.comments import CommentCampaignAPIMixin
from services.api_parts.joins import JoinCampaignAPIMixin
from services.api_parts.task_queue import TaskQueueAPIMixin
from storage.database import Database
from workers.queue_worker import QueueWorker
from tests.conftest import open_project_database


def test_stop_and_dispatch_share_one_linearization_barrier() -> None:
    worker = QueueWorker(lambda: {})
    barrier = worker.create_scope_dispatch_barrier(("comment_campaign", 41))
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    order: list[str] = []

    def dispatch() -> None:
        with barrier.dispatch():
            order.append("dispatch_entered")
            entered.set()
            assert release.wait(2)
            order.append("dispatch_scheduled")

    def stop() -> None:
        assert entered.wait(2)
        worker.cancel_scopes_and_run(
            (("comment_campaign", 41),),
            lambda: order.append("stop_committed") or True,
        )
        stopped.set()

    dispatch_thread = threading.Thread(target=dispatch)
    stop_thread = threading.Thread(target=stop)
    dispatch_thread.start()
    stop_thread.start()
    assert entered.wait(2)
    time.sleep(0.02)
    assert not stopped.is_set(), "Stop must wait for an RPC already crossing dispatch"
    release.set()
    dispatch_thread.join(2)
    stop_thread.join(2)

    assert order == ["dispatch_entered", "dispatch_scheduled", "stop_committed"]
    with pytest.raises(DeferredTelegramError) as exc_info:
        with barrier.dispatch():
            pass
    assert exc_info.value.code == "shutdown_before_dispatch"


def test_comment_and_join_stop_use_atomic_worker_mutation() -> None:
    order: list[object] = []

    class Worker:
        def cancel_scopes_and_run(self, scopes, mutation):
            order.append(("cancel", tuple(scopes)))
            return mutation()

    class DB:
        def get_active_comment_campaign(self):
            return {"id": 11, "status": "running"}

        def stop_comment_campaign(self, campaign_id):
            order.append(("comment_db", campaign_id))
            return True

        def get_active_join_campaign(self):
            return {"id": 12, "status": "running"}

        def stop_join_campaign(self, campaign_id):
            order.append(("join_db", campaign_id))
            return True

    host = SimpleNamespace(database=DB(), queue_worker=Worker())
    host._request_scope_cancellation = lambda *args: order.append(("fallback", args))
    host._cancel_scopes_and_mutate = (
        TaskQueueAPIMixin._cancel_scopes_and_mutate.__get__(host)
    )

    assert CommentCampaignAPIMixin.stop_comment_campaign(host)
    assert JoinCampaignAPIMixin.stop_join_campaign(host)
    assert order == [
        ("cancel", (("comment_campaign", 11),)),
        ("comment_db", 11),
        ("cancel", (("join_campaign", 12),)),
        ("join_db", 12),
    ]


def test_delivery_identity_ignores_changed_discussion_and_action(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "delivery.db")
    db.set_setting("telegram.account_id", 7001)

    assert db.reserve_comment_delivery(
        -10010,
        55,
        linked_chat_id=-10020,
        campaign_id=77,
        action_type="campaign_comment",
        account_id=7001,
        text="first",
    )
    assert not db.reserve_comment_delivery(
        -10010,
        55,
        linked_chat_id=-10030,
        campaign_id=77,
        action_type="manual_comment",
        account_id=7001,
        text="duplicate route",
    )
    assert db.has_commented(
        -10010,
        55,
        linked_chat_id=-10030,
        campaign_id=77,
        action_type="manual_comment",
        account_id=7001,
    )


def test_join_events_are_persisted_with_microseconds(tmp_path: Path) -> None:
    db = Database(tmp_path / "join-time.db")
    db.set_setting("telegram.account_id", 7002)
    db.record_join_event(-10099, account_id=7002)
    with db.get_connection() as conn:
        value = str(
            conn.execute(
                "SELECT joined_at FROM join_events ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}", value)


def test_account_layout_uses_owned_cancellable_timers() -> None:
    init_source = inspect.getsource(AccountView.__init__)
    refresh_source = inspect.getsource(AccountView._refresh_dynamic_layout)
    assert "self._dynamic_layout_queued_timer = QTimer(self)" in init_source
    assert "self._dynamic_layout_settle_timer = QTimer(self)" in init_source
    assert "QTimer.singleShot" not in refresh_source
    assert "lambda" not in refresh_source


def test_channel_delete_is_transactional_and_keeps_delivery_receipts(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "delete.db")
    account_id = 7003
    peer_id = -100777
    db.set_setting("telegram.account_id", account_id)
    db.upsert_channels_batch(
        [
            {
                "channel_id": peer_id,
                "username": "delete_me",
                "title": "Delete me",
                "comment_mode": "channel_post",
                "linked_chat_id": -100778,
                "link_status": "linked",
            }
        ],
        account_id=account_id,
    )
    dialog_id = db.upsert_saved_dialog(
        {
            "peer_id": peer_id,
            "username": "delete_me",
            "title": "Delete me",
            "kind": "channel",
        },
        account_id=account_id,
    )
    db.set_saved_dialog_membership(dialog_id, account_id, "left")

    comment_campaign = db.create_comment_campaign(
        ["hello"], daily_limit=1, slot_count=1, account_id=account_id
    )
    comment_task = db.insert_task(
        "auto_comment_slot",
        {"account_id": account_id, "campaign_id": comment_campaign["id"], "slot_id": 1},
        0,
    )
    join_campaign = db.create_join_campaign(account_id, max_per_hour=1000)
    join_task = db.insert_task(
        "join_saved_slot",
        {"account_id": account_id, "campaign_id": join_campaign["id"], "slot_id": 1},
        0,
    )
    with db.get_connection() as conn:
        comment_slot = conn.execute(
            "SELECT id FROM comment_schedule WHERE campaign_id=?",
            (comment_campaign["id"],),
        ).fetchone()[0]
        join_slot = conn.execute(
            "SELECT id FROM join_schedule WHERE campaign_id=?",
            (join_campaign["id"],),
        ).fetchone()[0]
        conn.execute(
            "UPDATE tasks SET status='running' WHERE id IN (?, ?)",
            (comment_task, join_task),
        )
        conn.execute(
            """UPDATE comment_schedule
               SET status='running', task_id=?, channel_id=? WHERE id=?""",
            (comment_task, peer_id, comment_slot),
        )
        conn.execute(
            "UPDATE join_schedule SET status='running', task_id=? WHERE id=?",
            (join_task, join_slot),
        )
    assert db.reserve_comment_delivery(
        peer_id,
        99,
        linked_chat_id=-100778,
        campaign_id=comment_campaign["id"],
        action_type="campaign_comment",
        account_id=account_id,
    )

    result = db.delete_channels_transactional([peer_id], account_id=account_id)

    assert result["cancelled_task_ids"] == [comment_task, join_task]
    assert result["comment_slot_count"] == 1
    assert result["join_slot_count"] == 1
    with db.get_connection() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM channels WHERE account_id=? AND channel_id=?",
                (account_id, peer_id),
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM saved_dialogs WHERE id=?", (dialog_id,)
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT status FROM comment_schedule WHERE id=?", (comment_slot,)
            ).fetchone()[0]
            == "cancelled"
        )
        assert (
            conn.execute(
                "SELECT 1 FROM join_schedule WHERE id=?", (join_slot,)
            ).fetchone()
            is None
        )
        statuses = {
            row[0]
            for row in conn.execute(
                "SELECT status FROM tasks WHERE id IN (?, ?)",
                (comment_task, join_task),
            )
        }
        assert statuses == {"cancelled"}
        assert (
            conn.execute(
                """SELECT 1 FROM comment_deliveries
               WHERE account_id=? AND campaign_id=? AND channel_id=? AND post_id=?""",
                (account_id, comment_campaign["id"], peer_id, 99),
            ).fetchone()
            is not None
        )


def test_gui_adapter_exposes_full_delete_chain() -> None:
    assert hasattr(GUIServiceAdapter, "delete_channels")
    assert hasattr(ChannelsView, "delete_selected_channels")


def test_v23_delivery_routes_collapse_to_one_source_receipt(tmp_path: Path) -> None:
    path = tmp_path / "delivery-v23.db"
    db = Database(path)
    db.close()


    conn = open_project_database(path)
    try:
        conn.executescript(
            """
            DROP TABLE comment_deliveries;
            CREATE TABLE comment_deliveries(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL DEFAULT 0,
                campaign_id INTEGER NOT NULL DEFAULT 0,
                action_type TEXT NOT NULL DEFAULT 'comment',
                channel_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                linked_chat_id INTEGER NOT NULL DEFAULT 0,
                comment_message_id INTEGER,
                text TEXT,
                status TEXT NOT NULL DEFAULT 'sending',
                error TEXT,
                reserved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_id, campaign_id, action_type, channel_id, post_id, linked_chat_id)
            );
            INSERT INTO comment_deliveries(
                account_id, campaign_id, action_type, channel_id, post_id,
                linked_chat_id, status, updated_at
            ) VALUES(9, 8, 'campaign_comment', -1001, 42, -2001, 'sent',
                     '2026-07-18 10:00:00');
            INSERT INTO comment_deliveries(
                account_id, campaign_id, action_type, channel_id, post_id,
                linked_chat_id, status, updated_at
            ) VALUES(9, 8, 'manual_comment', -1001, 42, -3001, 'sending',
                     '2026-07-18 11:00:00');
            DELETE FROM migrations WHERE version=24;
            PRAGMA user_version=23;
            """
        )
        conn.commit()
    finally:
        conn.close()

    migrated = Database(path)
    assert migrated.get_version() == Database.SCHEMA_VERSION == 30
    with migrated.get_connection() as active:
        rows = active.execute(
            """SELECT linked_chat_id, status FROM comment_deliveries
               WHERE account_id=9 AND campaign_id=8 AND channel_id=-1001 AND post_id=42"""
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert not migrated.reserve_comment_delivery(
        -1001,
        42,
        account_id=9,
        campaign_id=8,
        linked_chat_id=-4001,
        action_type="campaign_comment",
    )


def test_atomic_join_finalizer_writes_precise_event_time(tmp_path: Path) -> None:
    db = Database(tmp_path / "join-finalizer-time.db")
    account_id = 7004
    peer_id = -100888
    db.set_setting("telegram.account_id", account_id)
    dialog_id = db.upsert_saved_dialog(
        {
            "peer_id": peer_id,
            "username": "join_time",
            "title": "Join time",
            "kind": "channel",
        },
        account_id=account_id,
    )
    db.set_saved_dialog_membership(dialog_id, account_id, "left")
    campaign = db.create_join_campaign(account_id, max_per_hour=1000)
    task_id = db.insert_task(
        "join_saved_slot",
        {"account_id": account_id, "campaign_id": campaign["id"], "slot_id": 1},
        0,
    )
    with db.get_connection() as conn:
        slot_id = conn.execute(
            "SELECT id FROM join_schedule WHERE campaign_id=?", (campaign["id"],)
        ).fetchone()[0]
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.execute(
            "UPDATE join_schedule SET status='running', task_id=? WHERE id=?",
            (task_id, slot_id),
        )

    assert db.finalize_join_slot_outcome(
        task_id,
        slot_id,
        status="joined",
        result="joined",
        joined=True,
        saved_dialog_id=dialog_id,
        account_id=account_id,
        membership_status="member",
        join_event_peer_id=peer_id,
    )
    with db.get_connection() as conn:
        value = str(
            conn.execute(
                "SELECT joined_at FROM join_events ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}", value)

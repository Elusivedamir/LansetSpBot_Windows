from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QScrollArea
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.contacts import ResolveUsernameRequest
from telethon.tl.functions.messages import SendMessageRequest
from telethon.tl.types import InputChannel, InputPeerSelf

from core.account_restriction import (
    activate_account_restriction,
    clear_account_restriction_after_spambot_confirmation,
    get_account_restriction_state,
)
from core.rate_limiter import RateLimiter, RpcCategory, classify_rpc_request
from gui.activity_panel import ActivityPanel
from gui.views.instructions_view import InstructionsView
from services.telegram_service import TelegramService
from storage.database import Database


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_global_restriction_persists_and_cancels_only_mutating_tasks(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "restriction.db")
    mutating_id = db.insert_task("auto_comment_slot", {"campaign_id": 1, "slot_id": 1})
    read_id = db.insert_task("sync_channels", {})

    state = activate_account_restriction(
        db,
        code="peer_flood",
        message="Telegram PeerFlood restriction",
        details={"rpc_error": "PeerFloodError"},
    )

    assert state["active"] is True
    persisted = get_account_restriction_state(db)
    assert persisted["active"] is True
    assert persisted["code"] == "peer_flood"
    assert persisted["details"]["rpc_error"] == "PeerFloodError"
    assert db.get_task(mutating_id)["status"] == "cancelled"
    assert db.get_task(read_id)["status"] == "pending"

    cleared = clear_account_restriction_after_spambot_confirmation(db)
    assert cleared["active"] is False
    assert get_account_restriction_state(db)["active"] is False


def test_rpc_categories_keep_read_resolve_join_and_send_separate() -> None:
    assert (
        classify_rpc_request(ResolveUsernameRequest(username="example"))
        is RpcCategory.RESOLVE_ENTITY
    )
    assert (
        classify_rpc_request(
            JoinChannelRequest(InputChannel(channel_id=1, access_hash=2))
        )
        is RpcCategory.JOIN
    )
    assert (
        classify_rpc_request(
            SendMessageRequest(peer=InputPeerSelf(), message="x", random_id=1)
        )
        is RpcCategory.SEND_COMMENT
    )
    assert classify_rpc_request(object()) is RpcCategory.READ


@pytest.mark.asyncio
async def test_rate_limiter_keeps_independent_category_counters() -> None:
    RateLimiter._reset_for_tests()
    limiter = RateLimiter(1.0)
    limiter.interval = 0.001
    async with limiter.request_slot(RpcCategory.READ):
        pass
    async with limiter.request_slot(RpcCategory.RESOLVE_ENTITY):
        pass
    async with limiter.request_slot(RpcCategory.JOIN):
        pass
    async with limiter.request_slot(RpcCategory.SEND_COMMENT):
        pass
    assert RateLimiter.category_snapshot() == {
        "READ": 1,
        "RESOLVE_ENTITY": 1,
        "JOIN": 1,
        "SEND_COMMENT": 1,
    }


def test_flood_wait_buffer_is_random_thirty_to_forty_five_seconds() -> None:
    assert TelegramService.FLOOD_WAIT_BUFFER_MIN_SECONDS == 30
    assert TelegramService.FLOOD_WAIT_BUFFER_MAX_SECONDS == 45


def test_instruction_view_is_a_real_multi_step_slideshow() -> None:
    _app()
    view = InstructionsView()
    total = len(view.STEPS)
    assert view.stack.count() == total
    assert total >= 11, "the guide must stay a full multi-step walkthrough"
    assert view.progress_label.text() == f"Шаг 1 из {total}"
    first = view.stack.widget(0)
    assert isinstance(first, QScrollArea)
    assert first.widgetResizable() is True
    image = first.findChild(type(view.progress_label), "instructionImage")
    assert image is not None
    assert image.pixmap() is not None and not image.pixmap().isNull()
    view.next_step()
    assert view.progress_label.text() == f"Шаг 2 из {total}"
    assert view.back_button.isEnabled()
    view.deleteLater()


def test_activity_panel_has_spambot_button_without_requiring_new_adapter_api() -> None:
    class Adapter:
        def get_logs(self, level=None, limit=100):
            return []

        def get_scheduler_error(self):
            return ""

        def get_comment_campaign_state(self):
            return None

        def get_join_campaign_state(self):
            return None

    _app()
    panel = ActivityPanel(Adapter())
    panel.timer.stop()
    assert "@SpamBot" in panel.spambot_button.text()
    panel.deleteLater()


def test_activity_panel_enables_spambot_after_persistent_restriction() -> None:
    class Adapter:
        def get_logs(self, level=None, limit=100):
            return []

        def get_scheduler_error(self):
            return ""

        def get_comment_campaign_state(self):
            return None

        def get_join_campaign_state(self):
            return None

        def get_account_restriction_state(self):
            return {"active": True, "code": "peer_flood"}

    _app()
    panel = ActivityPanel(Adapter())
    panel.timer.stop()
    panel.refresh()
    assert panel.spambot_button.isEnabled()
    assert "RESTRICTED" in panel.state_label.text()
    assert "оставшиеся вступления остановлены" in panel.feed.toPlainText()
    panel.deleteLater()

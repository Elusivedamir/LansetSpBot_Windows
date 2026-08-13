from __future__ import annotations

from types import SimpleNamespace

from core.exceptions import TelegramOperationError
from gui.gui_service_adapter import GUIServiceAdapter
from workers.comment_slot.decisions import nonretryable_comment_decision
from workers.comment_slot.models import CommentSlotPhase
from workers.comment_slot.runner import CommentSlotRunner
from workers.comment_slot.state import CommentSlotState


class _ReasonAPI:
    @staticmethod
    def get_queue_unavailable_reason() -> str:
        return "account_transition_pending"


class _FakeWorkerDB:
    def __init__(self) -> None:
        self.defer_calls = 0

    def defer_comment_slot_and_set_network_wait(self, *args, **kwargs) -> bool:
        self.defer_calls += 1
        return True


def _runner(phase: CommentSlotPhase) -> tuple[CommentSlotRunner, _FakeWorkerDB]:
    worker_db = _FakeWorkerDB()
    runner = CommentSlotRunner(
        as_int=lambda value, default: int(value if value is not None else default),
        queue_worker=SimpleNamespace(),
        config=SimpleNamespace(),
        worker_db=worker_db,
        telegram=SimpleNamespace(),
        comments=SimpleNamespace(),
        openai_service=None,
        set_runtime=lambda *args, **kwargs: None,
        task={},
    )
    state = CommentSlotState(
        task_id=101,
        payload={},
        campaign_id=202,
        slot_id=303,
        campaign={},
        campaign_account_id=404,
        payload_account_id=404,
    )
    state.channel_id = -100500
    state.post_id = 77
    state.linked_chat_id = -100600
    state.discussion_chat_id = -100600
    state.discussion_message_id = 88
    state.phase = phase
    runner.state = state
    runner._safe_log = lambda *args, **kwargs: None
    return runner, worker_db


def test_account_transition_pending_has_specific_gui_message() -> None:
    adapter = GUIServiceAdapter(_ReasonAPI())
    message = adapter.get_queue_unavailable_message()
    assert "Не завершено сохранение состояния Telegram-аккаунта" in message
    assert message != "Фоновый обработчик недоступен"


def test_post_send_telegram_operation_error_is_uncertain_and_not_retried() -> None:
    runner, worker_db = _runner(CommentSlotPhase.SEND_STARTED)
    runner._handle_telegram_operation_error(
        TelegramOperationError("unexpected post-dispatch failure")
    )
    state = runner.s
    assert state.final_status == "uncertain"
    assert state.slot_deferred is False
    assert state.consume_channel is True
    assert state.campaign_pause_reason == state.final_message
    assert "результат отправки" in state.final_message
    assert worker_db.defer_calls == 0


def test_pre_send_telegram_operation_error_remains_deferred() -> None:
    runner, worker_db = _runner(CommentSlotPhase.PRECHECK)
    runner._handle_telegram_operation_error(
        TelegramOperationError("pre-dispatch transient failure")
    )
    state = runner.s
    assert state.slot_deferred is True
    assert state.consume_channel is False
    assert state.campaign_pause_reason is None
    assert worker_db.defer_calls == 1


def test_persist_failures_are_uncertain_and_pause_campaign() -> None:
    for code in ("delivery_persist_failed", "direct_message_persist_failed"):
        decision = nonretryable_comment_decision(code, "fallback")
        assert decision.final_status == "uncertain"
        assert decision.consume_channel is True
        assert decision.pause_campaign is True

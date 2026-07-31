from __future__ import annotations



from contextlib import contextmanager


from unittest.mock import MagicMock



import pytest



from core.campaign_state import CampaignStatus

from core.rpc_audit import RPCAudit

from core.startup_pipeline import StartupPhase, StartupPipeline

from services.application_facade import ApplicationFacade

from services.telegram_request_policy import (

    RequestKind,

    RetryClass,

    TelegramRequestPolicy,

)

from storage.atomic_workflow import AtomicWorkflow, AtomicWorkflowError





class _DB:

    def __init__(self) -> None:

        self.events: list[str] = []



    @contextmanager

    def get_connection(self):

        self.events.append("begin")

        try:

            yield object()

        except Exception:

            self.events.append("rollback")

            raise

        else:

            self.events.append("commit")





def test_stage_2_reserve_and_enqueue_share_one_transaction() -> None:

    db = _DB()

    workflow = AtomicWorkflow(db)

    calls: list[str] = []



    result = workflow.reserve_and_enqueue(

        reserve=lambda: calls.append("reserve") or True,

        enqueue=lambda: calls.append("enqueue") or 42,

    )



    assert result == 42

    assert calls == ["reserve", "enqueue"]

    assert db.events == ["begin", "commit"]





def test_stage_2_failed_reservation_rolls_back_without_enqueue() -> None:

    db = _DB()

    workflow = AtomicWorkflow(db)

    enqueue = MagicMock()



    with pytest.raises(AtomicWorkflowError):

        workflow.reserve_and_enqueue(reserve=lambda: False, enqueue=enqueue)



    enqueue.assert_not_called()

    assert db.events == ["begin", "rollback"]





def test_stage_2_compare_and_set_validates_campaign_transition() -> None:

    workflow = AtomicWorkflow(_DB())

    update = MagicMock(return_value=True)



    result = workflow.compare_and_set_campaign(

        current="running",

        target="paused_floodwait",

        update=update,

    )



    assert result.changed

    update.assert_called_once_with(

        CampaignStatus.RUNNING,

        CampaignStatus.PAUSED_FLOODWAIT,

    )





def test_stage_3_send_timeout_after_dispatch_is_uncertain() -> None:

    policy = TelegramRequestPolicy()

    decision = policy.classify(

        TimeoutError("connection timeout"),

        request_kind=RequestKind.SEND,

        send_started=True,

    )



    assert decision.retry_class is RetryClass.UNCERTAIN

    assert decision.retry_after is None





def test_stage_3_floodwait_is_deferred_with_buffer() -> None:

    class FloodWaitError(Exception):

        seconds = 120



    policy = TelegramRequestPolicy()

    decision = policy.classify(

        FloodWaitError("FLOOD_WAIT"),

        request_kind=RequestKind.JOIN,

    )



    assert decision.retry_class is RetryClass.DEFERRED

    assert decision.retry_after is not None

    assert 180 <= decision.retry_after <= 300





def test_stage_4_facade_delegates_without_breaking_legacy_api() -> None:

    api = MagicMock()

    api.list_telegram_accounts.return_value = [{"telegram_account_id": 1}]

    api.get_comment_campaign_state.return_value = {"id": 7, "status": "running"}

    api.start_queue.return_value = True



    facade = ApplicationFacade.from_legacy_api(api)



    assert facade.accounts.list() == [{"telegram_account_id": 1}]

    assert facade.comments.state() == {"id": 7, "status": "running"}

    assert facade.runtime.start_queue() is True





def test_stage_4_startup_pipeline_stops_after_first_failure() -> None:

    calls: list[str] = []

    pipeline = (

        StartupPipeline()

        .add(

            StartupPhase.PROFILE_SECURITY,

            lambda: calls.append("security") or "ok",

        )

        .add(

            StartupPhase.DATABASE,

            lambda: (_ for _ in ()).throw(RuntimeError("broken database")),

        )

        .add(

            StartupPhase.RUNTIME_READY,

            lambda: calls.append("runtime") or "ready",

        )

    )



    report = pipeline.run()



    assert not report.ok

    assert report.failed_phase is StartupPhase.DATABASE

    assert calls == ["security"]





def test_stage_5_rpc_audit_detects_duplicate_and_floor_violation() -> None:

    audit = RPCAudit(minimum_interval_seconds=1.0)



    audit.record("GetHistory", account_id=10, now=100.0)

    audit.record("GetHistory", account_id=10, now=100.05)

    audit.record("SendMessage", account_id=10, now=102.0)



    snapshot = audit.snapshot()

    assert snapshot.total == 3

    assert snapshot.by_operation["GetHistory"] == 2

    assert snapshot.by_account[10] == 3

    assert snapshot.duplicate_suspicions == 1

    assert snapshot.minimum_interval_violations == 1


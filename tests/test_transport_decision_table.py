from __future__ import annotations

import pytest

from services.telegram_transport_decisions import (
    CancellationAction,
    NetworkFailureAction,
    OperationFailureAction,
    RpcFailureAction,
    cancellation_action,
    network_failure_decision,
    operation_failure_decision,
    rpc_failure_action,
)


@pytest.mark.parametrize(
    ("retry_network", "dispatched", "expected"),
    [
        (False, False, CancellationAction.DEFER_BEFORE_DISPATCH),
        (False, True, CancellationAction.PROPAGATE),
        (True, False, CancellationAction.PROPAGATE),
        (True, True, CancellationAction.PROPAGATE),
    ],
)
def test_cancellation_table(retry_network, dispatched, expected) -> None:
    assert cancellation_action(
        retry_network=retry_network,
        request_dispatched=dispatched,
    ) is expected


@pytest.mark.parametrize(
    ("retry_network", "dispatched", "attempts", "action", "next_attempts"),
    [
        (False, True, 0, NetworkFailureAction.UNCERTAIN, 0),
        (False, False, 0, NetworkFailureAction.RETRY, 1),
        (True, True, 0, NetworkFailureAction.RETRY, 1),
        (True, False, 1, NetworkFailureAction.RETRY, 2),
        (True, False, 2, NetworkFailureAction.EXHAUSTED, 3),
    ],
)
def test_network_failure_table(
    retry_network, dispatched, attempts, action, next_attempts
) -> None:
    decision = network_failure_decision(
        retry_network=retry_network,
        request_dispatched=dispatched,
        attempts=attempts,
        max_attempts=3,
    )
    assert decision.action is action
    assert decision.attempts == next_attempts


@pytest.mark.parametrize(
    ("dispatched", "attempts", "action", "next_attempts"),
    [
        (True, 0, OperationFailureAction.PROPAGATE, 0),
        (False, 0, OperationFailureAction.RETRY, 1),
        (False, 1, OperationFailureAction.RETRY, 2),
        (False, 2, OperationFailureAction.EXHAUSTED, 3),
    ],
)
def test_operation_failure_table(dispatched, attempts, action, next_attempts) -> None:
    decision = operation_failure_decision(
        request_dispatched=dispatched,
        attempts=attempts,
        max_attempts=3,
    )
    assert decision.action is action
    assert decision.attempts == next_attempts


@pytest.mark.parametrize(
    ("code", "name", "text", "retry", "dispatched", "expected"),
    [
        (400, "UserRestrictedError", "", False, True, RpcFailureAction.USER_RESTRICTED),
        (400, "RPCError", "USER_RESTRICTED", False, True, RpcFailureAction.USER_RESTRICTED),
        (400, "AuthKeyDuplicatedError", "", False, True, RpcFailureAction.AUTH_KEY_DUPLICATED),
        (500, "ServerError", "", False, True, RpcFailureAction.UNCERTAIN),
        (500, "ServerError", "", True, True, RpcFailureAction.DEFER),
        (400, "TimedOutError", "", False, False, RpcFailureAction.DEFER),
        (400, "RPCError", "BAD_REQUEST", False, True, RpcFailureAction.GENERIC),
    ],
)
def test_rpc_failure_table(code, name, text, retry, dispatched, expected) -> None:
    assert rpc_failure_action(
        rpc_code=code,
        rpc_name=name,
        rpc_text=text,
        retry_network=retry,
        request_dispatched=dispatched,
    ) is expected

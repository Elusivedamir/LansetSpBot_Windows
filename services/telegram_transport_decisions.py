"""Pure state decisions for the Telegram transport boundary.

No Telethon, Qt, database, or network imports are allowed here.  Keeping the
retry/deferral/uncertainty table independent makes the no-replay contract easy
to test without constructing a live Telegram client.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CancellationAction(StrEnum):
    DEFER_BEFORE_DISPATCH = "defer_before_dispatch"
    PROPAGATE = "propagate"


class NetworkFailureAction(StrEnum):
    UNCERTAIN = "uncertain"
    RETRY = "retry"
    EXHAUSTED = "exhausted"


class OperationFailureAction(StrEnum):
    PROPAGATE = "propagate"
    RETRY = "retry"
    EXHAUSTED = "exhausted"


class RpcFailureAction(StrEnum):
    USER_RESTRICTED = "user_restricted"
    AUTH_KEY_DUPLICATED = "auth_key_duplicated"
    UNCERTAIN = "uncertain"
    DEFER = "defer"
    GENERIC = "generic"


@dataclass(frozen=True, slots=True)
class AttemptDecision:
    action: NetworkFailureAction | OperationFailureAction
    attempts: int


_TRANSIENT_RPC_NAMES = frozenset(
    {
        "ServerError",
        "TimedOutError",
        "InterdcCallErrorError",
        "InterdcCallRichErrorError",
        "RpcMcgetFailError",
        "WorkerBusyTooLongRetryError",
    }
)


def cancellation_action(
    *, retry_network: bool, request_dispatched: bool
) -> CancellationAction:
    if not retry_network and not request_dispatched:
        return CancellationAction.DEFER_BEFORE_DISPATCH
    return CancellationAction.PROPAGATE


def network_failure_decision(
    *,
    retry_network: bool,
    request_dispatched: bool,
    attempts: int,
    max_attempts: int,
) -> AttemptDecision:
    if not retry_network and request_dispatched:
        return AttemptDecision(NetworkFailureAction.UNCERTAIN, attempts)
    next_attempts = int(attempts) + 1
    if next_attempts >= int(max_attempts):
        return AttemptDecision(NetworkFailureAction.EXHAUSTED, next_attempts)
    return AttemptDecision(NetworkFailureAction.RETRY, next_attempts)


def operation_failure_decision(
    *, request_dispatched: bool, attempts: int, max_attempts: int
) -> AttemptDecision:
    if request_dispatched:
        return AttemptDecision(OperationFailureAction.PROPAGATE, attempts)
    next_attempts = int(attempts) + 1
    if next_attempts >= int(max_attempts):
        return AttemptDecision(OperationFailureAction.EXHAUSTED, next_attempts)
    return AttemptDecision(OperationFailureAction.RETRY, next_attempts)


def rpc_failure_action(
    *,
    rpc_code: int,
    rpc_name: str,
    rpc_text: str,
    retry_network: bool,
    request_dispatched: bool,
) -> RpcFailureAction:
    normalized_name = str(rpc_name or "")
    normalized_text = str(rpc_text or "").upper()
    if normalized_name == "UserRestrictedError" or "USER_RESTRICTED" in normalized_text:
        return RpcFailureAction.USER_RESTRICTED
    if (
        normalized_name == "AuthKeyDuplicatedError"
        or "AUTH_KEY_DUPLICATED" in normalized_text
    ):
        return RpcFailureAction.AUTH_KEY_DUPLICATED
    if int(rpc_code or 0) >= 500 or normalized_name in _TRANSIENT_RPC_NAMES:
        if not retry_network and request_dispatched:
            return RpcFailureAction.UNCERTAIN
        return RpcFailureAction.DEFER
    return RpcFailureAction.GENERIC

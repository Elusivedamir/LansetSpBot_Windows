from __future__ import annotations

import asyncio
from typing import Any

from core.account_safety import (
    conservative_request_spacing_seconds,
    telegram_request_is_mutating,
    telegram_request_name,
)
from core.exceptions import DeferredTelegramError, NonRetryableTelegramError


class AccountSafetyRequestGate:
    """Account-scoped mutation gate layered before the global RateLimiter."""

    def __init__(self, worker_db: Any, *, account_id: int) -> None:
        self.worker_db = worker_db
        self.account_id = max(0, int(account_id or 0))

    async def wait_for_request(self, request: Any) -> None:
        if self.account_id <= 0 or not telegram_request_is_mutating(request):
            return
        reserver = getattr(self.worker_db, "reserve_account_safety_request", None)
        if not callable(reserver):
            return
        name = telegram_request_name(request)
        spacing = conservative_request_spacing_seconds(request)
        while True:
            decision = dict(
                reserver(
                    account_id=self.account_id,
                    request_name=name,
                    spacing_seconds=spacing,
                ) or {}
            )
            action = str(decision.get("action") or "allow")
            if action == "allow":
                return
            if action == "block":
                raise NonRetryableTelegramError(
                    str(decision.get("reason_text") or "Account safety blocked Telegram mutation"),
                    code=str(decision.get("reason_code") or "account_safety_blocked"),
                )
            if action == "postpone":
                raise DeferredTelegramError(
                    str(decision.get("reason_text") or "Adaptive protective mode"),
                    code="account_safety_protective",
                    retry_after=max(30, int(decision.get("wait_seconds") or 30)),
                )
            if action != "wait":
                raise RuntimeError(f"Unknown account safety RPC action: {action}")
            await asyncio.sleep(min(1.0, max(0.05, float(decision.get("wait_seconds") or 1))))

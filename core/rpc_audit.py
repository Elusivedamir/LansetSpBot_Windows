from __future__ import annotations

ARCHITECTURE_STATUS = "experimental"




import threading

import time

from collections import Counter

from dataclasses import dataclass

from typing import Mapping





@dataclass(frozen=True, slots=True)

class RPCAuditSnapshot:

    total: int

    by_operation: Mapping[str, int]

    by_account: Mapping[int, int]

    duplicate_suspicions: int

    minimum_interval_violations: int





class RPCAudit:

    """Process-local, secret-free Telegram RPC observability.



    It records operation names, account ids, and monotonic timing only. Payloads,

    phone numbers, usernames, tokens, proxy data, and message text are excluded.

    """



    def __init__(self, *, minimum_interval_seconds: float = 1.0) -> None:

        self.minimum_interval_seconds = max(0.0, float(minimum_interval_seconds))

        self._lock = threading.RLock()

        self._by_operation: Counter[str] = Counter()

        self._by_account: Counter[int] = Counter()

        self._last_call: dict[tuple[int, str], float] = {}

        self._duplicate_suspicions = 0

        self._minimum_interval_violations = 0



    def record(

        self,

        operation: str,

        *,

        account_id: int = 0,

        now: float | None = None,

    ) -> None:

        normalized = str(operation or "unknown").strip() or "unknown"

        owner = max(0, int(account_id or 0))

        moment = time.monotonic() if now is None else float(now)

        key = (owner, normalized)



        with self._lock:

            previous = self._last_call.get(key)

            if previous is not None:

                elapsed = moment - previous

                if elapsed < self.minimum_interval_seconds:

                    self._minimum_interval_violations += 1

                if elapsed < 0.1:

                    self._duplicate_suspicions += 1

            self._last_call[key] = moment

            self._by_operation[normalized] += 1

            self._by_account[owner] += 1



    def snapshot(self) -> RPCAuditSnapshot:

        with self._lock:

            return RPCAuditSnapshot(

                total=sum(self._by_operation.values()),

                by_operation=dict(self._by_operation),

                by_account=dict(self._by_account),

                duplicate_suspicions=self._duplicate_suspicions,

                minimum_interval_violations=self._minimum_interval_violations,

            )


from __future__ import annotations



import random

from dataclasses import dataclass

from enum import StrEnum

from typing import Any





class RetryClass(StrEnum):

    RETRYABLE = "retryable"

    DEFERRED = "deferred"

    NON_RETRYABLE = "non_retryable"

    UNCERTAIN = "uncertain"





class RequestKind(StrEnum):

    READ = "read"

    JOIN = "join"

    SEND = "send"

    AUTH = "auth"





@dataclass(frozen=True, slots=True)

class RequestDecision:

    retry_class: RetryClass

    retry_after: float | None = None

    reason: str = ""





@dataclass(frozen=True, slots=True)

class FloodWaitPolicy:

    """Central FloodWait policy shared by JOIN and SEND boundaries."""



    minimum_buffer_seconds: int = 30

    maximum_buffer_seconds: int = 45

    long_pause_minimum_seconds: int = 180

    long_pause_maximum_seconds: int = 300



    def buffered_wait(

        self,

        telegram_seconds: int | float,

        *,

        rng: random.Random | None = None,

    ) -> float:

        source = rng or random

        raw = max(0.0, float(telegram_seconds))

        buffer = source.uniform(
            float(self.minimum_buffer_seconds),
            float(self.maximum_buffer_seconds),
        )
        operator_floor = source.uniform(
            float(self.long_pause_minimum_seconds),
            float(self.long_pause_maximum_seconds),
        )
        return max(raw + buffer, operator_floor)



    def operator_pause(

        self,

        *,

        rng: random.Random | None = None,

    ) -> float:

        source = rng or random

        return source.uniform(

            float(self.long_pause_minimum_seconds),

            float(self.long_pause_maximum_seconds),

        )





class TelegramRequestPolicy:

    """One classification point for Telegram RPC failures.



    The policy is deliberately independent from Telethon imports so it can be

    used by workers, auth, diagnostics, and tests without loading a session.

    """



    def __init__(self, flood_wait: FloodWaitPolicy | None = None) -> None:

        self.flood_wait = flood_wait or FloodWaitPolicy()



    @staticmethod

    def _name(error: BaseException) -> str:

        return type(error).__name__.lower()



    @staticmethod

    def _message(error: BaseException) -> str:

        return str(error or "").strip().lower()



    def classify(

        self,

        error: BaseException,

        *,

        request_kind: RequestKind | str,

        send_started: bool = False,

    ) -> RequestDecision:

        kind = RequestKind(request_kind)

        name = self._name(error)

        message = self._message(error)



        if "floodwait" in name or "flood_wait" in message:

            seconds = getattr(error, "seconds", 0) or 0

            return RequestDecision(

                RetryClass.DEFERRED,

                retry_after=self.flood_wait.buffered_wait(seconds),

                reason="telegram_flood_wait",

            )



        if send_started and kind is RequestKind.SEND and any(

            token in name or token in message

            for token in ("timeout", "connection", "disconnect", "network")

        ):

            return RequestDecision(

                RetryClass.UNCERTAIN,

                reason="remote_send_outcome_unknown",

            )



        if any(

            token in name or token in message

            for token in (

                "forbidden",

                "writeforbidden",

                "chatwriteforbidden",

                "userbanned",

                "authkey",

                "sessionrevoked",

                "unauthorized",

            )

        ):

            return RequestDecision(

                RetryClass.NON_RETRYABLE,

                reason="telegram_permission_or_session_restriction",

            )



        if any(

            token in name or token in message

            for token in ("timeout", "connection", "disconnect", "servererror")

        ):

            return RequestDecision(

                RetryClass.RETRYABLE,

                reason="transient_transport_failure",

            )



        return RequestDecision(

            RetryClass.NON_RETRYABLE,

            reason="unclassified_failure_fail_closed",

        )


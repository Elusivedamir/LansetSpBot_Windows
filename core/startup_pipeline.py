from __future__ import annotations

ARCHITECTURE_STATUS = "experimental"




from dataclasses import dataclass, field

from enum import StrEnum

from typing import Callable





class StartupPhase(StrEnum):

    PROFILE_SECURITY = "profile_security"

    DATABASE = "database"

    ACCOUNT_RECOVERY = "account_recovery"

    SESSION_RECOVERY = "session_recovery"

    SECRET_MIGRATION = "secret_migration"

    RUNTIME_READY = "runtime_ready"





@dataclass(frozen=True, slots=True)

class StartupStepResult:

    phase: StartupPhase

    ok: bool

    message: str = ""





@dataclass(slots=True)

class StartupReport:

    steps: list[StartupStepResult] = field(default_factory=list)



    @property

    def ok(self) -> bool:

        return all(step.ok for step in self.steps)



    @property

    def failed_phase(self) -> StartupPhase | None:

        for step in self.steps:

            if not step.ok:

                return step.phase

        return None





class StartupPipeline:

    """Explicit fail-closed startup phases.



    Production integration can move existing recovery calls from the container

    constructor into these steps without changing their implementations.

    """



    def __init__(self) -> None:

        self._steps: list[tuple[StartupPhase, Callable[[], str | None]]] = []



    def add(

        self,

        phase: StartupPhase,

        action: Callable[[], str | None],

    ) -> "StartupPipeline":

        self._steps.append((phase, action))

        return self



    def run(self) -> StartupReport:

        report = StartupReport()

        for phase, action in self._steps:

            try:

                message = action() or ""

            except Exception as exc:

                report.steps.append(

                    StartupStepResult(

                        phase=phase,

                        ok=False,

                        message=f"{type(exc).__name__}: {exc}",

                    )

                )

                break

            report.steps.append(

                StartupStepResult(phase=phase, ok=True, message=str(message))

            )

        return report


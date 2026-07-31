from __future__ import annotations

ARCHITECTURE_STATUS = "experimental"




from contextlib import AbstractContextManager

from dataclasses import dataclass

from typing import Any, Callable, Protocol, TypeVar



from core.campaign_state import (

    CampaignStatus,

    require_campaign_transition,

)





class ConnectionProvider(Protocol):

    def get_connection(self) -> AbstractContextManager[Any]:

        """Return the existing transactional SQLite context manager."""





class AtomicWorkflowError(RuntimeError):

    """Raised when an atomic workflow precondition is not satisfied."""





T = TypeVar("T")





@dataclass(frozen=True, slots=True)

class CompareAndSetResult:

    changed: bool

    current: CampaignStatus

    target: CampaignStatus





class AtomicWorkflow:

    """Small Unit-of-Work boundary over the existing Database connection API.



    The production repositories already accept nested ``get_connection()``

    scopes. This coordinator keeps related operations inside one outer

    transaction without replacing repository implementations.

    """



    def __init__(self, database: ConnectionProvider) -> None:

        self.database = database



    def run(self, operation: Callable[[Any], T]) -> T:

        """Execute ``operation`` inside one database transaction."""



        with self.database.get_connection() as connection:

            return operation(connection)



    def compare_and_set_campaign(

        self,

        *,

        current: CampaignStatus | str,

        target: CampaignStatus | str,

        update: Callable[[CampaignStatus, CampaignStatus], bool],

        allow_idempotent: bool = True,

    ) -> CompareAndSetResult:

        """Validate a transition and execute a repository compare-and-set.



        ``update`` must perform an SQL update guarded by the expected current

        status and return whether exactly one row changed.

        """



        normalized_current, normalized_target = require_campaign_transition(

            current,

            target,

            allow_idempotent=allow_idempotent,

        )



        changed = bool(update(normalized_current, normalized_target))

        if not changed and normalized_current != normalized_target:

            raise AtomicWorkflowError(

                "Campaign state changed concurrently; compare-and-set updated no rows"

            )

        return CompareAndSetResult(

            changed=changed,

            current=normalized_current,

            target=normalized_target,

        )



    def reserve_and_enqueue(

        self,

        *,

        reserve: Callable[[], bool],

        enqueue: Callable[[], T],

    ) -> T:

        """Reserve a business operation and enqueue its technical task atomically."""



        def operation(_connection: Any) -> T:

            if not reserve():

                raise AtomicWorkflowError("Business operation is already reserved")

            return enqueue()



        return self.run(operation)



    def finalize_success(

        self,

        *,

        persist_remote_result: Callable[[], None],

        mark_succeeded: Callable[[], None],

        consume_quota: Callable[[], None],

    ) -> None:

        """Commit the durable success ledger and quota in one transaction."""



        def operation(_connection: Any) -> None:

            persist_remote_result()

            mark_succeeded()

            consume_quota()



        self.run(operation)


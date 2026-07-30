from __future__ import annotations



from dataclasses import dataclass

from typing import Any, Protocol





class LegacyServiceAPI(Protocol):

    """Structural subset used by typed application-service namespaces."""





@dataclass(frozen=True, slots=True)

class AccountApplicationService:

    api: Any



    def list(self) -> list[dict[str, Any]]:

        return list(self.api.list_telegram_accounts())



    def select(self, account_id: int) -> dict[str, Any]:

        return dict(self.api.select_telegram_account(int(account_id)))



    def stop(self, account_id: int) -> dict[str, Any]:

        return dict(self.api.stop_telegram_account(int(account_id)))





@dataclass(frozen=True, slots=True)

class CommentApplicationService:

    api: Any



    def start(

        self,

        texts: list[str],

        *,

        continuous: bool,

        daily_limit: int,

    ) -> dict[str, Any]:

        return dict(

            self.api.start_comment_campaign(

                list(texts),

                continuous=bool(continuous),

                daily_limit=int(daily_limit),

            )

        )



    def state(self, *, account_id: int | None = None) -> dict[str, Any] | None:

        value = self.api.get_comment_campaign_state(account_id=account_id)

        return None if value is None else dict(value)



    def pause(self, campaign_id: int) -> bool:

        return bool(self.api.pause_comment_campaign(int(campaign_id)))



    def resume(self, campaign_id: int) -> bool:

        return bool(self.api.resume_comment_campaign(int(campaign_id)))



    def stop(self, campaign_id: int) -> bool:

        return bool(self.api.stop_comment_campaign(int(campaign_id)))





@dataclass(frozen=True, slots=True)

class JoinApplicationService:

    api: Any



    def state(self, *, account_id: int | None = None) -> dict[str, Any] | None:

        value = self.api.get_join_campaign_state(account_id=account_id)

        return None if value is None else dict(value)



    def stop(self, campaign_id: int) -> bool:

        return bool(self.api.stop_join_campaign(int(campaign_id)))





@dataclass(frozen=True, slots=True)

class RuntimeApplicationService:

    api: Any



    def start_queue(self) -> bool:

        return bool(self.api.start_queue())



    def pause_queue(self) -> bool:

        return bool(self.api.pause_queue())



    def resume_queue(self) -> bool:

        return bool(self.api.resume_queue())



    def unavailable_reason(self) -> str | None:

        value = self.api.get_queue_unavailable_reason()

        return None if value in (None, "") else str(value)





@dataclass(frozen=True, slots=True)

class ApplicationFacade:

    """Typed namespaces over the backwards-compatible ServiceAPI."""



    accounts: AccountApplicationService

    comments: CommentApplicationService

    joins: JoinApplicationService

    runtime: RuntimeApplicationService



    @classmethod

    def from_legacy_api(cls, api: Any) -> "ApplicationFacade":

        return cls(

            accounts=AccountApplicationService(api),

            comments=CommentApplicationService(api),

            joins=JoinApplicationService(api),

            runtime=RuntimeApplicationService(api),

        )


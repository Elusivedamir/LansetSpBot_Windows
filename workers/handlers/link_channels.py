"""Factory for the resumable channel-link preparation workflow."""

from __future__ import annotations

from typing import Any

from workers.handlers.link_channels_hardened import HardenedLinkChannelsRunner


def create_link_channels_handler(
    *,
    self,
    telegram: Any,
    worker_db: Any,
    linked: Any,
    set_runtime: Any,
    publish_activity: Any,
):
    """Return the account-bound hardened ``link_channels`` queue handler."""

    async def link_channels(task: dict[str, Any]) -> None:
        runner = HardenedLinkChannelsRunner(
            owner=self,
            telegram=telegram,
            worker_db=worker_db,
            linked=linked,
            set_runtime=set_runtime,
            publish_activity=publish_activity,
            task=task,
        )
        await runner.run()

    return link_channels

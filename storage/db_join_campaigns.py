from __future__ import annotations

from storage.join_campaigns import (
    JoinCampaignLifecycleMixin,
    JoinFinalizationMixin,
    JoinGuardRepositoryMixin,
    JoinRecoveryMixin,
    JoinScheduleMixin,
    SavedDialogRepositoryMixin,
)


class JoinCampaignRepositoryMixin(
    SavedDialogRepositoryMixin,
    JoinCampaignLifecycleMixin,
    JoinScheduleMixin,
    JoinFinalizationMixin,
    JoinRecoveryMixin,
    JoinGuardRepositoryMixin,
):
    """Compatibility facade for the split join campaign repositories."""

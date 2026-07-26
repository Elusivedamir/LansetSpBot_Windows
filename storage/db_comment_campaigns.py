from __future__ import annotations

from storage.comment_campaigns import (
    CommentCampaignLifecycleMixin,
    CommentFinalizationMixin,
    CommentHistoryMixin,
    CommentReconciliationMixin,
    CommentScheduleMixin,
)


class CommentCampaignRepositoryMixin(
    CommentHistoryMixin,
    CommentCampaignLifecycleMixin,
    CommentScheduleMixin,
    CommentFinalizationMixin,
    CommentReconciliationMixin,
):
    """Compatibility facade for the split comment campaign repositories."""

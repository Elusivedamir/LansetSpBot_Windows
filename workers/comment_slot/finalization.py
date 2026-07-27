from __future__ import annotations

from typing import Any


def finalize_comment_slot(
    *,
    worker_db: Any,
    task_id: int,
    slot_id: int,
    campaign_id: int,
    channel_id: int,
    post_id: int | None,
    selected: str | None,
    final_status: str,
    final_message: str,
    sent: bool,
    consume_channel: bool,
    campaign_pause_reason: str | None,
    internal_error: Exception | None,
    slot_deferred: bool,
    account_id: int | None = None,
    restriction_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Commit the local slot outcome through the atomic repository path."""

    if slot_deferred:
        worker_db.update_task_progress(task_id, 100)
        return None

    finalizer = getattr(type(worker_db), "finalize_comment_slot_outcome", None)
    if callable(finalizer):
        outcome_kwargs = {
            "status": final_status,
            "result": final_message,
            "channel_id": channel_id,
            "post_id": post_id,
            "selected_text": selected,
            "sent": sent,
            "consume_channel": consume_channel,
            "campaign_pause_reason": campaign_pause_reason,
            "task_failed": internal_error is not None,
            "task_error": (
                f"{type(internal_error).__name__}: {internal_error}"
                if internal_error is not None
                else None
            ),
            "expected_campaign_id": campaign_id,
            "expected_account_id": account_id,
        }
        if restriction_kwargs is not None:
            restricted_finalizer = getattr(
                type(worker_db),
                "finalize_comment_slot_outcome_with_restriction",
                None,
            )
            if callable(restricted_finalizer):
                return dict(
                    restricted_finalizer(
                        worker_db,
                        task_id,
                        slot_id,
                        restriction_kwargs=restriction_kwargs,
                        **outcome_kwargs,
                    )
                    or {}
                )
        finalized = finalizer(worker_db, task_id, slot_id, **outcome_kwargs)
        if finalized is not True:
            raise RuntimeError(
                "Comment slot finalization returned without committing an outcome"
            )
        if restriction_kwargs is not None:
            activator = getattr(worker_db, "activate_account_restriction_atomic", None)
            if not callable(activator):
                raise RuntimeError(
                    "Database does not support account-scoped restrictions"
                )
            return dict(activator(**restriction_kwargs) or {})
        return None

    # Compatibility path for deliberately small repository test doubles.
    worker_db.add_comment_history(
        task_id,
        channel_id,
        post_id,
        selected,
        final_message,
        campaign_id=campaign_id,
        slot_id=slot_id,
    )
    if consume_channel:
        worker_db.mark_channel_comment_checked(channel_id)
    if campaign_pause_reason:
        worker_db.pause_campaign_for_safety(campaign_id, campaign_pause_reason)
    worker_db.finish_comment_slot(
        slot_id,
        status=final_status,
        result=final_message,
        channel_id=channel_id,
        post_id=post_id,
        sent=sent,
        selected_text=selected if sent else None,
    )
    worker_db.update_task_progress(task_id, 100)
    if restriction_kwargs is not None:
        activator = getattr(worker_db, "activate_account_restriction_atomic", None)
        if not callable(activator):
            raise RuntimeError("Database does not support account-scoped restrictions")
        return dict(activator(**restriction_kwargs) or {})
    return None

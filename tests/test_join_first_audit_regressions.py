from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from core.exceptions import NonRetryableTelegramError
from services.comment_service import CommentService


class _Barrier:
    @contextmanager
    def dispatch(self, _request=None):
        yield


class _MembershipDB:
    def __init__(self, results):
        self.results = list(results)
        self.membership_calls = []
        self.reserved = 0
        self.released = 0

    def is_comment_link_membership_confirmed(
        self, channel_id, linked_chat_id, *, account_id=None
    ):
        self.membership_calls.append((channel_id, linked_chat_id, account_id))
        return self.results.pop(0)

    def is_channel_locally_banned(self, peer_id, *, account_id=None):
        return False

    def reserve_comment_delivery(self, *args, **kwargs):
        self.reserved += 1
        return True

    def release_comment_delivery(self, *args, **kwargs):
        self.released += 1


class _Telegram:
    def __init__(self):
        self.mutated = False

    async def send_comment(
        self, channel_id, post_id, text, *, reply_to=None, linked_chat_id=None,
        dispatch_barrier=None,
    ):
        with dispatch_barrier.dispatch(object()):
            self.mutated = True
        return SimpleNamespace(id=44, sender_id=9, date=None)


@pytest.mark.asyncio
async def test_manual_comment_requires_exact_durable_membership_before_reservation():
    db = _MembershipDB([False])
    telegram = _Telegram()
    service = CommentService(telegram=telegram, linked_chat_service=None, db=db)

    with pytest.raises(NonRetryableTelegramError) as error:
        await service.ensure_and_send_comment(
            channel_id=10,
            post_message_id=20,
            linked_chat_id=30,
            reply_to=777,
            text="hello",
            membership_ready=True,
            account_id=77,
            campaign_id=0,
            action_type="manual_comment",
            dispatch_barrier=_Barrier(),
        )

    assert error.value.code == "join_required"
    assert db.membership_calls == [(10, 30, 77)]
    assert db.reserved == 0
    assert telegram.mutated is False


@pytest.mark.asyncio
async def test_membership_is_rechecked_inside_final_dispatch_barrier():
    db = _MembershipDB([True, False])
    telegram = _Telegram()
    service = CommentService(telegram=telegram, linked_chat_service=None, db=db)

    with pytest.raises(NonRetryableTelegramError) as error:
        await service.ensure_and_send_comment(
            channel_id=10,
            post_message_id=20,
            linked_chat_id=30,
            reply_to=777,
            text="hello",
            membership_ready=True,
            account_id=77,
            campaign_id=5,
            action_type="campaign_comment",
            dispatch_barrier=_Barrier(),
        )

    assert error.value.code == "join_required"
    assert db.membership_calls == [(10, 30, 77), (10, 30, 77)]
    assert db.reserved == 1
    assert db.released == 1
    assert telegram.mutated is False

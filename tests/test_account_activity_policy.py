from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tools.account_activity_policy import ActivityLedger, ActivityPolicy


BASE = {
    "account_id": 10,
    "private_dialogs": [
        {"peer": "@alice", "messages": ["Привет"]},
    ],
    "groups": [{"peer": "@group"}],
    "join_targets": ["@target_one", "@target_two"],
}


class ActivityPolicyTests(unittest.TestCase):
    def test_account_id_is_required(self) -> None:
        raw = dict(BASE)
        raw.pop("account_id")
        with self.assertRaises(ValueError):
            ActivityPolicy.from_mapping(raw)

    def test_join_limit_is_bounded_to_requested_range(self) -> None:
        with self.assertRaises(ValueError):
            ActivityPolicy.from_mapping({**BASE, "weekly_join_limit": 6})
        with self.assertRaises(ValueError):
            ActivityPolicy.from_mapping({**BASE, "weekly_join_limit": 21})
        self.assertEqual(
            ActivityPolicy.from_mapping({**BASE, "weekly_join_limit": 20}).weekly_join_limit,
            20,
        )

    def test_messages_require_operator_text(self) -> None:
        with self.assertRaises(ValueError):
            ActivityPolicy.from_mapping(
                {**BASE, "private_dialogs": [{"peer": "@alice", "messages": []}]}
            )

    def test_per_run_mutation_limits_are_conservative(self) -> None:
        with self.assertRaises(ValueError):
            ActivityPolicy.from_mapping({**BASE, "send_messages_per_run": 3})
        with self.assertRaises(ValueError):
            ActivityPolicy.from_mapping({**BASE, "max_reactions_per_run": 4})
        with self.assertRaises(ValueError):
            ActivityPolicy.from_mapping({**BASE, "max_joins_per_run": 4})


    def test_reaction_probability_rejects_non_finite_numbers(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite"):
                    ActivityPolicy.from_mapping(
                        {**BASE, "reaction_probability": value}
                    )

    def test_join_targets_are_canonical_and_post_links_are_rejected(self) -> None:
        policy = ActivityPolicy.from_mapping(
            {
                **BASE,
                "join_targets": [
                    "https://telegram.me/valid_group",
                    "t.me/+Abcdefgh1234",
                ],
            }
        )
        self.assertEqual(
            policy.join_targets,
            ("@valid_group", "https://t.me/+Abcdefgh1234"),
        )
        with self.assertRaisesRegex(ValueError, "post/path"):
            ActivityPolicy.from_mapping(
                {**BASE, "join_targets": ["https://t.me/valid_group/123"]}
            )

    def test_weekly_join_budget_uses_rolling_seven_days(self) -> None:
        policy = ActivityPolicy.from_mapping({**BASE, "weekly_join_limit": 7})
        now = datetime(2026, 8, 6, 10, tzinfo=timezone.utc)
        ledger = ActivityLedger()
        for index in range(6):
            ledger.record_join(f"@target{index}", now - timedelta(days=index))
        ledger.record_join("@old", now - timedelta(days=8))
        self.assertEqual(ledger.weekly_join_count(now), 6)
        self.assertEqual(ledger.weekly_join_remaining(policy, now), 1)

    def test_minimum_join_interval_is_enforced(self) -> None:
        policy = ActivityPolicy.from_mapping({**BASE, "min_hours_between_joins": 8})
        now = datetime(2026, 8, 6, 10, tzinfo=timezone.utc)
        ledger = ActivityLedger()
        ledger.record_join("@target_one", now)
        self.assertFalse(ledger.can_join_now(policy, now + timedelta(hours=7, minutes=59)))
        self.assertTrue(ledger.can_join_now(policy, now + timedelta(hours=8)))

    def test_target_join_cooldown_is_enforced(self) -> None:
        policy = ActivityPolicy.from_mapping({**BASE, "join_target_cooldown_days": 30})
        now = datetime(2026, 8, 6, 10, tzinfo=timezone.utc)
        ledger = ActivityLedger()
        ledger.record_join_attempt("@target_one", now)
        self.assertFalse(ledger.can_join_target("@target_one", policy, now + timedelta(days=29)))
        self.assertTrue(ledger.can_join_target("@target_one", policy, now + timedelta(days=30)))

    def test_message_cooldown_is_per_peer(self) -> None:
        policy = ActivityPolicy.from_mapping({**BASE, "message_cooldown_hours": 24})
        now = datetime(2026, 8, 6, 10, tzinfo=timezone.utc)
        ledger = ActivityLedger()
        ledger.record_message("@alice", now)
        self.assertFalse(ledger.message_due("@alice", policy, now + timedelta(hours=23)))
        self.assertTrue(ledger.message_due("@alice", policy, now + timedelta(hours=24)))
        self.assertTrue(ledger.message_due("@bob", policy, now))

    def test_reaction_is_deduplicated_by_message(self) -> None:
        policy = ActivityPolicy.from_mapping(BASE)
        now = datetime(2026, 8, 6, 10, tzinfo=timezone.utc)
        ledger = ActivityLedger()
        ledger.record_reaction("@group", 101, now)
        self.assertFalse(ledger.reaction_due("@group", 101, policy, now + timedelta(hours=1)))
        self.assertTrue(ledger.reaction_due("@group", 102, policy, now + timedelta(hours=1)))

    def test_ledger_json_round_trip(self) -> None:
        now = datetime(2026, 8, 6, 10, tzinfo=timezone.utc)
        ledger = ActivityLedger()
        ledger.record_join("@target_one", now)
        ledger.record_message("@alice", now)
        ledger.record_reaction("@group", 101, now)
        restored = ActivityLedger.from_json(ledger.to_json())
        self.assertEqual(restored.to_mapping(), ledger.to_mapping())


if __name__ == "__main__":
    unittest.main()

# Added by the current-main release audit.
def test_numeric_join_target_is_rejected_without_access_hash() -> None:
    import pytest

    with pytest.raises(ValueError, match="numeric id"):
        ActivityPolicy.from_mapping({**BASE, "join_targets": [123456789]})


def test_strict_ledger_rejects_corrupt_or_partial_state() -> None:
    import pytest

    with pytest.raises(ValueError, match="corrupted"):
        ActivityLedger.from_json("{broken", strict=True)
    with pytest.raises(ValueError, match="message_events"):
        ActivityLedger.from_json(
            '{"version":1,"message_events":{"peer":"not-a-date"}}',
            strict=True,
        )
    # Historical/non-executing callers may still request tolerant recovery.
    assert ActivityLedger.from_json("{broken").to_mapping() == ActivityLedger().to_mapping()


def test_strict_ledger_rejects_boolean_version() -> None:
    import pytest

    with pytest.raises(ValueError, match="version"):
        ActivityLedger.from_json('{"version":true}', strict=True)


def test_allow_reactions_requires_real_json_boolean() -> None:
    import pytest

    with pytest.raises(ValueError, match="JSON boolean"):
        ActivityPolicy.from_mapping(
            {**BASE, "groups": [{"peer": "@group", "allow_reactions": "false"}]}
        )
    policy = ActivityPolicy.from_mapping(
        {**BASE, "groups": [{"peer": "@group", "allow_reactions": False}]}
    )
    assert policy.groups[0].allow_reactions is False


def test_integer_fields_reject_json_booleans() -> None:
    import pytest

    with pytest.raises(ValueError, match="integer"):
        ActivityPolicy.from_mapping({**BASE, "account_id": True})
    with pytest.raises(ValueError, match="integer"):
        ActivityPolicy.from_mapping({**BASE, "max_joins_per_run": False})


def test_operator_messages_and_emojis_require_strings() -> None:
    import pytest

    with pytest.raises(ValueError, match="must be a string"):
        ActivityPolicy.from_mapping(
            {**BASE, "private_dialogs": [{"peer": "@alice", "messages": [123]}]}
        )
    with pytest.raises(ValueError, match="must be a string"):
        ActivityPolicy.from_mapping({**BASE, "reaction_emojis": [123]})


def test_reserved_join_consumes_weekly_and_interval_budgets() -> None:
    policy = ActivityPolicy.from_mapping({**BASE, "weekly_join_limit": 7})
    now = datetime(2026, 8, 6, 10, tzinfo=timezone.utc)
    ledger = ActivityLedger()
    ledger.record_join_attempt("@target_one", now)
    assert ledger.weekly_join_count(now) == 1
    assert not ledger.can_join_now(policy, now + timedelta(hours=7, minutes=59))
    assert not ledger.can_join_target(
        "@target_one", policy, now + timedelta(days=29)
    )

from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from core.warmup_scenarios import SCENARIOS


@dataclass(frozen=True, slots=True)
class WarmupProfile:
    seed: str
    day_order: tuple[str, ...]
    dialogue_windows: int
    reply_min_seconds: int
    reply_max_seconds: int
    typing_min_seconds: int
    typing_max_seconds: int
    group_visits_per_day: int
    posts_min: int
    posts_max: int
    reaction_probability_percent: int
    private_reaction_probability_percent: int
    active_start_hour: int
    active_end_hour: int

    def to_record(self) -> dict[str, object]:
        result = asdict(self)
        result["day_order"] = ",".join(self.day_order)
        return result


@dataclass(frozen=True, slots=True)
class PlannedWarmupStep:
    sequence_no: int
    day_number: int
    scenario_key: str
    action: str
    actor_account_id: int
    target_account_id: int | None
    message_text: str | None
    scheduled_at: str
    typing_seconds: int
    reply_to_previous: bool
    posts_to_read: int
    should_react: bool

    def to_record(self) -> dict[str, object]:
        return asdict(self)


def _rng(seed: str, *parts: object) -> random.Random:
    digest = hashlib.sha256(
        "|".join([str(seed), *(str(part) for part in parts)]).encode("utf-8")
    ).digest()
    return random.Random(int.from_bytes(digest[:16], "big"))


def generate_profile(seed: str) -> WarmupProfile:
    clean_seed = str(seed or "").strip()
    if len(clean_seed) < 8:
        raise ValueError("Warmup profile seed is too short")
    generator = _rng(clean_seed, "profile")
    order = [scenario.key for scenario in SCENARIOS]
    generator.shuffle(order)
    reply_min = generator.randint(120, 300)
    reply_max = generator.randint(max(reply_min + 180, 480), 900)
    typing_min = generator.randint(3, 5)
    typing_max = generator.randint(max(typing_min + 2, 7), 12)
    return WarmupProfile(
        seed=clean_seed,
        day_order=tuple(order),
        dialogue_windows=generator.randint(3, 4),
        reply_min_seconds=reply_min,
        reply_max_seconds=reply_max,
        typing_min_seconds=typing_min,
        typing_max_seconds=typing_max,
        group_visits_per_day=generator.randint(1, 2),
        posts_min=generator.randint(2, 3),
        posts_max=generator.randint(3, 4),
        reaction_probability_percent=generator.randint(35, 70),
        private_reaction_probability_percent=generator.randint(20, 55),
        active_start_hour=generator.randint(9, 11),
        active_end_hour=generator.randint(21, 23),
    )


def _to_db_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _split_counts(total: int, buckets: int) -> list[int]:
    base, remainder = divmod(total, buckets)
    return [base + (1 if index < remainder else 0) for index in range(buckets)]


def _step_values(
    *,
    day_number: int,
    scenario_key: str,
    action: str,
    actor_account_id: int,
    target_account_id: int | None,
    message_text: str | None = None,
    typing_seconds: int = 0,
    reply_to_previous: bool = False,
    posts_to_read: int = 0,
    should_react: bool = False,
) -> dict[str, Any]:
    return {
        "day_number": int(day_number),
        "scenario_key": str(scenario_key),
        "action": str(action),
        "actor_account_id": int(actor_account_id),
        "target_account_id": (
            int(target_account_id) if target_account_id is not None else None
        ),
        "message_text": message_text,
        "typing_seconds": int(typing_seconds),
        "reply_to_previous": bool(reply_to_previous),
        "posts_to_read": int(posts_to_read),
        "should_react": bool(should_react),
    }


def build_week_plan(
    *,
    account_a_id: int,
    account_b_id: int,
    week_number: int,
    profile: WarmupProfile,
    start_at: datetime | None = None,
) -> list[PlannedWarmupStep]:
    account_a = int(account_a_id)
    account_b = int(account_b_id)
    if account_a <= 0 or account_b <= 0 or account_a == account_b:
        raise ValueError("Warmup pair requires two different positive account ids")
    week = max(1, int(week_number))
    local_now = (start_at or datetime.now().astimezone()).astimezone()
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=timezone.utc).astimezone()

    scenarios = {scenario.key: scenario for scenario in SCENARIOS}
    pending: list[tuple[datetime, dict[str, Any]]] = []

    # Contact import happens only during the first week. The peer phone is never
    # persisted in this plan; the worker reads it from the protected account
    # secret store immediately before ImportContactsRequest.
    if week == 1:
        pending.extend(
            [
                (
                    local_now + timedelta(seconds=30),
                    _step_values(
                        day_number=1,
                        scenario_key="contacts",
                        action="ensure_contact",
                        actor_account_id=account_a,
                        target_account_id=account_b,
                    ),
                ),
                (
                    local_now + timedelta(seconds=75),
                    _step_values(
                        day_number=1,
                        scenario_key="contacts",
                        action="ensure_contact",
                        actor_account_id=account_b,
                        target_account_id=account_a,
                    ),
                ),
            ]
        )

    for day_index, scenario_key in enumerate(profile.day_order, start=1):
        scenario = scenarios[scenario_key]
        generator = _rng(profile.seed, "week", week, "day", day_index)
        day_date = (local_now + timedelta(days=day_index - 1)).date()
        nominal_start = datetime.combine(
            day_date,
            datetime.min.time(),
            tzinfo=local_now.tzinfo,
        ) + timedelta(
            hours=profile.active_start_hour,
            minutes=generator.randint(0, 55),
        )
        if day_index == 1 and nominal_start <= local_now:
            nominal_start = local_now + timedelta(minutes=2)
        available_seconds = max(
            4 * 60 * 60,
            int(
                (
                    datetime.combine(
                        day_date,
                        datetime.min.time(),
                        tzinfo=local_now.tzinfo,
                    )
                    + timedelta(hours=profile.active_end_hour)
                    - nominal_start
                ).total_seconds()
            ),
        )
        window_count = profile.dialogue_windows
        window_starts = [
            nominal_start
            + timedelta(
                seconds=(available_seconds * index) // max(1, window_count),
                minutes=generator.randint(0, 25),
            )
            for index in range(window_count)
        ]
        counts = _split_counts(len(scenario.lines), window_count)
        starts_with_a = bool(generator.getrandbits(1))
        line_index = 0
        previous_message_exists = False
        private_reaction_used = False

        for window_index, count in enumerate(counts):
            cursor = window_starts[window_index]
            for _ in range(count):
                actor_is_a = starts_with_a if line_index % 2 == 0 else not starts_with_a
                actor = account_a if actor_is_a else account_b
                target = account_b if actor_is_a else account_a
                if line_index > 0:
                    cursor += timedelta(
                        seconds=generator.randint(
                            profile.reply_min_seconds,
                            profile.reply_max_seconds,
                        )
                    )
                pending.append(
                    (
                        cursor,
                        _step_values(
                            day_number=day_index,
                            scenario_key=scenario_key,
                            action="message",
                            actor_account_id=actor,
                            target_account_id=target,
                            message_text=scenario.lines[line_index],
                            typing_seconds=generator.randint(
                                profile.typing_min_seconds,
                                profile.typing_max_seconds,
                            ),
                            reply_to_previous=previous_message_exists,
                        ),
                    )
                )
                # At most one private reaction per day. It is scheduled before
                # the next possible reply (minimum reply gap is two minutes), so
                # pair.last_message_id still points to the intended message.
                if (
                    not private_reaction_used
                    and line_index >= 1
                    and generator.randint(1, 100)
                    <= profile.private_reaction_probability_percent
                ):
                    pending.append(
                        (
                            cursor + timedelta(seconds=generator.randint(25, 75)),
                            _step_values(
                                day_number=day_index,
                                scenario_key=scenario_key,
                                action="private_reaction",
                                actor_account_id=target,
                                target_account_id=actor,
                                should_react=True,
                            ),
                        )
                    )
                    private_reaction_used = True
                previous_message_exists = True
                line_index += 1

        for visit_index in range(profile.group_visits_per_day):
            fraction = (visit_index + 1) / (profile.group_visits_per_day + 1)
            visit_at = nominal_start + timedelta(
                seconds=int(available_seconds * fraction),
                minutes=generator.randint(-20, 20),
            )
            actor = account_a if (day_index + visit_index) % 2 == 0 else account_b
            pending.append(
                (
                    visit_at,
                    _step_values(
                        day_number=day_index,
                        scenario_key=scenario_key,
                        action="group_visit",
                        actor_account_id=actor,
                        target_account_id=None,
                        posts_to_read=generator.randint(
                            profile.posts_min,
                            profile.posts_max,
                        ),
                        should_react=(
                            generator.randint(1, 100)
                            <= profile.reaction_probability_percent
                        ),
                    ),
                )
            )

    action_priority = {
        "ensure_contact": 0,
        "message": 1,
        "private_reaction": 2,
        "group_visit": 3,
    }
    pending.sort(
        key=lambda item: (
            item[0],
            action_priority.get(str(item[1]["action"]), 99),
        )
    )
    result: list[PlannedWarmupStep] = []
    for index, (moment, values) in enumerate(pending, start=1):
        target_value = values.get("target_account_id")
        result.append(
            PlannedWarmupStep(
                sequence_no=index,
                day_number=int(values["day_number"]),
                scenario_key=str(values["scenario_key"]),
                action=str(values["action"]),
                actor_account_id=int(values["actor_account_id"]),
                target_account_id=(
                    int(target_value) if target_value is not None else None
                ),
                message_text=(
                    str(values["message_text"])
                    if values.get("message_text") is not None
                    else None
                ),
                scheduled_at=_to_db_time(moment),
                typing_seconds=int(values.get("typing_seconds") or 0),
                reply_to_previous=bool(values.get("reply_to_previous")),
                posts_to_read=int(values.get("posts_to_read") or 0),
                should_react=bool(values.get("should_react")),
            )
        )
    return result


def day_order_titles(profile: WarmupProfile) -> tuple[str, ...]:
    titles = {scenario.key: scenario.title for scenario in SCENARIOS}
    return tuple(titles[key] for key in profile.day_order)


def validate_plan(steps: Iterable[PlannedWarmupStep]) -> None:
    values = list(steps)
    if not values:
        raise ValueError("Warmup plan is empty")
    expected = list(range(1, len(values) + 1))
    actual = [step.sequence_no for step in values]
    if actual != expected:
        raise ValueError("Warmup plan sequence is not contiguous")
    if sorted({step.day_number for step in values}) != list(range(1, 8)):
        raise ValueError("Warmup plan must contain exactly seven days")
    allowed = {"ensure_contact", "message", "private_reaction", "group_visit"}
    if any(step.action not in allowed for step in values):
        raise ValueError("Warmup plan contains an unsupported action")
    for step in values:
        if step.action in {"ensure_contact", "message", "private_reaction"}:
            if step.target_account_id is None:
                raise ValueError(f"{step.action} requires target_account_id")
        if step.action == "message" and not str(step.message_text or "").strip():
            raise ValueError("Message step has empty text")

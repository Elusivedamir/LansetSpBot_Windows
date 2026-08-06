from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, TypeVar
from urllib.parse import urlparse

LEDGER_SETTING_KEY = "automation.account_activity.ledger.v1"
LEDGER_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def target_key(value: str | int) -> str:
    normalized = str(value).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _strict_bool(value: Any, *, name: str, default: bool) -> bool:
    if value is None:
        return bool(default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a JSON boolean")
    return value


def _bounded_int(
    value: Any,
    *,
    name: str,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(default if value is None else value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _bounded_float(
    value: Any,
    *,
    name: str,
    minimum: float,
    maximum: float,
    default: float,
) -> float:
    try:
        result = float(default if value is None else value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    if result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_INVITE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def normalize_join_target(value: Any, *, name: str) -> str:
    """Return one canonical public username/invite target.

    Post links and arbitrary Telegram URL paths are rejected so the runner never
    turns a link such as ``t.me/channel/123`` into an invalid username.
    """

    target = normalize_peer(value, name=name)
    if isinstance(target, int):
        raise ValueError(
            f"{name} must be a public @username or Telegram invite link; "
            "a numeric id cannot be resolved safely before joining"
        )
    text = str(target).strip()
    if text.startswith("@"):
        username = text[1:]
        if not _USERNAME_RE.fullmatch(username):
            raise ValueError(f"{name} contains an invalid Telegram username")
        return f"@{username}"

    candidate = text if "://" in text else f"https://{text}"
    parsed = urlparse(candidate)
    host = str(parsed.hostname or "").lower()
    if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        raise ValueError(f"{name} must use t.me or telegram.me")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 1 and parts[0].startswith("+"):
        invite_hash = parts[0][1:]
        if not _INVITE_RE.fullmatch(invite_hash):
            raise ValueError(f"{name} contains an invalid invite hash")
        return f"https://t.me/+{invite_hash}"
    if len(parts) == 2 and parts[0].lower() == "joinchat":
        invite_hash = parts[1]
        if not _INVITE_RE.fullmatch(invite_hash):
            raise ValueError(f"{name} contains an invalid invite hash")
        return f"https://t.me/+{invite_hash}"
    if len(parts) == 1 and _USERNAME_RE.fullmatch(parts[0]):
        return f"@{parts[0]}"
    raise ValueError(
        f"{name} must be a public @username or a Telegram invite link, not a post/path URL"
    )


def normalize_peer(value: Any, *, name: str) -> str | int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a Telegram peer id or username")
    if isinstance(value, int):
        if value == 0:
            raise ValueError(f"{name} cannot be zero")
        return value
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} cannot be empty")
    if len(text) > 256:
        raise ValueError(f"{name} is too long")
    return text


_PeerT = TypeVar("_PeerT", str, int)


def _unique(values: Iterable[_PeerT]) -> tuple[_PeerT, ...]:
    seen: set[str] = set()
    result: list[_PeerT] = []
    for value in values:
        key = str(value).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class PrivateDialogRule:
    peer: str | int
    messages: tuple[str, ...]
    allow_reactions: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], index: int) -> "PrivateDialogRule":
        peer = normalize_peer(raw.get("peer"), name=f"private_dialogs[{index}].peer")
        values = raw.get("messages") or []
        if not isinstance(values, list):
            raise ValueError(f"private_dialogs[{index}].messages must be a list")
        messages: list[str] = []
        for message_index, value in enumerate(values):
            if not isinstance(value, str):
                raise ValueError(
                    f"private_dialogs[{index}].messages[{message_index}] must be a string"
                )
            text = value.strip()
            if not text:
                continue
            if len(text) > 1024:
                raise ValueError(
                    f"private_dialogs[{index}].messages[{message_index}] exceeds 1024 characters"
                )
            messages.append(text)
        if not messages:
            raise ValueError(
                f"private_dialogs[{index}] requires at least one non-empty operator message"
            )
        return cls(
            peer=peer,
            messages=tuple(dict.fromkeys(messages)),
            allow_reactions=_strict_bool(
                raw.get("allow_reactions"),
                name=f"private_dialogs[{index}].allow_reactions",
                default=True,
            ),
        )


@dataclass(frozen=True, slots=True)
class GroupRule:
    peer: str | int
    allow_reactions: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], index: int) -> "GroupRule":
        return cls(
            peer=normalize_peer(raw.get("peer"), name=f"groups[{index}].peer"),
            allow_reactions=_strict_bool(
                raw.get("allow_reactions"),
                name=f"groups[{index}].allow_reactions",
                default=True,
            ),
        )


@dataclass(frozen=True, slots=True)
class ActivityPolicy:
    account_id: int
    private_dialogs: tuple[PrivateDialogRule, ...]
    groups: tuple[GroupRule, ...]
    join_targets: tuple[str, ...]
    weekly_join_limit: int = 7
    max_joins_per_run: int = 1
    send_messages_per_run: int = 1
    max_group_reads_per_run: int = 3
    read_messages_per_group: int = 5
    max_reactions_per_run: int = 2
    reaction_probability: float = 0.25
    reaction_emojis: tuple[str, ...] = ("👍", "❤️", "🔥")
    message_cooldown_hours: int = 24
    reaction_cooldown_hours: int = 24
    join_target_cooldown_days: int = 30
    min_hours_between_joins: int = 8
    action_pause_min_seconds: int = 20
    action_pause_max_seconds: int = 90
    max_dialog_scan: int = 200

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ActivityPolicy":
        if not isinstance(raw, Mapping):
            raise ValueError("Configuration root must be an object")
        account_id = _bounded_int(
            raw.get("account_id"),
            name="account_id",
            minimum=1,
            maximum=9_223_372_036_854_775_807,
            default=0,
        )

        private_raw = raw.get("private_dialogs") or []
        if not isinstance(private_raw, list):
            raise ValueError("private_dialogs must be a list")
        if len(private_raw) > 10:
            raise ValueError("private_dialogs supports at most 10 explicit targets")
        private_dialogs = tuple(
            PrivateDialogRule.from_mapping(item, index)
            for index, item in enumerate(private_raw)
            if isinstance(item, Mapping)
        )
        if len(private_dialogs) != len(private_raw):
            raise ValueError("Every private_dialogs item must be an object")

        group_raw = raw.get("groups") or []
        if not isinstance(group_raw, list):
            raise ValueError("groups must be a list")
        if len(group_raw) > 50:
            raise ValueError("groups supports at most 50 explicit targets")
        groups = tuple(
            GroupRule.from_mapping(item, index)
            for index, item in enumerate(group_raw)
            if isinstance(item, Mapping)
        )
        if len(groups) != len(group_raw):
            raise ValueError("Every groups item must be an object")

        join_raw = raw.get("join_targets") or []
        if not isinstance(join_raw, list):
            raise ValueError("join_targets must be a list")
        if len(join_raw) > 100:
            raise ValueError("join_targets supports at most 100 explicit targets")
        join_targets = _unique(
            normalize_join_target(value, name=f"join_targets[{index}]")
            for index, value in enumerate(join_raw)
        )

        emojis_raw = raw.get("reaction_emojis", ["👍", "❤️", "🔥"])
        if not isinstance(emojis_raw, list):
            raise ValueError("reaction_emojis must be a list")
        emojis: list[str] = []
        for index, value in enumerate(emojis_raw):
            if not isinstance(value, str):
                raise ValueError(f"reaction_emojis[{index}] must be a string")
            text = value.strip()
            if not text or len(text) > 16:
                raise ValueError(f"reaction_emojis[{index}] is invalid")
            emojis.append(text)
        if not emojis:
            raise ValueError("reaction_emojis cannot be empty")

        minimum_pause = _bounded_int(
            raw.get("action_pause_min_seconds"),
            name="action_pause_min_seconds",
            minimum=15,
            maximum=600,
            default=20,
        )
        maximum_pause = _bounded_int(
            raw.get("action_pause_max_seconds"),
            name="action_pause_max_seconds",
            minimum=minimum_pause,
            maximum=1800,
            default=max(90, minimum_pause),
        )

        return cls(
            account_id=account_id,
            private_dialogs=private_dialogs,
            groups=groups,
            join_targets=join_targets,
            weekly_join_limit=_bounded_int(
                raw.get("weekly_join_limit"),
                name="weekly_join_limit",
                minimum=7,
                maximum=20,
                default=7,
            ),
            max_joins_per_run=_bounded_int(
                raw.get("max_joins_per_run"),
                name="max_joins_per_run",
                minimum=0,
                maximum=3,
                default=1,
            ),
            send_messages_per_run=_bounded_int(
                raw.get("send_messages_per_run"),
                name="send_messages_per_run",
                minimum=0,
                maximum=2,
                default=1,
            ),
            max_group_reads_per_run=_bounded_int(
                raw.get("max_group_reads_per_run"),
                name="max_group_reads_per_run",
                minimum=0,
                maximum=5,
                default=3,
            ),
            read_messages_per_group=_bounded_int(
                raw.get("read_messages_per_group"),
                name="read_messages_per_group",
                minimum=1,
                maximum=20,
                default=5,
            ),
            max_reactions_per_run=_bounded_int(
                raw.get("max_reactions_per_run"),
                name="max_reactions_per_run",
                minimum=0,
                maximum=3,
                default=2,
            ),
            reaction_probability=_bounded_float(
                raw.get("reaction_probability"),
                name="reaction_probability",
                minimum=0.0,
                maximum=0.5,
                default=0.25,
            ),
            reaction_emojis=tuple(dict.fromkeys(emojis)),
            message_cooldown_hours=_bounded_int(
                raw.get("message_cooldown_hours"),
                name="message_cooldown_hours",
                minimum=12,
                maximum=168,
                default=24,
            ),
            reaction_cooldown_hours=_bounded_int(
                raw.get("reaction_cooldown_hours"),
                name="reaction_cooldown_hours",
                minimum=6,
                maximum=168,
                default=24,
            ),
            join_target_cooldown_days=_bounded_int(
                raw.get("join_target_cooldown_days"),
                name="join_target_cooldown_days",
                minimum=7,
                maximum=180,
                default=30,
            ),
            min_hours_between_joins=_bounded_int(
                raw.get("min_hours_between_joins"),
                name="min_hours_between_joins",
                minimum=4,
                maximum=48,
                default=8,
            ),
            action_pause_min_seconds=minimum_pause,
            action_pause_max_seconds=maximum_pause,
            max_dialog_scan=_bounded_int(
                raw.get("max_dialog_scan"),
                name="max_dialog_scan",
                minimum=50,
                maximum=500,
                default=200,
            ),
        )

    @classmethod
    def from_json(cls, text: str) -> "ActivityPolicy":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        return cls.from_mapping(raw)


@dataclass(slots=True)
class ActivityLedger:
    join_events: list[dict[str, str]] = field(default_factory=list)
    message_events: dict[str, str] = field(default_factory=dict)
    reaction_events: dict[str, str] = field(default_factory=dict)
    join_target_events: dict[str, str] = field(default_factory=dict)
    last_join_at: str = ""

    @classmethod
    def from_mapping(
        cls, raw: Any, *, strict: bool = False
    ) -> "ActivityLedger":
        if not isinstance(raw, Mapping):
            if strict:
                raise ValueError("Activity ledger root must be a JSON object")
            return cls()
        version = raw.get("version", LEDGER_VERSION)
        version_valid = (
            isinstance(version, int)
            and not isinstance(version, bool)
            and version == LEDGER_VERSION
        )
        if strict and not version_valid:
            raise ValueError(
                f"Unsupported activity ledger version: {version!r}"
            )
        events = raw.get("join_events")
        join_events: list[dict[str, str]] = []
        if events is not None and not isinstance(events, list):
            if strict:
                raise ValueError("Activity ledger join_events must be a list")
            events = []
        if isinstance(events, list):
            for index, item in enumerate(events):
                if not isinstance(item, Mapping):
                    if strict:
                        raise ValueError(
                            f"Activity ledger join_events[{index}] must be an object"
                        )
                    continue
                at = str(item.get("at") or "")
                target = str(item.get("target") or "")
                if not target or parse_utc(at) is None:
                    if strict:
                        raise ValueError(
                            f"Activity ledger join_events[{index}] is invalid"
                        )
                    continue
                join_events.append({"at": at, "target": target})

        def clean_mapping(value: Any, *, field_name: str) -> dict[str, str]:
            if value is None:
                return {}
            if not isinstance(value, Mapping):
                if strict:
                    raise ValueError(
                        f"Activity ledger {field_name} must be an object"
                    )
                return {}
            result: dict[str, str] = {}
            for key, timestamp in value.items():
                normalized_key = str(key).strip()
                if not normalized_key or parse_utc(timestamp) is None:
                    if strict:
                        raise ValueError(
                            f"Activity ledger {field_name} contains an invalid entry"
                        )
                    continue
                result[normalized_key] = str(timestamp)
            return result

        return cls(
            join_events=join_events,
            message_events=clean_mapping(
                raw.get("message_events"), field_name="message_events"
            ),
            reaction_events=clean_mapping(
                raw.get("reaction_events"), field_name="reaction_events"
            ),
            join_target_events=clean_mapping(
                raw.get("join_target_events"), field_name="join_target_events"
            ),
            last_join_at=cls._clean_last_join_at(
                raw.get("last_join_at"), strict=strict
            ),
        )

    @staticmethod
    def _clean_last_join_at(value: Any, *, strict: bool) -> str:
        text = str(value or "")
        if not text:
            return ""
        if parse_utc(text) is None:
            if strict:
                raise ValueError("Activity ledger last_join_at is invalid")
            return ""
        return text

    @classmethod
    def from_json(
        cls, text: Any, *, strict: bool = False
    ) -> "ActivityLedger":
        try:
            raw = json.loads(str(text or "{}"))
        except json.JSONDecodeError as exc:
            if strict:
                raise ValueError(
                    "Activity ledger is corrupted; automatic actions are blocked"
                ) from exc
            return cls()
        return cls.from_mapping(raw, strict=strict)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": LEDGER_VERSION,
            "join_events": list(self.join_events),
            "message_events": dict(self.message_events),
            "reaction_events": dict(self.reaction_events),
            "join_target_events": dict(self.join_target_events),
            "last_join_at": self.last_join_at,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def prune(self, now: datetime) -> None:
        now = now.astimezone(timezone.utc)
        join_cutoff = now - timedelta(days=8)
        event_cutoff = now - timedelta(days=190)
        self.join_events = [
            item
            for item in self.join_events
            if (parse_utc(item.get("at")) or datetime.min.replace(tzinfo=timezone.utc))
            >= join_cutoff
        ]

        def prune_mapping(values: dict[str, str]) -> dict[str, str]:
            return {
                key: stamp
                for key, stamp in values.items()
                if (parse_utc(stamp) or datetime.min.replace(tzinfo=timezone.utc))
                >= event_cutoff
            }

        self.message_events = prune_mapping(self.message_events)
        self.reaction_events = prune_mapping(self.reaction_events)
        self.join_target_events = prune_mapping(self.join_target_events)

    def weekly_join_count(self, now: datetime) -> int:
        cutoff = now.astimezone(timezone.utc) - timedelta(days=7)
        return sum(
            1
            for item in self.join_events
            if (parse_utc(item.get("at")) or datetime.min.replace(tzinfo=timezone.utc))
            >= cutoff
        )

    def weekly_join_remaining(self, policy: ActivityPolicy, now: datetime) -> int:
        return max(0, policy.weekly_join_limit - self.weekly_join_count(now))

    def can_join_now(self, policy: ActivityPolicy, now: datetime) -> bool:
        if self.weekly_join_remaining(policy, now) <= 0:
            return False
        last = parse_utc(self.last_join_at)
        if last is None:
            return True
        return now.astimezone(timezone.utc) - last >= timedelta(
            hours=policy.min_hours_between_joins
        )

    def can_join_target(
        self, target: str | int, policy: ActivityPolicy, now: datetime
    ) -> bool:
        last = parse_utc(self.join_target_events.get(target_key(target)))
        if last is None:
            return True
        return now.astimezone(timezone.utc) - last >= timedelta(
            days=policy.join_target_cooldown_days
        )

    def record_join(self, target: str | int, now: datetime) -> None:
        stamp = iso_utc(now)
        key = target_key(target)
        self.join_events.append({"at": stamp, "target": key})
        self.join_target_events[key] = stamp
        self.last_join_at = stamp

    def record_join_attempt(self, target: str | int, now: datetime) -> None:
        """Reserve one JOIN before dispatch and consume its safety budgets.

        A timeout or process crash after dispatch can leave Telegram's result
        unknown. Counting the attempt immediately prevents a different target
        from bypassing the rolling weekly cap or minimum join interval.
        """

        stamp = iso_utc(now)
        key = target_key(target)
        self.join_events.append({"at": stamp, "target": key})
        self.join_target_events[key] = stamp
        self.last_join_at = stamp

    def message_due(
        self, peer: str | int, policy: ActivityPolicy, now: datetime
    ) -> bool:
        last = parse_utc(self.message_events.get(target_key(peer)))
        if last is None:
            return True
        return now.astimezone(timezone.utc) - last >= timedelta(
            hours=policy.message_cooldown_hours
        )

    def record_message(self, peer: str | int, now: datetime) -> None:
        self.message_events[target_key(peer)] = iso_utc(now)

    def reaction_due(
        self,
        peer: str | int,
        message_id: int,
        policy: ActivityPolicy,
        now: datetime,
    ) -> bool:
        key = f"{target_key(peer)}:{int(message_id)}"
        last = parse_utc(self.reaction_events.get(key))
        if last is None:
            return True
        return now.astimezone(timezone.utc) - last >= timedelta(
            hours=policy.reaction_cooldown_hours
        )

    def record_reaction(
        self, peer: str | int, message_id: int, now: datetime
    ) -> None:
        key = f"{target_key(peer)}:{int(message_id)}"
        self.reaction_events[key] = iso_utc(now)

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from PySide6.QtCore import QCoreApplication
from telethon import functions, types, utils
from core.account_restriction import (
    RESTRICTION_CODES,
    activate_account_restriction,
)
from core.config import Config, TelegramSettings
from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from core.local_security import LocalFileSecurityError, validate_private_regular_file
from core.rate_limiter import RateLimiter
from core.secret_store import SecretStore
from core.single_instance import SingleInstance
from services.account_context import AccountSecretStoreView
from services.telegram_service import TelegramService
from storage.account_database_view import AccountDatabaseView
from storage.database import Database
from storage.db_account_activity import (
    DEFAULT_WARMUP_LEASE_SECONDS,
    new_activity_owner_token,
)
from storage.db_common import DatabaseError
from workers.flood_wait_guard import (
    persist_account_flood_wait,
    persisted_account_flood_wait_remaining,
)
from tools.account_activity_policy import (
    ActivityLedger,
    ActivityPolicy,
    GroupRule,
    LEDGER_SETTING_KEY,
    PrivateDialogRule,
    utc_now,
)

log = logging.getLogger("account_activity")
MAX_ACTIVITY_CONFIG_BYTES = 1024 * 1024


@dataclass(slots=True)
class DialogEntry:
    peer_id: int
    entity: Any
    title: str
    username: str
    is_user: bool
    is_group: bool


@dataclass(slots=True)
class SessionSummary:
    matched_private_dialogs: int = 0
    matched_groups: int = 0
    messages_sent: int = 0
    groups_read: int = 0
    reactions_sent: int = 0
    joins_confirmed: int = 0
    joins_already_member: int = 0
    joins_requested: int = 0
    skipped: int = 0

    def to_mapping(self) -> dict[str, int]:
        return {
            "matched_private_dialogs": self.matched_private_dialogs,
            "matched_groups": self.matched_groups,
            "messages_sent": self.messages_sent,
            "groups_read": self.groups_read,
            "reactions_sent": self.reactions_sent,
            "joins_confirmed": self.joins_confirmed,
            "joins_already_member": self.joins_already_member,
            "joins_requested": self.joins_requested,
            "skipped": self.skipped,
        }


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return default


def _strict_secret(view: AccountSecretStoreView, key: str) -> str | None:
    getter = getattr(type(view), "get_strict_optional", None)
    if callable(getter):
        return view.get_strict_optional(key)
    value = view.get(key, "")
    return None if value in (None, "") else str(value)


def build_account_settings(
    *,
    config: Config,
    database: Database,
    secret_store: SecretStore,
    account_id: int,
) -> TelegramSettings:
    account = database.get_telegram_account(account_id)
    if not account:
        raise RuntimeError(f"Telegram account {account_id} does not exist")
    if not bool(account.get("authorized")):
        raise RuntimeError(f"Telegram account {account_id} is not authorized")
    if not database.account_accepts_new_work(account_id):
        raise RuntimeError(
            f"Telegram account {account_id} is stopped or restricted"
        )

    account_db = AccountDatabaseView(database, account_id)
    secrets = AccountSecretStoreView(secret_store, account_id)
    saved = account_db.get_settings("telegram.")
    api_id = _as_int(saved.get("telegram.api_id"), config.telegram.api_id)
    api_hash = str(
        _strict_secret(secrets, "telegram.api_hash")
        or config.telegram.api_hash
        or ""
    ).strip()
    phone = str(
        _strict_secret(secrets, "telegram.phone")
        or config.telegram.phone
        or ""
    ).strip() or None
    proxy_port = _as_int(saved.get("telegram.proxy_port"), 0) or None
    return TelegramSettings(
        api_id=api_id,
        api_hash=api_hash,
        session_dir=config.telegram.session_dir,
        session_name=str(account.get("session_name") or f"account_{account_id}"),
        account_id=account_id,
        phone=phone,
        proxy_enabled=_as_bool(saved.get("telegram.proxy_enabled")),
        proxy_type=str(saved.get("telegram.proxy_type") or "SOCKS5").upper(),
        proxy_host=str(saved.get("telegram.proxy_host") or "").strip() or None,
        proxy_port=proxy_port,
        proxy_username=str(
            _strict_secret(secrets, "telegram.proxy_username") or ""
        ).strip()
        or None,
        proxy_password=str(
            _strict_secret(secrets, "telegram.proxy_password") or ""
        )
        or None,
        expected_account_id=account_id,
    )


def load_policy(path: Path) -> ActivityPolicy:
    try:
        validate_private_regular_file(
            path, max_bytes=MAX_ACTIVITY_CONFIG_BYTES, harden=True
        )
        text = path.read_text(encoding="utf-8")
    except (LocalFileSecurityError, OSError) as exc:
        raise RuntimeError(
            f"Could not securely read activity configuration {path}: {exc}"
        ) from exc
    return ActivityPolicy.from_json(text)


def normalize_lookup(value: str | int) -> str:
    if isinstance(value, int):
        return str(value)
    text = str(value or "").strip().lower()
    for prefix in (
        "https://t.me/",
        "http://t.me/",
        "t.me/",
        "https://telegram.me/",
        "http://telegram.me/",
        "telegram.me/",
    ):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.split("?", 1)[0].strip("/")
    if text.startswith("+") or text.startswith("joinchat/"):
        return text
    return text.lstrip("@")


def entry_aliases(entry: DialogEntry) -> set[str]:
    aliases = {str(entry.peer_id)}
    raw_id = getattr(entry.entity, "id", None)
    if raw_id is not None:
        aliases.add(str(int(raw_id)))
    if entry.username:
        aliases.add(entry.username.lower())
    return aliases


async def collect_dialogs(
    telegram: TelegramService, limit: int
) -> tuple[list[DialogEntry], int]:
    await telegram.ensure_connected()
    me = await telegram.get_connected_identity()
    self_id = int(getattr(me, "id", 0) or 0)
    result: list[DialogEntry] = []
    iterator = telegram.client.iter_dialogs(limit=int(limit)).__aiter__()
    async for dialog in telegram._iter_with_timeout(iterator):
        entity = dialog.entity
        try:
            peer_id = int(utils.get_peer_id(entity))
        except Exception:
            continue
        username = str(getattr(entity, "username", None) or "").strip()
        title = str(
            getattr(entity, "title", None)
            or getattr(entity, "first_name", None)
            or username
            or peer_id
        )
        is_user = bool(getattr(dialog, "is_user", False)) and isinstance(
            entity, types.User
        )
        is_group = bool(getattr(dialog, "is_group", False))
        if is_user and int(getattr(entity, "id", 0) or 0) == self_id:
            is_user = False
        result.append(
            DialogEntry(
                peer_id=peer_id,
                entity=entity,
                title=title,
                username=username,
                is_user=is_user,
                is_group=is_group,
            )
        )
    return result, self_id


def find_dialog(
    entries: Iterable[DialogEntry], peer: str | int
) -> DialogEntry | None:
    wanted = normalize_lookup(peer)
    for entry in entries:
        if wanted in entry_aliases(entry):
            return entry
    return None


async def pause_between_actions(
    telegram: TelegramService,
    policy: ActivityPolicy,
    rng: random.Random,
) -> None:
    seconds = rng.randint(
        policy.action_pause_min_seconds,
        policy.action_pause_max_seconds,
    )
    if not await telegram.safe_sleep(seconds):
        raise asyncio.CancelledError


async def latest_messages(
    telegram: TelegramService,
    entity: Any,
    limit: int,
) -> list[Any]:
    values = await telegram.get_messages(entity, limit=max(1, int(limit)))
    return list(values or [])


def latest_real_message(messages: Iterable[Any]) -> Any | None:
    for message in messages:
        if getattr(message, "id", None) is None:
            continue
        if getattr(message, "action", None) is not None:
            continue
        return message
    return None


def latest_incoming_message(messages: Iterable[Any]) -> Any | None:
    for message in messages:
        if getattr(message, "id", None) is None:
            continue
        if getattr(message, "action", None) is not None:
            continue
        if bool(getattr(message, "out", False)):
            continue
        return message
    return None


async def send_reaction_once(
    telegram: TelegramService,
    *,
    peer: Any,
    message_id: int,
    emoji: str,
) -> None:
    """Dispatch one reaction through the normal fail-closed transport path."""

    request = functions.messages.SendReactionRequest(
        peer=peer,
        msg_id=int(message_id),
        reaction=[types.ReactionEmoji(emoticon=str(emoji))],
    )
    await telegram.execute(
        telegram.client,
        request,
        retry_network=False,
        unknown_result_code="reaction_result_unknown",
    )


async def execute_session(
    *,
    policy: ActivityPolicy,
    account_db: AccountDatabaseView,
    telegram: TelegramService,
    execute_mutations: bool,
    rng: random.Random,
) -> SessionSummary:
    summary = SessionSummary()
    now = utc_now()
    ledger = ActivityLedger.from_json(
        account_db.get_setting(LEDGER_SETTING_KEY, "{}"), strict=True
    )
    ledger.prune(now)

    def save_ledger() -> None:
        account_db.set_setting(LEDGER_SETTING_KEY, ledger.to_json())

    dialogs, _self_id = await collect_dialogs(telegram, policy.max_dialog_scan)
    private_matches: list[tuple[PrivateDialogRule, DialogEntry]] = []
    group_matches: list[tuple[GroupRule, DialogEntry]] = []

    for rule in policy.private_dialogs:
        entry = find_dialog(dialogs, rule.peer)
        if entry is None or not entry.is_user:
            log.warning("Private dialog is not present or is not a user chat: %r", rule.peer)
            summary.skipped += 1
            continue
        if bool(getattr(entry.entity, "bot", False)):
            log.warning("Bot dialogs are excluded from automatic messages: %s", entry.title)
            summary.skipped += 1
            continue
        private_matches.append((rule, entry))

    for group_rule in policy.groups:
        entry = find_dialog(dialogs, group_rule.peer)
        if entry is None or not entry.is_group:
            log.warning("Group is not present in the account: %r", group_rule.peer)
            summary.skipped += 1
            continue
        group_matches.append((group_rule, entry))

    summary.matched_private_dialogs = len(private_matches)
    summary.matched_groups = len(group_matches)

    if not execute_mutations:
        log.info("Dry run: no reads, reactions, messages, or joins were sent")
        return summary

    # Read a small explicit subset of groups. Fetching and read acknowledgement
    # are bounded and use the project's process-wide paced Telegram client.
    rng.shuffle(group_matches)
    reaction_budget = policy.max_reactions_per_run
    for group_rule, entry in group_matches[: policy.max_group_reads_per_run]:
        messages = await latest_messages(
            telegram, entry.entity, policy.read_messages_per_group
        )
        latest = latest_real_message(messages)
        if latest is None:
            summary.skipped += 1
            continue
        await telegram.execute(
            telegram.client.send_read_acknowledge,
            entry.entity,
            latest,
        )
        summary.groups_read += 1
        log.info("Read acknowledged: %s", entry.title)

        incoming = latest_incoming_message(messages)
        if (
            reaction_budget > 0
            and group_rule.allow_reactions
            and incoming is not None
            and rng.random() < policy.reaction_probability
            and ledger.reaction_due(
                group_rule.peer, int(incoming.id), policy, utc_now()
            )
        ):
            await pause_between_actions(telegram, policy, rng)
            emoji = rng.choice(policy.reaction_emojis)
            # Reserve before dispatch. A crash or ambiguous result must not
            # replay the same mutating action during the next one-shot run.
            ledger.record_reaction(group_rule.peer, int(incoming.id), utc_now())
            save_ledger()
            await send_reaction_once(
                telegram,
                peer=entry.entity,
                message_id=int(incoming.id),
                emoji=emoji,
            )
            reaction_budget -= 1
            summary.reactions_sent += 1
            log.info("Reaction sent in group %s", entry.title)
        await pause_between_actions(telegram, policy, rng)

    # Existing private dialogs only. Text is supplied by the operator; the tool
    # does not generate conversation content or discover recipients.
    rng.shuffle(private_matches)
    message_budget = policy.send_messages_per_run
    for rule, entry in private_matches:
        if message_budget <= 0:
            break
        messages = await latest_messages(telegram, entry.entity, 5)
        incoming = latest_incoming_message(messages)
        if (
            reaction_budget > 0
            and rule.allow_reactions
            and incoming is not None
            and rng.random() < policy.reaction_probability
            and ledger.reaction_due(
                rule.peer, int(incoming.id), policy, utc_now()
            )
        ):
            await pause_between_actions(telegram, policy, rng)
            emoji = rng.choice(policy.reaction_emojis)
            ledger.record_reaction(rule.peer, int(incoming.id), utc_now())
            save_ledger()
            await send_reaction_once(
                telegram,
                peer=entry.entity,
                message_id=int(incoming.id),
                emoji=emoji,
            )
            reaction_budget -= 1
            summary.reactions_sent += 1
            log.info("Reaction sent in private dialog %s", entry.title)

        if not ledger.message_due(rule.peer, policy, utc_now()):
            summary.skipped += 1
            continue
        await pause_between_actions(telegram, policy, rng)
        text = rng.choice(rule.messages)
        ledger.record_message(rule.peer, utc_now())
        save_ledger()
        await telegram.send_message(
            entry.entity,
            text,
            unknown_result_code="account_activity_message_unknown",
        )
        message_budget -= 1
        summary.messages_sent += 1
        log.info("Operator message sent to existing dialog %s", entry.title)
        await pause_between_actions(telegram, policy, rng)

    # Join only explicit targets. A Telegram join request that is merely pending
    # is not counted as membership. Confirmed direct responses are persisted in
    # the encrypted account-scoped settings ledger.
    join_budget = min(
        policy.max_joins_per_run,
        ledger.weekly_join_remaining(policy, utc_now()),
    )
    join_targets = list(policy.join_targets)
    rng.shuffle(join_targets)
    for target in join_targets:
        if join_budget <= 0:
            break
        current = utc_now()
        if not ledger.can_join_now(policy, current):
            break
        if not ledger.can_join_target(target, policy, current):
            summary.skipped += 1
            continue
        await pause_between_actions(telegram, policy, rng)
        # Reserve the target before the JOIN request. This is deliberately
        # fail-closed: after a process crash the runner will not replay a join
        # whose Telegram-side outcome might already have been accepted.
        ledger.record_join_attempt(target, utc_now())
        save_ledger()
        try:
            if isinstance(target, int):
                joined = await telegram.join(target)
            else:
                text = str(target)
                if "+" in text or "joinchat/" in text.lower():
                    joined = await telegram.join_saved_dialog(invite_link=text)
                else:
                    username = normalize_lookup(text)
                    joined = await telegram.join_saved_dialog(username=username)
        except NonRetryableTelegramError as exc:
            if getattr(exc, "code", "") == "join_requested":
                summary.joins_requested += 1
                log.warning("Join request is pending and was not counted: %r", target)
                break
            raise

        if joined:
            summary.joins_confirmed += 1
            join_budget -= 1
            log.info("Join confirmed: %r", target)
        else:
            summary.joins_already_member += 1
            log.info("Already a participant: %r", target)
        await pause_between_actions(telegram, policy, rng)

    return summary


async def maintain_warmup_lease(
    *,
    database: Database,
    account_id: int,
    owner_token: str,
    stop_event: asyncio.Event,
) -> None:
    """Renew the durable warmup lease until the one-shot session finishes."""

    heartbeat_seconds = max(30, DEFAULT_WARMUP_LEASE_SECONDS // 6)
    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=float(heartbeat_seconds)
            )
        except asyncio.TimeoutError:
            renewed = database.renew_account_activity_lease(
                account_id,
                owner_token=owner_token,
                lease_seconds=DEFAULT_WARMUP_LEASE_SECONDS,
            )
            if not renewed:
                raise RuntimeError(
                    "Прогрев остановлен: потеряна блокировка Telegram-аккаунта"
                )
        else:
            return


async def run_session_with_lease_supervision(
    *,
    policy: ActivityPolicy,
    account_db: AccountDatabaseView,
    telegram: TelegramService,
    rng: random.Random,
    lease_task: asyncio.Task[None],
    lease_stop: asyncio.Event,
) -> SessionSummary:
    """Cancel Telegram work immediately when the durable lease is lost."""

    session_task = asyncio.create_task(
        execute_session(
            policy=policy,
            account_db=account_db,
            telegram=telegram,
            execute_mutations=True,
            rng=rng,
        ),
        name=f"account-activity-session-{policy.account_id}",
    )
    try:
        done, _pending = await asyncio.wait(
            {session_task, lease_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lease_task in done:
            # result() re-raises heartbeat/ownership failures. A clean early
            # return is also unsafe because only async_main owns stop_event.
            try:
                lease_task.result()
            except BaseException:
                session_task.cancel()
                with suppress(asyncio.CancelledError):
                    await session_task
                raise
            session_task.cancel()
            with suppress(asyncio.CancelledError):
                await session_task
            raise RuntimeError(
                "Прогрев остановлен: heartbeat блокировки завершился раньше сессии"
            )

        summary = session_task.result()
        lease_stop.set()
        await lease_task
        return summary
    finally:
        if not session_task.done():
            session_task.cancel()
            with suppress(asyncio.CancelledError):
                await session_task


def require_account_rpc_ready(database: Database, account_id: int) -> None:
    """Block a fresh standalone run while Telegram's account FloodWait is active."""

    remaining = persisted_account_flood_wait_remaining(
        worker_db=database, account_id=account_id
    )
    if remaining > 0:
        raise RuntimeError(
            "Для аккаунта действует общий FloodWait Telegram. "
            f"Повторите запуск не раньше чем через {remaining} сек."
        )


def persist_account_safety_outcome(
    *, database: Database, account_id: int, exc: BaseException
) -> None:
    """Persist account-wide Telegram safety state before the lease is released."""

    code = str(getattr(exc, "code", "") or "").strip().lower()
    if isinstance(exc, DeferredTelegramError) and code == "flood_wait_deferred":
        wait = max(1, int(getattr(exc, "retry_after", 1) or 1))
        persist_account_flood_wait(
            worker_db=database,
            account_id=account_id,
            retry_at=utc_now() + timedelta(seconds=wait),
            code=code,
            wait_seconds=wait,
            source_task_id=None,
        )
        return

    if isinstance(exc, NonRetryableTelegramError) and code in RESTRICTION_CODES:
        activate_account_restriction(
            database,
            code=code,
            message=str(exc),
            details=dict(getattr(exc, "details", {}) or {}),
            account_id=account_id,
        )
        database.set_account_runtime_state(
            account_id,
            "restricted",
            error=f"{code}: {exc}",
        )
        return

    if isinstance(exc, NonRetryableTelegramError) and code == "authorization_required":
        database.set_account_runtime_state(
            account_id,
            "authorization_required",
            error=f"{code}: {exc}",
        )


def validate_local_configuration(
    *,
    policy: ActivityPolicy,
    config: Config,
    database: Database,
) -> dict[str, Any]:
    """Validate local account/configuration without opening Telegram or scanning dialogs."""

    settings = build_account_settings(
        config=config,
        database=database,
        secret_store=SecretStore(),
        account_id=policy.account_id,
    )
    if database.get_active_comment_campaign(account_id=policy.account_id):
        raise RuntimeError(
            "Для аккаунта уже запущена кампания комментирования. "
            "Остановите её перед запуском прогрева."
        )
    if database.get_active_join_campaign(account_id=policy.account_id):
        raise RuntimeError(
            "Для аккаунта уже запущена кампания вступлений. "
            "Остановите её перед запуском прогрева."
        )
    database.require_account_not_warming(policy.account_id)
    require_account_rpc_ready(database, policy.account_id)
    return {
        "validated": True,
        "account_id": policy.account_id,
        "private_dialog_targets": len(policy.private_dialogs),
        "group_targets": len(policy.groups),
        "join_targets": len(policy.join_targets),
        "telegram_configured": bool(settings.configured),
        "telegram_rpc_performed": False,
    }


async def async_main(args: argparse.Namespace) -> int:
    policy = load_policy(Path(args.config).expanduser().resolve())
    config = Config()
    RateLimiter.configure_process_interval(config.rate_limit)

    database: Database | None = None
    telegram: TelegramService | None = None
    lease_token: str | None = None
    lease_stop: asyncio.Event | None = None
    lease_task: asyncio.Task[None] | None = None
    instance: SingleInstance | None = None
    try:
        # Database bootstrap may run migrations, so even a local-only validation
        # owns the normal single-instance lock before opening the shared profile.
        app = QCoreApplication.instance() or QCoreApplication([sys.argv[0]])
        _ = app
        instance = SingleInstance()
        if not instance.acquire():
            raise RuntimeError(
                "LansetSpBot is already running. Close the main application before this runner."
            )

        database = Database(config.database_path)
        if not args.execute:
            result = validate_local_configuration(
                policy=policy,
                config=config,
                database=database,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        require_account_rpc_ready(database, policy.account_id)
        lease_token = new_activity_owner_token()
        database.acquire_account_activity_lease(
            policy.account_id,
            owner_token=lease_token,
            lease_seconds=DEFAULT_WARMUP_LEASE_SECONDS,
            metadata={
                "runner": "account_activity_experimental",
                "config": Path(args.config).name,
            },
        )
        lease_stop = asyncio.Event()
        lease_task = asyncio.create_task(
            maintain_warmup_lease(
                database=database,
                account_id=policy.account_id,
                owner_token=lease_token,
                stop_event=lease_stop,
            ),
            name=f"warmup-lease-{policy.account_id}",
        )
        account_db = AccountDatabaseView(database, policy.account_id)
        settings = build_account_settings(
            config=config,
            database=database,
            secret_store=SecretStore(),
            account_id=policy.account_id,
        )
        telegram = TelegramService(
            settings,
            RateLimiter(config.rate_limit),
            status_callback=lambda text: log.info("Telegram: %s", text) if text else None,
        )
        seed = (
            args.seed
            if args.seed is not None
            else random.SystemRandom().randrange(2**63)
        )
        rng = random.Random(seed)
        try:
            summary = await run_session_with_lease_supervision(
                policy=policy,
                account_db=account_db,
                telegram=telegram,
                rng=rng,
                lease_task=lease_task,
                lease_stop=lease_stop,
            )
        except (DeferredTelegramError, NonRetryableTelegramError) as exc:
            persist_account_safety_outcome(
                database=database,
                account_id=policy.account_id,
                exc=exc,
            )
            raise
        print(json.dumps(summary.to_mapping(), ensure_ascii=False, indent=2))
        return 0
    finally:
        # Cleanup is deliberately nested so cancellation or a failed Telegram
        # disconnect cannot skip lease release, SQLCipher shutdown, or the
        # process-wide SingleInstance unlock.
        try:
            if telegram is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await telegram.disconnect()
            if lease_stop is not None:
                lease_stop.set()
            if lease_task is not None and not lease_task.done():
                lease_task.cancel()
            if lease_task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await lease_task
            if database is not None and lease_token is not None:
                try:
                    database.release_account_activity_lease(
                        policy.account_id, owner_token=lease_token
                    )
                except Exception:
                    log.exception("Could not release the account warmup lease")
            if database is not None:
                try:
                    finalize = getattr(database, "finalize_shutdown", None)
                    if callable(finalize):
                        finalize()
                    else:
                        database.close_thread_connection()
                except Exception:
                    log.exception("Could not finalize the activity database")
        finally:
            if instance is not None:
                try:
                    instance.close()
                except Exception:
                    log.exception("Could not release the process instance lock")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-shot, allowlist-only Telegram account activity runner for owned accounts"
        )
    )
    parser.add_argument(
        "--config",
        default="account_activity.json",
        help="Path to UTF-8 JSON configuration",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform reads, reactions, messages and joins. Omit for validation-only dry run.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional deterministic test seed",
    )
    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(async_main(args))
    except (ValueError, RuntimeError, DatabaseError) as exc:
        log.error("%s", exc)
        return 2
    except DeferredTelegramError as exc:
        log.error(
            "Telegram deferred the session: code=%s retry_after=%s",
            getattr(exc, "code", "deferred"),
            getattr(exc, "retry_after", None),
        )
        return 3
    except NonRetryableTelegramError as exc:
        log.error(
            "Telegram stopped the session: code=%s message=%s",
            getattr(exc, "code", "non_retryable"),
            exc,
        )
        return 4
    except KeyboardInterrupt:
        log.warning("Interrupted by operator")
        return 130
    except Exception:
        log.exception("Experimental activity session failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

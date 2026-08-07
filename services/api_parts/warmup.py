from __future__ import annotations

import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from core.warmup_planner import (
    build_week_plan,
    day_order_titles,
    generate_profile,
    validate_plan,
)
from core.warmup_scenarios import SCENARIO_BY_KEY
from storage.db_account_activity import new_activity_owner_token

if TYPE_CHECKING:  # pragma: no cover
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class WarmupAPIMixin(_MixinHost):
    WARMUP_LEASE_SECONDS = 30 * 60

    @staticmethod
    def _setting_enabled(value: object) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _mask_proxy_value(value: object, *, tail: int = 3) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) <= tail:
            return "•" * len(text)
        return f"{text[:2]}{'•' * max(3, len(text) - tail - 2)}{text[-tail:]}"

    def _warmup_proxy_summary(self, account_id: int) -> dict[str, Any]:
        values = self.get_account_settings(account_id)
        enabled = self._setting_enabled(values.get("telegram.proxy_enabled"))
        host = str(values.get("telegram.proxy_host") or "").strip()
        port = str(values.get("telegram.proxy_port") or "").strip()
        proxy_type = str(values.get("telegram.proxy_type") or "SOCKS5").upper()
        username = str(values.get("telegram.proxy_username") or "").strip()
        configured = enabled and bool(host) and port.isdigit() and int(port) > 0
        return {
            "enabled": enabled,
            "configured": configured,
            "type": proxy_type,
            "host_masked": self._mask_proxy_value(host, tail=4),
            "port": port,
            "username_masked": self._mask_proxy_value(username, tail=2),
        }

    @staticmethod
    def _normalize_group_ref(value: object) -> str:
        text = " ".join(str(value or "").split()).strip()
        if not text:
            raise ValueError("Введите ссылку или @username группы")
        if text.startswith("@"): 
            username = text[1:].strip()
            if not username:
                raise ValueError("Некорректный @username группы")
            return f"@{username}"
        if text.startswith("t.me/"):
            text = f"https://{text}"
        if text.startswith(("http://", "https://")):
            parsed = urlparse(text)
            if parsed.netloc.lower() not in {"t.me", "telegram.me", "www.t.me"}:
                raise ValueError("Разрешены только ссылки Telegram")
            if not parsed.path.strip("/"):
                raise ValueError("Ссылка Telegram не содержит группу")
            return text
        if text.replace("_", "").isalnum():
            return f"@{text.lstrip('@')}"
        raise ValueError("Некорректная ссылка или @username группы")

    def get_warmup_overview(self) -> dict[str, Any]:
        accounts = [dict(item) for item in self.list_telegram_accounts()]
        states = {
            int(item["account_id"]): dict(item)
            for item in self.database.list_warmup_account_states()
        }
        for account in accounts:
            account_id = int(account["telegram_account_id"])
            state = states.get(account_id, {})
            account["warmup_status"] = str(state.get("status") or "available")
            account["active_pair_id"] = state.get("active_pair_id")
            account["weeks_completed"] = int(state.get("weeks_completed") or 0)
            account["proxy"] = self._warmup_proxy_summary(account_id)
            account["warmup_eligible"] = bool(
                account.get("authorized")
                and not account.get("stopped")
                and not account.get("campaign_active")
                and account["warmup_status"] in {"available", "completed"}
                and account["active_pair_id"] is None
                and account["proxy"]["configured"]
            )

        pairs = [dict(item) for item in self.database.list_warmup_pairs()]
        for pair in pairs:
            order = tuple(
                value for value in str(pair.get("day_order") or "").split(",") if value
            )
            pair["day_titles"] = [
                SCENARIO_BY_KEY[key].title for key in order if key in SCENARIO_BY_KEY
            ]
            pair["progress_percent"] = (
                int(
                    100
                    * int(pair.get("finished_steps") or 0)
                    / max(1, int(pair.get("total_steps") or 0))
                )
            )
        return {
            "accounts": accounts,
            "pairs": pairs,
            "groups": self.database.list_warmup_groups(),
            "active_account_count": sum(
                1 for account in accounts if account["warmup_status"] == "active"
            ),
            "account_limit": 40,
        }

    def _require_warmup_account(self, account_id: int) -> None:
        summary = self._warmup_proxy_summary(account_id)
        if not summary["configured"]:
            raise ValueError(
                "Для каждого аккаунта прогрева должен быть включён и заполнен собственный прокси"
            )
        with self._secret_lock:
            phone = self._strict_account_secret(int(account_id), "telegram.phone")
        if not str(phone or "").strip():
            raise ValueError(
                "У аккаунта отсутствует сохранённый телефон для создания связанного контакта"
            )

    def _acquire_pair_leases(
        self,
        *,
        account_a_id: int,
        account_b_id: int,
        owner_token_a: str,
        owner_token_b: str,
        pair_id: int | None = None,
    ) -> None:
        acquired: list[tuple[int, str]] = []
        try:
            for account_id, token in (
                (account_a_id, owner_token_a),
                (account_b_id, owner_token_b),
            ):
                self.database.acquire_account_activity_lease(
                    account_id,
                    owner_token=token,
                    lease_seconds=self.WARMUP_LEASE_SECONDS,
                    metadata={"pair_id": pair_id or 0, "source": "warmup_gui"},
                )
                acquired.append((account_id, token))
        except Exception:
            for account_id, token in acquired:
                try:
                    self.database.release_account_activity_lease(
                        account_id, owner_token=token
                    )
                except Exception:
                    pass
            raise

    def _release_account_warmup_lease(self, account_id: int) -> None:
        lease = self.database.get_account_activity_lease(account_id)
        if not lease:
            return
        token = str(lease.get("owner_token") or "")
        if token:
            self.database.release_account_activity_lease(
                account_id, owner_token=token
            )

    def create_warmup_pair(self, account_a_id: int, account_b_id: int) -> dict[str, Any]:
        account_a = int(account_a_id)
        account_b = int(account_b_id)
        if account_a <= 0 or account_b <= 0 or account_a == account_b:
            raise ValueError("Выберите два разных Telegram-аккаунта")
        self._require_warmup_account(account_a)
        self._require_warmup_account(account_b)
        seed = secrets.token_hex(16)
        profile = generate_profile(seed)
        start_at = datetime.now().astimezone()
        steps = build_week_plan(
            account_a_id=account_a,
            account_b_id=account_b,
            week_number=1,
            profile=profile,
            start_at=start_at,
        )
        validate_plan(steps)
        owner_token_a = new_activity_owner_token()
        owner_token_b = new_activity_owner_token()
        self._acquire_pair_leases(
            account_a_id=account_a,
            account_b_id=account_b,
            owner_token_a=owner_token_a,
            owner_token_b=owner_token_b,
        )
        try:
            pair = self.database.create_warmup_pair(
                account_a_id=account_a,
                account_b_id=account_b,
                profile=profile.to_record(),
                steps=[step.to_record() for step in steps],
                owner_token_a=owner_token_a,
                owner_token_b=owner_token_b,
                started_at=steps[0].scheduled_at,
                ends_at=steps[-1].scheduled_at,
            )
        except Exception:
            for account_id, token in (
                (account_a, owner_token_a),
                (account_b, owner_token_b),
            ):
                try:
                    self.database.release_account_activity_lease(
                        account_id, owner_token=token
                    )
                except Exception:
                    pass
            raise
        pair_id = int(pair["id"])
        self.database.enqueue_warmup_step(pair_id)
        self.start_queue()
        return {
            "pair": pair,
            "profile": profile.to_record(),
            "day_titles": list(day_order_titles(profile)),
        }

    def add_warmup_group(self, chat_ref: str) -> dict[str, Any]:
        normalized = self._normalize_group_ref(chat_ref)
        return cast(
            dict[str, Any], self.database.add_warmup_group(normalized, normalized)
        )

    def remove_warmup_group(self, group_id: int) -> bool:
        return bool(self.database.remove_warmup_group(group_id))

    def pause_warmup_pair(self, pair_id: int) -> bool:
        owner = int(pair_id)
        def mutation() -> Any:
            return self.database.pause_warmup_pair(owner)
        worker = self.queue_worker
        if worker is not None and worker.isRunning():
            return bool(
                worker.cancel_scopes_and_run((("warmup_pair", owner),), mutation)
            )
        return bool(mutation())

    def resume_warmup_pair(self, pair_id: int) -> bool:
        owner = int(pair_id)
        changed = bool(self.database.resume_warmup_pair(owner))
        if not changed:
            return False
        worker = self.queue_worker
        if worker is not None:
            worker.clear_scope_cancellation("warmup_pair", owner)
        self.database.enqueue_warmup_step(owner)
        self.start_queue()
        return True

    def retry_failed_warmup_pair(self, pair_id: int) -> bool:
        owner = int(pair_id)
        changed = bool(self.database.retry_failed_warmup_step(owner))
        if not changed:
            return False
        worker = self.queue_worker
        if worker is not None:
            worker.clear_scope_cancellation("warmup_pair", owner)
        self.database.enqueue_warmup_step(owner)
        self.start_queue()
        return True

    def extend_warmup_pair(self, pair_id: int) -> dict[str, Any]:
        pair = self.database.get_warmup_pair(pair_id)
        if not pair or str(pair.get("status") or "") != "completed":
            raise ValueError("Продлить можно только завершённую связку")
        account_a = int(pair["account_a_id"])
        account_b = int(pair["account_b_id"])
        self._require_warmup_account(account_a)
        self._require_warmup_account(account_b)
        week_number = int(pair.get("week_number") or 1) + 1
        seed = secrets.token_hex(16)
        profile = generate_profile(seed)
        steps = build_week_plan(
            account_a_id=account_a,
            account_b_id=account_b,
            week_number=week_number,
            profile=profile,
            start_at=datetime.now().astimezone(),
        )
        validate_plan(steps)
        owner_token_a = new_activity_owner_token()
        owner_token_b = new_activity_owner_token()
        self._acquire_pair_leases(
            account_a_id=account_a,
            account_b_id=account_b,
            owner_token_a=owner_token_a,
            owner_token_b=owner_token_b,
            pair_id=int(pair_id),
        )
        try:
            updated = self.database.extend_warmup_pair(
                pair_id,
                profile=profile.to_record(),
                steps=[step.to_record() for step in steps],
                owner_token_a=owner_token_a,
                owner_token_b=owner_token_b,
                started_at=steps[0].scheduled_at,
                ends_at=steps[-1].scheduled_at,
            )
        except Exception:
            for account_id, token in (
                (account_a, owner_token_a),
                (account_b, owner_token_b),
            ):
                try:
                    self.database.release_account_activity_lease(
                        account_id, owner_token=token
                    )
                except Exception:
                    pass
            raise
        self.database.enqueue_warmup_step(int(pair_id))
        self.start_queue()
        return {
            "pair": updated,
            "profile": profile.to_record(),
            "day_titles": list(day_order_titles(profile)),
        }

    def transfer_warmup_account(self, account_id: int) -> dict[str, Any]:
        result = cast(
            dict[str, Any], self.database.transfer_warmup_account(account_id)
        )
        self._release_account_warmup_lease(int(account_id))
        return result

    def _warmup_bootstrap(self) -> None:
        if getattr(self, "_shutdown_requested", False):
            return
        if not getattr(self, "_warmup_recovery_done", False):
            self.database.recover_stale_warmup_steps()
            self._warmup_recovery_done = True
        self._warmup_lease_tick()

    def _warmup_lease_tick(self) -> None:
        if getattr(self, "_shutdown_requested", False):
            return
        should_start = False
        for pair in self.database.list_active_warmup_pairs():
            pair_id = int(pair["id"])
            try:
                for account_id, token in (
                    (int(pair["account_a_id"]), str(pair["owner_token_a"])),
                    (int(pair["account_b_id"]), str(pair["owner_token_b"])),
                ):
                    self.database.acquire_account_activity_lease(
                        account_id,
                        owner_token=token,
                        lease_seconds=self.WARMUP_LEASE_SECONDS,
                        metadata={"pair_id": pair_id, "source": "warmup_runtime"},
                    )
                if str(pair.get("status") or "") == "running":
                    if self.database.enqueue_warmup_step(pair_id):
                        should_start = True
            except Exception as exc:
                try:
                    self.database.pause_warmup_pair(
                        pair_id,
                        f"Прогрев приостановлен: {type(exc).__name__}: {exc}",
                    )
                except Exception:
                    # A timer callback must never escape into the Qt event loop.
                    # The next tick can retry after a transient SQLite failure.
                    pass
        if should_start:
            self.start_queue()

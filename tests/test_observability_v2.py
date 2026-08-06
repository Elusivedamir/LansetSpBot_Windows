from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.observability import (
    build_account_health_snapshot,
    campaign_statistics,
    classify_result,
    humanize_reason,
)


class _Database:
    def __init__(self, now: datetime):
        self.now = now
        self.cooldown_calls: list[int] = []

    def list_telegram_accounts(self):
        return [
            {
                "telegram_account_id": 101,
                "authorized": True,
                "runtime_state": "running",
                "stopped": False,
            }
        ]

    def get_account_restriction_state(self, *, account_id):
        assert account_id == 101
        return {"active": False}

    def get_account_settings(self, account_id):
        assert account_id == 101
        return {
            "telegram.proxy_enabled": "1",
            "telegram.proxy_host": "127.0.0.1",
        }

    def get_tasks(self):
        return [
            {
                "id": 8,
                "account_id": 101,
                "type": "parse_audience",
                "status": "running",
                "status_text": "Парсинг аудитории",
            }
        ]

    def get_comment_history(self, *, limit, account_id):
        assert limit == 1000
        assert account_id == 101
        return [
            {
                "id": 1,
                "status": "sent",
                "sent_at": (self.now - timedelta(days=3)).isoformat(),
            },
            {
                "id": 4,
                "status": "failed",
                "sent_at": (self.now - timedelta(minutes=5)).isoformat(),
            },
            {
                "id": 3,
                "status": "sent",
                "sent_at": (self.now - timedelta(hours=1)).isoformat(),
            },
            {
                "id": 2,
                "status": "uncertain",
                "sent_at": (self.now - timedelta(hours=2)).isoformat(),
            },
        ]

    def get_account_rpc_cooldown(self, *, account_id):
        self.cooldown_calls.append(account_id)
        return {
            "active": True,
            "code": "flood_wait_deferred",
            "effective_next_allowed_at": (self.now + timedelta(minutes=8)).isoformat(),
        }


def test_account_health_uses_real_24_hour_window_latest_rows_and_rpc_cooldown():
    now = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)
    database = _Database(now)

    snapshot = build_account_health_snapshot(database, 101, now=now)

    assert snapshot["sent_24h"] == 1
    assert snapshot["errors_24h"] == 2
    assert snapshot["last_success"] == "2026-08-06 08:00 UTC"
    assert snapshot["last_error"] == "Ошибка выполнения"
    assert "FloodWait" in snapshot["flood_wait"]
    assert "2026-08-06 09:08 UTC" in snapshot["flood_wait"]
    assert database.cooldown_calls == [101]
    assert snapshot["current_task"].startswith("parse_audience")


def test_campaign_statistics_are_read_only_and_use_existing_ledger():
    state = {"planned_count": 10, "attempted_count": 5, "sent_count": 2}
    history = [
        {"status": "sent"},
        {"status": "Отправлено"},
        {"status": "skipped"},
        {"status": "failed"},
        {"status": "uncertain"},
    ]
    before = [dict(row) for row in history]

    assert campaign_statistics(state, history) == {
        "planned": 10,
        "attempted": 5,
        "sent": 2,
        "skipped": 1,
        "failed": 1,
        "cancelled": 0,
        "uncertain": 1,
        "remaining": 5,
    }
    assert history == before
    assert classify_result("Нет обсуждения · пропущено") == "skipped"
    assert humanize_reason("authorization_required") == "Требуется повторная авторизация аккаунта"

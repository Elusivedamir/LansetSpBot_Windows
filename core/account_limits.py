from __future__ import annotations

MAX_REGISTERED_TELEGRAM_ACCOUNTS = 70
MAX_ACTIVE_TELEGRAM_ACCOUNT_RUNTIMES = 70
MAX_CONCURRENT_TELEGRAM_ACCOUNT_TASKS = 5

# Backward-compatible alias for integrations that imported the old name.
# It limits concurrent account task execution, not the number of active runtimes.
MAX_PARALLEL_ACCOUNT_RUNTIMES = MAX_CONCURRENT_TELEGRAM_ACCOUNT_TASKS


def account_limit_message(
    limit: int = MAX_REGISTERED_TELEGRAM_ACCOUNTS,
) -> str:
    return (
        "Достигнут лимит: можно подключить не более "
        f"{int(limit)} Telegram-аккаунтов."
    )

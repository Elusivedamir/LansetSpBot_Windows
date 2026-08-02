from __future__ import annotations

MAX_REGISTERED_TELEGRAM_ACCOUNTS = 70
MAX_PARALLEL_ACCOUNT_RUNTIMES = 5


def account_limit_message(
    limit: int = MAX_REGISTERED_TELEGRAM_ACCOUNTS,
) -> str:
    return (
        "Достигнут лимит: можно подключить не более "
        f"{int(limit)} Telegram-аккаунтов."
    )

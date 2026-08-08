from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any, cast

from core.account_limits import (
    MAX_REGISTERED_TELEGRAM_ACCOUNTS,
    account_limit_message,
)
from core.config import MAX_COMMENT_VARIANTS
from storage.db_common import DatabaseError
from storage.sqlcipher_driver import dbapi as sqlite3

if TYPE_CHECKING:  # pragma: no cover
    from core.mixin_host import MixinHost as _MixinHost
else:
    class _MixinHost:
        pass














from storage.account_repository_parts.common import (
    ACCOUNT_SETTING_PREFIXES,
    ACCOUNT_STATES,
    MAX_TELEGRAM_ACCOUNTS,
    SECRET_ACCOUNT_SETTING_KEYS,
    SESSION_NAME_RE,
    _active_unique,
    _fingerprint,
    _mask_phone,
    _normalized_slots,
    _positive_account_id,
)

from storage.account_repository_parts.transfers import AccountTransferRepositoryMixin
from storage.account_repository_parts.settings import AccountSettingsRepositoryMixin
from storage.account_repository_parts.registry import AccountRegistryRepositoryMixin

class AccountRepositoryMixin(AccountTransferRepositoryMixin, AccountSettingsRepositoryMixin, AccountRegistryRepositoryMixin, _MixinHost):
    """Durable registry and explicit cross-account operations."""

    MAX_TELEGRAM_ACCOUNTS = MAX_TELEGRAM_ACCOUNTS

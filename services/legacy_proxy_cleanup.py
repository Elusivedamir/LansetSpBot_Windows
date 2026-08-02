from __future__ import annotations

import re
from typing import Any

_REMOVED_SECRET_KEY = "telegram.proxy_secret"
_ACCOUNT_SECRET_RE = re.compile(
    r"^account\.[1-9][0-9]*\.telegram\.proxy_secret$"
)
CLEANUP_SETTING = "internal.removed_proxy_secret_cleanup_complete"


def purge_removed_proxy_credentials(database, secret_store) -> dict[str, Any]:
    """Remove credentials for the retired proxy transport from protected storage.

    Database migration has already disabled the retired transport. This encrypted
    store cleanup is intentionally idempotent and can be retried on every startup.
    """

    export = getattr(secret_store, "export_snapshot", None)
    replace = getattr(secret_store, "replace_snapshot", None)
    if not callable(export) or not callable(replace):
        raise RuntimeError("Protected credential store does not support cleanup")

    snapshot = dict(export())
    removed_keys = {
        key
        for key in snapshot
        if key == _REMOVED_SECRET_KEY or _ACCOUNT_SECRET_RE.fullmatch(str(key))
    }
    if removed_keys:
        replace(
            {
                key: value
                for key, value in snapshot.items()
                if key not in removed_keys
            }
        )
    database.set_setting(CLEANUP_SETTING, "1")
    return {
        "completed": True,
        "removed": len(removed_keys),
    }

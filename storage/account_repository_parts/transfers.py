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

class AccountTransferRepositoryMixin:
    def get_previous_selected_account_id(self) -> int:
        raw = self.get_setting("ui.previous_selected_account_id", 0)
        try:
            value = int(raw or 0)
        except (TypeError, ValueError, OverflowError):
            return 0
        return value if value > 0 else 0
    def import_comment_profile_between_accounts(
        self,
        *,
        source_account_id: object,
        target_account_id: object,
        mode: str,
    ) -> dict[str, Any]:
        source = _positive_account_id(source_account_id)
        target = _positive_account_id(target_account_id)
        if source == target:
            raise DatabaseError("Source and target accounts must be different")
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"replace", "fill"}:
            raise DatabaseError("Comment import mode must be replace or fill")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                active = conn.execute(
                    """SELECT 1 FROM comment_campaigns
                       WHERE account_id=?
                         AND status IN ('running','paused','network_wait','cycle_wait')
                       LIMIT 1""",
                    (target,),
                ).fetchone()
                if active is not None:
                    raise DatabaseError(
                        "Остановите кампанию целевого аккаунта перед импортом комментариев"
                    )
                source_row = conn.execute(
                    """SELECT text_1, text_2, text_3, text_4, text_5,
                              text_6, text_7, text_8, text_9, text_10
                       FROM account_comment_templates WHERE account_id=?""",
                    (source,),
                ).fetchone()
                if source_row is None:
                    raise DatabaseError("У предыдущего аккаунта нет комментариев")
                target_row = conn.execute(
                    """SELECT text_1, text_2, text_3, text_4, text_5,
                              text_6, text_7, text_8, text_9, text_10
                       FROM account_comment_templates WHERE account_id=?""",
                    (target,),
                ).fetchone()
                source_values = _normalized_slots(
                    [source_row[f"text_{index}"] for index in range(1, 11)]
                )
                target_values = _normalized_slots(
                    [target_row[f"text_{index}"] for index in range(1, 11)]
                    if target_row is not None
                    else []
                )
                if normalized_mode == "replace":
                    merged = list(source_values)
                else:
                    merged = list(target_values)
                    candidates = [
                        value
                        for value in source_values
                        if value and value not in set(merged)
                    ]
                    for index, value in enumerate(merged):
                        if not candidates:
                            break
                        if not value:
                            merged[index] = candidates.pop(0)
                active_values = _active_unique(merged)
                values = [value or None for value in merged]
                conn.execute(
                    """INSERT INTO account_comment_templates(
                           account_id, visible_count,
                           text_1, text_2, text_3, text_4, text_5,
                           text_6, text_7, text_8, text_9, text_10,
                           bag_fingerprint, bag_order_json, bag_position,
                           last_variant_index, last_used_at, updated_at)
                       VALUES(?, 10, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 0,
                              NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                       ON CONFLICT(account_id) DO UPDATE SET
                           visible_count=10,
                           text_1=excluded.text_1, text_2=excluded.text_2,
                           text_3=excluded.text_3, text_4=excluded.text_4,
                           text_5=excluded.text_5, text_6=excluded.text_6,
                           text_7=excluded.text_7, text_8=excluded.text_8,
                           text_9=excluded.text_9, text_10=excluded.text_10,
                           bag_fingerprint=excluded.bag_fingerprint,
                           bag_order_json='[]', bag_position=0,
                           last_variant_index=NULL,
                           last_used_at=CURRENT_TIMESTAMP,
                           updated_at=CURRENT_TIMESTAMP""",
                    (target, *values, _fingerprint(active_values)),
                )
                imported = sum(
                    1
                    for old, new in zip(target_values, merged)
                    if new and old != new
                )
                conn.execute(
                    """INSERT INTO logs(account_id, level, message, created_at)
                       VALUES(?, 'INFO', ?, CURRENT_TIMESTAMP)""",
                    (
                        target,
                        f"[Импорт] Комментарии скопированы из аккаунта {source}: "
                        f"режим={normalized_mode}, изменено={imported}",
                    ),
                )
                return {
                    "source_account_id": source,
                    "target_account_id": target,
                    "mode": normalized_mode,
                    "imported": imported,
                    "comments": merged,
                }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to import comments: {exc}") from exc
    def import_channels_between_accounts(
        self,
        *,
        source_account_id: object,
        target_account_id: object,
    ) -> dict[str, int]:
        source = _positive_account_id(source_account_id)
        target = _positive_account_id(target_account_id)
        if source == target:
            raise DatabaseError("Source and target accounts must be different")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                active = conn.execute(
                    """SELECT 1 FROM comment_campaigns
                       WHERE account_id=?
                         AND status IN ('running','paused','network_wait','cycle_wait')
                       LIMIT 1""",
                    (target,),
                ).fetchone()
                if active is not None:
                    raise DatabaseError(
                        "Остановите кампанию целевого аккаунта перед импортом каналов"
                    )
                source_rows = conn.execute(
                    """SELECT channel_id, username, title, target_kind, comment_mode,
                              linked_chat_id, linked_chat_title, peer_type
                       FROM channels
                       WHERE account_id=? AND target_kind IN ('channel','group')
                       ORDER BY id""",
                    (source,),
                ).fetchall()
                imported = 0
                existing = 0
                skipped = 0
                for row in source_rows:
                    channel_id = int(row["channel_id"] or 0)
                    if channel_id == 0:
                        skipped += 1
                        continue
                    found = conn.execute(
                        """SELECT 1 FROM channels
                           WHERE account_id=? AND channel_id=?""",
                        (target, channel_id),
                    ).fetchone()
                    if found is not None:
                        existing += 1
                        continue
                    conn.execute(
                        """INSERT INTO channels(
                               account_id, channel_id, username, title, target_kind,
                               comment_mode, linked_chat_id, linked_chat_title,
                               link_status, link_checked_at, last_sync_at,
                               last_comment_check_at, access_hash, peer_type,
                               negative_status, negative_until,
                               local_ban_reason, local_ban_peer_id, local_banned_at,
                               created_at)
                           VALUES(?, ?, ?, ?, ?, 'pending', ?, ?,
                                  'Импортировано; требуется повторная проверка доступа',
                                  NULL, NULL, NULL, NULL, ?, NULL, NULL,
                                  NULL, NULL, NULL, CURRENT_TIMESTAMP)""",
                        (
                            target,
                            channel_id,
                            row["username"],
                            row["title"],
                            row["target_kind"],
                            row["linked_chat_id"],
                            row["linked_chat_title"],
                            row["peer_type"],
                        ),
                    )
                    imported += 1
                conn.execute(
                    """INSERT INTO logs(account_id, level, message, created_at)
                       VALUES(?, 'INFO', ?, CURRENT_TIMESTAMP)""",
                    (
                        target,
                        f"[Импорт] Каналы скопированы из аккаунта {source}: "
                        f"импортировано={imported}, существовало={existing}, "
                        f"пропущено={skipped}. Участие и доступ будут проверены заново.",
                    ),
                )
                return {
                    "imported": imported,
                    "existing": existing,
                    "skipped": skipped,
                }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to import channels: {exc}") from exc

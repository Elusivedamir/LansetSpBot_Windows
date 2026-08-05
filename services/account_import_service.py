from __future__ import annotations

import hashlib
import json
from typing import Any

from storage.db_common import DatabaseError


class AccountImportService:
    """Explicit, account-isolated configuration import operations.

    Telegram-derived identity/access fields are never copied between accounts.
    """

    def __init__(self, database: Any) -> None:
        self.database = database

    @staticmethod
    def _account_id(value: object, *, field: str) -> int:
        if value is None:
            raw_value: str | bytes | bytearray | int | float = 0
        elif isinstance(value, (str, bytes, bytearray, int, float)):
            raw_value = value
        else:
            raise DatabaseError(f"Некорректный {field}")
        try:
            account_id = int(raw_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DatabaseError(f"Некорректный {field}") from exc
        if account_id <= 0:
            raise DatabaseError(f"Не выбран {field}")
        return int(account_id)

    def import_comments(
        self,
        *,
        source_account_id: object,
        target_account_id: object,
        mode: str = "replace",
    ) -> dict[str, Any]:
        source = self._account_id(source_account_id, field="аккаунт-источник")
        target = self._account_id(target_account_id, field="целевой аккаунт")
        if source == target:
            raise DatabaseError("Нельзя импортировать данные аккаунта в него же")
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"replace", "fill"}:
            raise DatabaseError("Режим импорта комментариев должен быть replace или fill")
        try:
            with self.database.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                source_exists = conn.execute(
                    "SELECT 1 FROM telegram_accounts WHERE telegram_account_id=?",
                    (source,),
                ).fetchone()
                target_exists = conn.execute(
                    "SELECT 1 FROM telegram_accounts WHERE telegram_account_id=?",
                    (target,),
                ).fetchone()
                if source_exists is None or target_exists is None:
                    raise DatabaseError("Источник или целевой аккаунт отсутствует")
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
                columns = ", ".join(f"text_{index}" for index in range(1, 11))
                source_row = conn.execute(
                    f"SELECT visible_count, {columns} FROM account_comment_templates "
                    "WHERE account_id=?",
                    (source,),
                ).fetchone()
                if source_row is None:
                    raise DatabaseError("У выбранного аккаунта нет комментариев")
                target_row = conn.execute(
                    f"SELECT visible_count, {columns} FROM account_comment_templates "
                    "WHERE account_id=?",
                    (target,),
                ).fetchone()
                source_values = [
                    str(source_row[f"text_{index}"] or "").strip()
                    for index in range(1, 11)
                ]
                target_values = (
                    [
                        str(target_row[f"text_{index}"] or "").strip()
                        for index in range(1, 11)
                    ]
                    if target_row is not None
                    else [""] * 10
                )
                if normalized_mode == "replace":
                    merged = list(source_values)
                    visible_count = max(1, min(10, int(source_row["visible_count"] or 10)))
                else:
                    merged = list(target_values)
                    existing = {value for value in merged if value}
                    candidates = [
                        value for value in source_values if value and value not in existing
                    ]
                    for index, value in enumerate(merged):
                        if not candidates:
                            break
                        if not value:
                            merged[index] = candidates.pop(0)
                    target_visible = int(target_row["visible_count"] or 1) if target_row else 1
                    source_visible = int(source_row["visible_count"] or 1)
                    visible_count = max(1, min(10, max(target_visible, source_visible)))
                active_values: list[str] = []
                seen: set[str] = set()
                for value in merged:
                    if value and value not in seen:
                        seen.add(value)
                        active_values.append(value)
                raw = json.dumps(
                    active_values, ensure_ascii=False, separators=(",", ":")
                )
                fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                values = [value or None for value in merged]
                conn.execute(
                    """INSERT INTO account_comment_templates(
                           account_id, visible_count,
                           text_1, text_2, text_3, text_4, text_5,
                           text_6, text_7, text_8, text_9, text_10,
                           bag_fingerprint, bag_order_json, bag_position,
                           last_variant_index, last_used_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 0,
                              NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                       ON CONFLICT(account_id) DO UPDATE SET
                           visible_count=excluded.visible_count,
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
                    (target, visible_count, *values, fingerprint),
                )
                imported = sum(
                    1
                    for old, new_value in zip(target_values, merged)
                    if new_value and old != new_value
                )
                conn.execute(
                    """INSERT INTO logs(account_id, level, message, created_at)
                       VALUES(?, 'INFO', ?, CURRENT_TIMESTAMP)""",
                    (
                        target,
                        f"[Импорт] Конфигурация комментариев скопирована из аккаунта "
                        f"{source}: изменено={imported}, видимых полей={visible_count}. "
                        "История отправок, ledger и runtime-позиция случайного мешка не перенесены.",
                    ),
                )
                return {
                    "imported": imported,
                    "visible_count": visible_count,
                    "mode": normalized_mode,
                }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Не удалось импортировать комментарии: {exc}") from exc

    def import_channels(
        self,
        *,
        source_account_id: object,
        target_account_id: object,
    ) -> dict[str, int]:
        source = self._account_id(source_account_id, field="аккаунт-источник")
        target = self._account_id(target_account_id, field="целевой аккаунт")
        if source == target:
            raise DatabaseError("Нельзя импортировать данные аккаунта в него же")

        try:
            with self.database.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")

                target_exists = conn.execute(
                    "SELECT 1 FROM telegram_accounts WHERE telegram_account_id=?",
                    (target,),
                ).fetchone()
                source_exists = conn.execute(
                    "SELECT 1 FROM telegram_accounts WHERE telegram_account_id=?",
                    (source,),
                ).fetchone()
                if source_exists is None or target_exists is None:
                    raise DatabaseError("Источник или целевой аккаунт отсутствует")

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
                    """SELECT channel_id, username, title, target_kind, comment_mode
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
                    target_kind = str(row["target_kind"] or "channel")
                    # comment_mode is partly a Telegram-derived classification
                    # (linked discussion/direct group), so it must be rebuilt for
                    # the target session rather than trusted from the source.
                    comment_mode = "pending"
                    conn.execute(
                        """INSERT INTO channels(
                               account_id, channel_id, username, title, target_kind,
                               comment_mode, linked_chat_id, linked_chat_title,
                               link_status, link_checked_at, last_sync_at,
                               last_comment_check_at, access_hash, peer_type,
                               negative_status, negative_until,
                               local_ban_reason, local_ban_peer_id, local_banned_at,
                               created_at)
                           VALUES(?, ?, ?, ?, ?, ?, NULL, NULL,
                                  'Импортировано; требуется повторная проверка доступа',
                                  NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                                  NULL, NULL, NULL, CURRENT_TIMESTAMP)""",
                        (
                            target,
                            channel_id,
                            row["username"],
                            row["title"],
                            target_kind,
                            comment_mode,
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
                        f"пропущено={skipped}. Связки, участие, права, access hash "
                        "и доступность будут проверены заново.",
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
            raise DatabaseError(f"Не удалось импортировать каналы: {exc}") from exc

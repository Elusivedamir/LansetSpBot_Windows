from __future__ import annotations

import hashlib
from typing import Any

from core.openai_settings import (
    SOURCE_PREWRITTEN,
    CommentGenerationSettings,
    normalize_comment_source,
)
from storage.db_common import DatabaseError

_ALLOWED_DRAFT_STATUSES = {
    "generated",
    "generation_failed",
    "sending",
    "sent",
    "failed",
    "uncertain",
    "cancelled",
}


class OpenAIDraftRepositoryMixin:
    def save_campaign_comment_settings(
        self,
        *,
        campaign_id: int,
        account_id: int,
        comment_source: str,
        settings: CommentGenerationSettings | None = None,
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        source = normalize_comment_source(comment_source)
        snapshot = settings or CommentGenerationSettings()
        try:
            with self.get_connection() as conn:
                conn.execute(
                    """INSERT INTO campaign_comment_settings(
                           campaign_id, account_id, comment_source, model,
                           system_prompt, max_words, temperature, timeout_seconds,
                           max_generation_attempts, manual_approval_required, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
                       ON CONFLICT(campaign_id) DO UPDATE SET
                           account_id=excluded.account_id,
                           comment_source=excluded.comment_source,
                           model=excluded.model,
                           system_prompt=excluded.system_prompt,
                           max_words=excluded.max_words,
                           temperature=excluded.temperature,
                           timeout_seconds=excluded.timeout_seconds,
                           max_generation_attempts=excluded.max_generation_attempts,
                           manual_approval_required=0,
                           updated_at=CURRENT_TIMESTAMP""",
                    (
                        int(campaign_id),
                        int(account_id),
                        source,
                        snapshot.model if source != SOURCE_PREWRITTEN else None,
                        str(system_prompt or "") if source != SOURCE_PREWRITTEN else None,
                        snapshot.max_words if source != SOURCE_PREWRITTEN else None,
                        snapshot.temperature if source != SOURCE_PREWRITTEN else None,
                        snapshot.timeout_seconds if source != SOURCE_PREWRITTEN else None,
                        snapshot.max_generation_attempts if source != SOURCE_PREWRITTEN else None,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM campaign_comment_settings WHERE campaign_id=?",
                    (int(campaign_id),),
                ).fetchone()
                return dict(row) if row else {}
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to save campaign comment settings: {exc}") from exc

    def get_campaign_comment_settings(self, campaign_id: int) -> dict[str, Any]:
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM campaign_comment_settings WHERE campaign_id=?",
                    (int(campaign_id),),
                ).fetchone()
                if row:
                    return dict(row)
                return {
                    "campaign_id": int(campaign_id),
                    "comment_source": SOURCE_PREWRITTEN,
                    "manual_approval_required": 0,
                }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read campaign comment settings: {exc}") from exc

    def upsert_generated_comment_draft(
        self,
        *,
        account_id: int,
        campaign_id: int | None,
        source_channel_id: int,
        source_post_id: int,
        linked_chat_id: int | None,
        discussion_message_id: int | None,
        post_text: str,
        generated_text: str | None,
        status: str,
        model: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        normalized_status = str(status or "")
        if normalized_status not in _ALLOWED_DRAFT_STATUSES:
            raise DatabaseError(f"Invalid generated draft status: {status}")
        owner = int(account_id)
        channel_id = int(source_channel_id)
        post_id = int(source_post_id)
        if owner <= 0 or channel_id == 0 or post_id <= 0:
            raise DatabaseError("Generated draft requires account/channel/post ids")
        source = str(post_text or "").replace("\x00", "")[:50_000]
        text = None if generated_text is None else str(generated_text).strip()
        source_hash = hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()
        try:
            with self.get_connection() as conn:
                conn.execute(
                    """INSERT INTO generated_comment_drafts(
                           account_id, campaign_id, source_channel_id, source_post_id,
                           linked_chat_id, discussion_message_id, post_text,
                           post_text_hash, generated_text, edited_text, status, model,
                           word_count, error_code, error_message, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(account_id, source_channel_id, source_post_id)
                       DO UPDATE SET
                           campaign_id=excluded.campaign_id,
                           linked_chat_id=excluded.linked_chat_id,
                           discussion_message_id=excluded.discussion_message_id,
                           post_text=excluded.post_text,
                           post_text_hash=excluded.post_text_hash,
                           generated_text=excluded.generated_text,
                           edited_text=NULL,
                           status=excluded.status,
                           model=excluded.model,
                           word_count=excluded.word_count,
                           error_code=excluded.error_code,
                           error_message=excluded.error_message,
                           sent_at=CASE WHEN excluded.status='sent' THEN CURRENT_TIMESTAMP ELSE NULL END,
                           updated_at=CURRENT_TIMESTAMP""",
                    (
                        owner,
                        campaign_id,
                        channel_id,
                        post_id,
                        linked_chat_id,
                        discussion_message_id,
                        source,
                        source_hash,
                        text,
                        normalized_status,
                        model,
                        len(text.split()) if text else 0,
                        error_code,
                        None if error_message is None else str(error_message)[:1000],
                    ),
                )
                row = conn.execute(
                    """SELECT * FROM generated_comment_drafts
                       WHERE account_id=? AND source_channel_id=? AND source_post_id=?""",
                    (owner, channel_id, post_id),
                ).fetchone()
                return dict(row) if row else {}
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to save generated comment draft: {exc}") from exc

    def get_generated_comment_draft_for_post(
        self, *, account_id: int, source_channel_id: int, source_post_id: int
    ) -> dict[str, Any] | None:
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT * FROM generated_comment_drafts
                       WHERE account_id=? AND source_channel_id=? AND source_post_id=?""",
                    (int(account_id), int(source_channel_id), int(source_post_id)),
                ).fetchone()
                return dict(row) if row else None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read generated comment draft: {exc}") from exc

    def list_generated_comment_drafts(
        self,
        *,
        account_id: int,
        statuses: tuple[str, ...] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        owner = int(account_id)
        if owner <= 0:
            return []
        normalized = tuple(str(item) for item in (statuses or ()))
        for status in normalized:
            if status not in _ALLOWED_DRAFT_STATUSES:
                raise DatabaseError(f"Invalid generated draft status: {status}")
        try:
            with self.get_connection() as conn:
                if normalized:
                    placeholders = ",".join("?" for _ in normalized)
                    rows = conn.execute(
                        f"""SELECT * FROM generated_comment_drafts
                            WHERE account_id=? AND status IN ({placeholders})
                            ORDER BY updated_at DESC, id DESC LIMIT ?""",
                        (owner, *normalized, max(1, min(500, int(limit)))),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT * FROM generated_comment_drafts
                           WHERE account_id=? ORDER BY updated_at DESC, id DESC LIMIT ?""",
                        (owner, max(1, min(500, int(limit)))),
                    ).fetchall()
                return [dict(row) for row in rows]
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to list generated comment drafts: {exc}") from exc

    def mark_generated_comment_draft_status(
        self,
        draft_id: int,
        *,
        account_id: int,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        normalized_status = str(status or "")
        if normalized_status not in _ALLOWED_DRAFT_STATUSES:
            raise DatabaseError(f"Invalid generated draft status: {status}")
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE generated_comment_drafts
                       SET status=?, error_code=?, error_message=?,
                           sent_at=CASE WHEN ?='sent' THEN CURRENT_TIMESTAMP ELSE sent_at END,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND account_id=?""",
                    (
                        normalized_status,
                        error_code,
                        None if error_message is None else str(error_message)[:1000],
                        normalized_status,
                        int(draft_id),
                        int(account_id),
                    ),
                )
                return int(cursor.rowcount or 0) == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to update generated comment draft: {exc}") from exc

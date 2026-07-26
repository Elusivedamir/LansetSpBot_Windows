from __future__ import annotations

import hashlib
import json
import random
from typing import Any, TYPE_CHECKING

from core.config import (
    DEFAULT_COMMENT_VARIANTS,
    MAX_COMMENT_VARIANTS,
)
from storage.db_common import DatabaseError, resolve_account_id


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


def _normalized_slots(comments: list[str] | tuple[str, ...] | None) -> list[str]:
    values = [
        str(item or "").strip() for item in list(comments or [])[:MAX_COMMENT_VARIANTS]
    ]
    values += [""] * (MAX_COMMENT_VARIANTS - len(values))
    return values


def _active_unique_variants(comments: list[str] | tuple[str, ...] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for text in _normalized_slots(comments):
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _variant_fingerprint(variants: list[str]) -> str:
    payload = json.dumps(variants, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _new_bag(size: int, last_variant_index: int | None, rng: Any) -> list[int]:
    order = list(range(size))
    rng.shuffle(order)
    if size > 1 and last_variant_index is not None and order[0] == last_variant_index:
        swap_index = next(
            (
                index
                for index, value in enumerate(order[1:], start=1)
                if value != last_variant_index
            ),
            None,
        )
        if swap_index is not None:
            order[0], order[swap_index] = order[swap_index], order[0]
    return order


def _parse_bag_order(raw: object) -> list[int]:
    try:
        parsed = json.loads(str(raw or "[]"))
        return [int(value) for value in parsed]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _remap_bag_after_edit(
    *,
    old_variants: list[str],
    new_variants: list[str],
    old_order: list[int],
    old_position: int,
    old_last_index: int | None,
    rng: Any,
) -> tuple[list[int], int, int | None]:
    """Preserve the unfinished cycle when the ten text fields are edited.

    Mapping is text-based rather than index-based, so reordering fields does not
    repeat an already used text. Newly added texts join the unfinished part of
    the current cycle. If the old cycle was already exhausted, a fresh shuffled
    cycle is created while keeping the no-immediate-repeat invariant.
    """

    index_by_text = {text: index for index, text in enumerate(new_variants)}
    last_text = (
        old_variants[old_last_index]
        if old_last_index is not None and 0 <= old_last_index < len(old_variants)
        else None
    )
    new_last_index = index_by_text.get(last_text) if last_text is not None else None

    valid_old_order = len(old_order) == len(old_variants) and sorted(old_order) == list(
        range(len(old_variants))
    )
    if not valid_old_order:
        order = _new_bag(len(new_variants), new_last_index, rng)
        return order, 0, new_last_index

    position = max(0, min(int(old_position), len(old_order)))
    consumed_texts = {
        old_variants[index]
        for index in old_order[:position]
        if 0 <= index < len(old_variants)
    }
    remaining_texts: list[str] = []
    for index in old_order[position:]:
        if not 0 <= index < len(old_variants):
            continue
        text = old_variants[index]
        if text in index_by_text and text not in remaining_texts:
            remaining_texts.append(text)

    additions = [
        text
        for text in new_variants
        if text not in old_variants and text not in remaining_texts
    ]
    rng.shuffle(additions)
    pending_texts = remaining_texts + additions

    if not pending_texts:
        order = _new_bag(len(new_variants), new_last_index, rng)
        return order, 0, new_last_index

    pending_set = set(pending_texts)
    consumed_indices = [
        index_by_text[text]
        for text in new_variants
        if text in consumed_texts and text not in pending_set
    ]
    # Texts retained from the old profile but absent from both sets are treated
    # as already consumed in this cycle. This keeps a complete permutation for
    # the durable bag validation performed during reservation.
    consumed_indices.extend(
        index_by_text[text]
        for text in new_variants
        if text not in pending_set and index_by_text[text] not in consumed_indices
    )
    pending_indices = [index_by_text[text] for text in pending_texts]
    order = consumed_indices + pending_indices
    return order, len(consumed_indices), new_last_index


class CommentVariantRepositoryMixin(_MixinHost):
    """Per-account comment templates and a crash-safe shuffled bag."""

    def get_account_comment_profile(self, account_id=None, *, touch: bool = False):
        owner_account_id = resolve_account_id(self, account_id)
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT account_id, visible_count,
                              text_1, text_2, text_3, text_4, text_5,
                              text_6, text_7, text_8, text_9, text_10,
                              bag_fingerprint, bag_order_json, bag_position,
                              last_variant_index, last_used_at, updated_at
                       FROM account_comment_templates WHERE account_id=?""",
                    (owner_account_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """INSERT INTO account_comment_templates(
                               account_id, visible_count, last_used_at, updated_at)
                           VALUES(?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                        (owner_account_id, DEFAULT_COMMENT_VARIANTS),
                    )
                    row = conn.execute(
                        """SELECT account_id, visible_count,
                                  text_1, text_2, text_3, text_4, text_5,
                                  text_6, text_7, text_8, text_9, text_10,
                                  bag_fingerprint, bag_order_json, bag_position,
                                  last_variant_index, last_used_at, updated_at
                           FROM account_comment_templates WHERE account_id=?""",
                        (owner_account_id,),
                    ).fetchone()
                elif touch:
                    conn.execute(
                        """UPDATE account_comment_templates
                           SET last_used_at=CURRENT_TIMESTAMP
                           WHERE account_id=?""",
                        (owner_account_id,),
                    )
                result = dict(row)
                result["comments"] = [
                    str(result.get(f"text_{index}") or "")
                    for index in range(1, MAX_COMMENT_VARIANTS + 1)
                ]
                result["visible_count"] = MAX_COMMENT_VARIANTS
                return result
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to read account comment profile: {exc}"
            ) from exc

    def save_account_comment_profile(
        self,
        comments,
        *,
        visible_count=None,
        account_id=None,
    ):
        owner_account_id = resolve_account_id(self, account_id)
        slots = _normalized_slots(list(comments or []))
        del visible_count
        count = MAX_COMMENT_VARIANTS
        active = _active_unique_variants(slots)
        new_fingerprint = _variant_fingerprint(active)
        generator = random.SystemRandom()
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    """SELECT text_1, text_2, text_3, text_4, text_5,
                                      text_6, text_7, text_8, text_9, text_10,
                                      bag_fingerprint, bag_order_json, bag_position,
                                      last_variant_index
                               FROM account_comment_templates
                               WHERE account_id=?""",
                    (owner_account_id,),
                ).fetchone()

                bag_order: list[int] = []
                bag_position = 0
                last_variant_index: int | None = None
                if existing is not None:
                    old_slots = [
                        str(existing[f"text_{index}"] or "")
                        for index in range(1, MAX_COMMENT_VARIANTS + 1)
                    ]
                    old_active = _active_unique_variants(old_slots)
                    stored_fingerprint = str(existing["bag_fingerprint"] or "")
                    old_order = _parse_bag_order(existing["bag_order_json"])
                    old_position = max(0, int(existing["bag_position"] or 0))
                    raw_last = existing["last_variant_index"]
                    old_last_index = int(raw_last) if raw_last is not None else None
                    if stored_fingerprint == new_fingerprint:
                        bag_order = old_order
                        bag_position = old_position
                        last_variant_index = old_last_index
                    elif active:
                        (
                            bag_order,
                            bag_position,
                            last_variant_index,
                        ) = _remap_bag_after_edit(
                            old_variants=old_active,
                            new_variants=active,
                            old_order=old_order,
                            old_position=old_position,
                            old_last_index=old_last_index,
                            rng=generator,
                        )

                values = [slots[index] or None for index in range(MAX_COMMENT_VARIANTS)]
                conn.execute(
                    """INSERT INTO account_comment_templates(
                           account_id, visible_count,
                           text_1, text_2, text_3, text_4, text_5,
                           text_6, text_7, text_8, text_9, text_10,
                           bag_fingerprint, bag_order_json, bag_position,
                           last_variant_index, last_used_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                       ON CONFLICT(account_id) DO UPDATE SET
                           visible_count=excluded.visible_count,
                           text_1=excluded.text_1, text_2=excluded.text_2,
                           text_3=excluded.text_3, text_4=excluded.text_4,
                           text_5=excluded.text_5, text_6=excluded.text_6,
                           text_7=excluded.text_7, text_8=excluded.text_8,
                           text_9=excluded.text_9, text_10=excluded.text_10,
                           bag_fingerprint=excluded.bag_fingerprint,
                           bag_order_json=excluded.bag_order_json,
                           bag_position=excluded.bag_position,
                           last_variant_index=excluded.last_variant_index,
                           last_used_at=CURRENT_TIMESTAMP,
                           updated_at=CURRENT_TIMESTAMP""",
                    (
                        owner_account_id,
                        count,
                        *values,
                        new_fingerprint,
                        json.dumps(bag_order, separators=(",", ":")),
                        bag_position,
                        last_variant_index,
                    ),
                )
            return self.get_account_comment_profile(owner_account_id)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to save account comment profile: {exc}"
            ) from exc

    def import_previous_account_comment_profile(self, account_id=None):
        """Copy only texts from the most recently used different account."""

        owner_account_id = resolve_account_id(self, account_id)
        if owner_account_id <= 0:
            raise DatabaseError(
                "Authorize a Telegram account before importing comment variants"
            )
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                source = conn.execute(
                    """SELECT account_id, visible_count,
                              text_1, text_2, text_3, text_4, text_5,
                              text_6, text_7, text_8, text_9, text_10
                       FROM account_comment_templates
                       WHERE account_id<>? AND account_id>0
                         AND COALESCE(text_1,'') || COALESCE(text_2,'') ||
                             COALESCE(text_3,'') || COALESCE(text_4,'') ||
                             COALESCE(text_5,'') || COALESCE(text_6,'') ||
                             COALESCE(text_7,'') || COALESCE(text_8,'') ||
                             COALESCE(text_9,'') || COALESCE(text_10,'') <> ''
                       ORDER BY last_used_at DESC, updated_at DESC, account_id DESC
                       LIMIT 1""",
                    (owner_account_id,),
                ).fetchone()
                if source is None:
                    return None
                comments = [
                    str(source[f"text_{index}"] or "")
                    for index in range(1, MAX_COMMENT_VARIANTS + 1)
                ]
                visible_count = MAX_COMMENT_VARIANTS
                active = _active_unique_variants(comments)
                fingerprint = _variant_fingerprint(active)
                values = [text or None for text in comments]
                conn.execute(
                    """INSERT INTO account_comment_templates(
                           account_id, visible_count,
                           text_1, text_2, text_3, text_4, text_5,
                           text_6, text_7, text_8, text_9, text_10,
                           bag_fingerprint, bag_order_json, bag_position,
                           last_variant_index, last_used_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 0, NULL,
                              CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
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
                    (owner_account_id, visible_count, *values, fingerprint),
                )
                return {
                    "source_account_id": int(source["account_id"]),
                    "account_id": owner_account_id,
                    "visible_count": visible_count,
                    "comments": comments,
                }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to import previous comment profile: {exc}"
            ) from exc

    def reserve_comment_variant_for_slot(
        self,
        slot_id,
        task_id,
        *,
        account_id,
        variants,
        rng=None,
    ):
        """Select and persist one bag item before any Telegram mutation.

        Re-entering the same queued/running slot returns the already persisted
        text. The bag cursor and slot reservation advance in one transaction, so
        a crash cannot make the slot pick another comment after restart.
        """

        owner_account_id = resolve_account_id(self, account_id)
        active = _active_unique_variants(list(variants or []))
        if not active:
            raise DatabaseError("At least one non-empty comment variant is required")
        fingerprint = _variant_fingerprint(active)
        generator = rng if rng is not None else random.SystemRandom()
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                slot = conn.execute(
                    """SELECT s.selected_text, s.selected_variant_index, s.status,
                              s.task_id, c.account_id
                       FROM comment_schedule s
                       JOIN comment_campaigns c ON c.id=s.campaign_id
                       WHERE s.id=?""",
                    (int(slot_id),),
                ).fetchone()
                if slot is None:
                    raise DatabaseError("Comment campaign slot does not exist")
                if int(slot["account_id"] or 0) != owner_account_id:
                    raise DatabaseError(
                        "Comment slot belongs to another Telegram account"
                    )
                if str(slot["status"] or "") not in {"queued", "running"}:
                    raise DatabaseError(
                        "Comment slot is not available for variant reservation"
                    )
                if slot["task_id"] is not None and int(slot["task_id"]) != int(task_id):
                    raise DatabaseError("Comment slot is bound to another queue task")
                existing_text = str(slot["selected_text"] or "").strip()
                if existing_text:
                    return {
                        "text": existing_text,
                        "variant_index": int(slot["selected_variant_index"] or 0),
                        "reused": True,
                    }

                profile = conn.execute(
                    """SELECT bag_fingerprint, bag_order_json, bag_position,
                              last_variant_index
                       FROM account_comment_templates WHERE account_id=?""",
                    (owner_account_id,),
                ).fetchone()
                if profile is None:
                    slots = _normalized_slots(active)
                    values = [text or None for text in slots]
                    conn.execute(
                        """INSERT INTO account_comment_templates(
                               account_id, visible_count,
                               text_1, text_2, text_3, text_4, text_5,
                               text_6, text_7, text_8, text_9, text_10,
                               bag_fingerprint, bag_order_json, bag_position,
                               last_variant_index, last_used_at, updated_at)
                           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 0, NULL,
                                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                        (
                            owner_account_id,
                            MAX_COMMENT_VARIANTS,
                            *values,
                            fingerprint,
                        ),
                    )
                    bag_order: list[int] = []
                    bag_position = 0
                    last_index = None
                else:
                    stored_fingerprint = str(profile["bag_fingerprint"] or "")
                    try:
                        parsed = json.loads(str(profile["bag_order_json"] or "[]"))
                        bag_order = [int(value) for value in parsed]
                    except (TypeError, ValueError, json.JSONDecodeError):
                        bag_order = []
                    bag_position = max(0, int(profile["bag_position"] or 0))
                    raw_last = profile["last_variant_index"]
                    last_index = int(raw_last) if raw_last is not None else None
                    valid_order = len(bag_order) == len(active) and sorted(
                        bag_order
                    ) == list(range(len(active)))
                    if stored_fingerprint != fingerprint or not valid_order:
                        bag_order = []
                        bag_position = 0
                        last_index = None

                if bag_position >= len(bag_order):
                    bag_order = _new_bag(len(active), last_index, generator)
                    bag_position = 0
                variant_index = int(bag_order[bag_position])
                selected_text = active[variant_index]
                next_position = bag_position + 1
                cursor = conn.execute(
                    """UPDATE comment_schedule
                       SET selected_text=?, selected_variant_index=?
                       WHERE id=? AND task_id=? AND status IN ('queued','running')
                         AND COALESCE(selected_text,'')=''""",
                    (selected_text, variant_index, int(slot_id), int(task_id)),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError(
                        "Comment variant reservation lost the slot race"
                    )
                conn.execute(
                    """UPDATE account_comment_templates
                       SET bag_fingerprint=?, bag_order_json=?, bag_position=?,
                           last_variant_index=?, last_used_at=CURRENT_TIMESTAMP,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE account_id=?""",
                    (
                        fingerprint,
                        json.dumps(bag_order, separators=(",", ":")),
                        next_position,
                        variant_index,
                        owner_account_id,
                    ),
                )
                return {
                    "text": selected_text,
                    "variant_index": variant_index,
                    "reused": False,
                }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to reserve comment variant: {exc}") from exc

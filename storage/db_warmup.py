from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from storage.db_common import DatabaseError

if TYPE_CHECKING:  # pragma: no cover
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


ACTIVE_PAIR_STATES = ("running", "paused")
ACTIVE_TASK_STATES = ("pending", "running", "processing", "paused")
MAX_WARMUP_ACCOUNTS = 40


def _positive(value: object, label: str) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DatabaseError(f"{label} must be an integer") from exc
    if parsed <= 0:
        raise DatabaseError(f"{label} must be positive")
    return parsed


class WarmupRepositoryMixin(_MixinHost):
    """Durable, account-scoped warmup workflow repository.

    Every mutating method reserves SQLite with ``BEGIN IMMEDIATE`` before it
    checks invariants. GUI threads and the QueueWorker therefore never share a
    connection and cannot create two active pairs for one account.
    """

    def list_warmup_account_states(self) -> list[dict[str, Any]]:
        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    """SELECT account_id, status, active_pair_id, weeks_completed,
                              transferred_at, updated_at
                       FROM warmup_accounts ORDER BY account_id"""
                ).fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            raise DatabaseError(f"Failed to list warmup account states: {exc}") from exc

    def list_warmup_pairs(self, *, include_archived: bool = True) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE p.status<>'archived'"
        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    f"""SELECT p.*,
                               a.display_name AS account_a_name,
                               a.username AS account_a_username,
                               a.phone_masked AS account_a_phone,
                               b.display_name AS account_b_name,
                               b.username AS account_b_username,
                               b.phone_masked AS account_b_phone,
                               COALESCE(SUM(CASE WHEN s.status IN ('done','skipped') THEN 1 ELSE 0 END),0)
                                   AS finished_steps,
                               COALESCE(SUM(CASE WHEN s.status='uncertain' THEN 1 ELSE 0 END),0)
                                   AS uncertain_steps,
                               COALESCE(SUM(CASE WHEN s.status='failed' THEN 1 ELSE 0 END),0)
                                   AS failed_steps
                        FROM warmup_pairs p
                        JOIN telegram_accounts a ON a.telegram_account_id=p.account_a_id
                        JOIN telegram_accounts b ON b.telegram_account_id=p.account_b_id
                        LEFT JOIN warmup_steps s
                          ON s.pair_id=p.id AND s.week_number=p.week_number
                        {where}
                        GROUP BY p.id
                        ORDER BY CASE p.status
                            WHEN 'running' THEN 0 WHEN 'paused' THEN 1
                            WHEN 'completed' THEN 2 ELSE 3 END,
                            p.updated_at DESC, p.id DESC"""
                ).fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            raise DatabaseError(f"Failed to list warmup pairs: {exc}") from exc

    def get_warmup_pair(self, pair_id: object) -> dict[str, Any] | None:
        owner = _positive(pair_id, "pair_id")
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM warmup_pairs WHERE id=?", (owner,)
                ).fetchone()
                return dict(row) if row else None
        except Exception as exc:
            raise DatabaseError(f"Failed to read warmup pair: {exc}") from exc

    def list_active_warmup_pairs(self) -> list[dict[str, Any]]:
        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    """SELECT * FROM warmup_pairs
                       WHERE status IN ('running','paused') ORDER BY id"""
                ).fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            raise DatabaseError(f"Failed to list active warmup pairs: {exc}") from exc

    def list_warmup_groups(self) -> list[dict[str, Any]]:
        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    """SELECT g.*,
                              COUNT(ga.account_id) AS account_count,
                              COALESCE(SUM(CASE WHEN ga.membership_state='joined' THEN 1 ELSE 0 END),0)
                                  AS joined_count
                       FROM warmup_groups g
                       LEFT JOIN warmup_group_accounts ga ON ga.group_id=g.id
                       GROUP BY g.id ORDER BY g.enabled DESC, g.id DESC"""
                ).fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            raise DatabaseError(f"Failed to list warmup groups: {exc}") from exc

    def add_warmup_group(self, chat_ref: str, title: str | None = None) -> dict[str, Any]:
        clean_ref = " ".join(str(chat_ref or "").split()).strip()
        if not clean_ref or len(clean_ref) > 512:
            raise DatabaseError("Некорректная ссылка или username группы")
        clean_title = " ".join(str(title or clean_ref).split())[:160]
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """INSERT INTO warmup_groups(chat_ref,title,enabled,updated_at)
                       VALUES(?,?,1,CURRENT_TIMESTAMP)
                       ON CONFLICT(chat_ref) DO UPDATE SET
                           title=excluded.title, enabled=1,
                           updated_at=CURRENT_TIMESTAMP""",
                    (clean_ref, clean_title),
                )
                row = conn.execute(
                    "SELECT * FROM warmup_groups WHERE chat_ref=?", (clean_ref,)
                ).fetchone()
                if row is None:
                    raise DatabaseError("Warmup group was not persisted")
                return dict(row)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to add warmup group: {exc}") from exc

    def remove_warmup_group(self, group_id: object) -> bool:
        owner = _positive(group_id, "group_id")
        try:
            with self.get_connection() as conn:
                cursor = conn.execute("DELETE FROM warmup_groups WHERE id=?", (owner,))
                return int(cursor.rowcount or 0) == 1
        except Exception as exc:
            raise DatabaseError(f"Failed to remove warmup group: {exc}") from exc

    def _ensure_pair_accounts_available(self, conn, account_ids: tuple[int, int]) -> None:
        placeholders = ",".join("?" for _ in account_ids)
        rows = conn.execute(
            f"""SELECT telegram_account_id, authorized, stopped
                FROM telegram_accounts
                WHERE telegram_account_id IN ({placeholders})""",
            account_ids,
        ).fetchall()
        found = {int(row["telegram_account_id"]): dict(row) for row in rows}
        if set(found) != set(account_ids):
            raise DatabaseError("Один из Telegram-аккаунтов не найден")
        for account_id in account_ids:
            row = found[account_id]
            if not bool(row.get("authorized")):
                raise DatabaseError("Один из Telegram-аккаунтов не авторизован")
            if bool(row.get("stopped")):
                raise DatabaseError("Сначала возобновите остановленный аккаунт")
            state = conn.execute(
                "SELECT status, active_pair_id FROM warmup_accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if state is not None:
                if str(state["status"] or "") == "transferred":
                    raise DatabaseError(
                        "Аккаунт уже перенесён в кампанию и недоступен для новой связки"
                    )
                if state["active_pair_id"] is not None:
                    raise DatabaseError("Аккаунт уже участвует в активной связке")
            campaign = self._active_campaign_in_transaction(conn, account_id)
            if campaign is not None:
                raise DatabaseError(
                    "Для аккаунта уже запущена кампания. Остановите её перед прогревом."
                )

        active_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM warmup_accounts WHERE status='active'"
            ).fetchone()[0]
            or 0
        )
        newly_active = sum(
            1
            for account_id in account_ids
            if conn.execute(
                "SELECT 1 FROM warmup_accounts WHERE account_id=? AND status='active'",
                (account_id,),
            ).fetchone()
            is None
        )
        if active_count + newly_active > MAX_WARMUP_ACCOUNTS:
            raise DatabaseError("Во вкладке «Прогрев» разрешено не более 40 аккаунтов")

    @staticmethod
    def _insert_steps(conn, pair_id: int, week_number: int, steps: Iterable[Mapping[str, Any]]) -> int:
        count = 0
        for raw in steps:
            item = dict(raw)
            conn.execute(
                """INSERT INTO warmup_steps(
                       pair_id, week_number, sequence_no, day_number, scenario_key,
                       action, actor_account_id, target_account_id, message_text,
                       typing_seconds, reply_to_previous, posts_to_read, should_react,
                       scheduled_at, status, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',CURRENT_TIMESTAMP)""",
                (
                    int(pair_id),
                    int(week_number),
                    int(item["sequence_no"]),
                    int(item["day_number"]),
                    str(item["scenario_key"]),
                    str(item["action"]),
                    int(item["actor_account_id"]),
                    (
                        int(item["target_account_id"])
                        if item.get("target_account_id") is not None
                        else None
                    ),
                    item.get("message_text"),
                    int(item.get("typing_seconds") or 0),
                    1 if bool(item.get("reply_to_previous")) else 0,
                    int(item.get("posts_to_read") or 0),
                    1 if bool(item.get("should_react")) else 0,
                    str(item["scheduled_at"]),
                ),
            )
            count += 1
        return count

    def create_warmup_pair(
        self,
        *,
        account_a_id: object,
        account_b_id: object,
        profile: Mapping[str, Any],
        steps: Iterable[Mapping[str, Any]],
        owner_token_a: str,
        owner_token_b: str,
        started_at: str,
        ends_at: str,
    ) -> dict[str, Any]:
        account_a = _positive(account_a_id, "account_a_id")
        account_b = _positive(account_b_id, "account_b_id")
        if account_a == account_b:
            raise DatabaseError("Для связки нужны два разных аккаунта")
        profile_data = dict(profile)
        step_values = [dict(step) for step in steps]
        if not step_values:
            raise DatabaseError("План прогрева пуст")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                self._ensure_pair_accounts_available(conn, (account_a, account_b))
                cursor = conn.execute(
                    """INSERT INTO warmup_pairs(
                           account_a_id, account_b_id, status, week_number,
                           profile_seed, day_order, dialogue_windows,
                           reply_min_seconds, reply_max_seconds,
                           typing_min_seconds, typing_max_seconds,
                           group_visits_per_day, posts_min, posts_max,
                           reaction_probability_percent,
                           private_reaction_probability_percent, active_start_hour,
                           active_end_hour, owner_token_a, owner_token_b,
                           total_steps, started_at, ends_at, updated_at)
                       VALUES(?,?,'running',1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (
                        account_a,
                        account_b,
                        str(profile_data["seed"]),
                        str(profile_data["day_order"]),
                        int(profile_data["dialogue_windows"]),
                        int(profile_data["reply_min_seconds"]),
                        int(profile_data["reply_max_seconds"]),
                        int(profile_data["typing_min_seconds"]),
                        int(profile_data["typing_max_seconds"]),
                        int(profile_data["group_visits_per_day"]),
                        int(profile_data["posts_min"]),
                        int(profile_data["posts_max"]),
                        int(profile_data["reaction_probability_percent"]),
                        int(profile_data["private_reaction_probability_percent"]),
                        int(profile_data["active_start_hour"]),
                        int(profile_data["active_end_hour"]),
                        str(owner_token_a),
                        str(owner_token_b),
                        len(step_values),
                        str(started_at),
                        str(ends_at),
                    ),
                )
                pair_id = int(cursor.lastrowid or 0)
                if pair_id <= 0:
                    raise DatabaseError("Warmup pair was not created")
                inserted = self._insert_steps(conn, pair_id, 1, step_values)
                if inserted != len(step_values):
                    raise DatabaseError("Warmup steps were not fully persisted")
                for account_id in (account_a, account_b):
                    conn.execute(
                        """INSERT INTO warmup_accounts(
                               account_id,status,active_pair_id,updated_at)
                           VALUES(?,'active',?,CURRENT_TIMESTAMP)
                           ON CONFLICT(account_id) DO UPDATE SET
                               status='active', active_pair_id=excluded.active_pair_id,
                               transferred_at=NULL, updated_at=CURRENT_TIMESTAMP""",
                        (account_id, pair_id),
                    )
                row = conn.execute(
                    "SELECT * FROM warmup_pairs WHERE id=?", (pair_id,)
                ).fetchone()
                return dict(row) if row else {"id": pair_id}
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to create warmup pair: {exc}") from exc

    def extend_warmup_pair(
        self,
        pair_id: object,
        *,
        profile: Mapping[str, Any],
        steps: Iterable[Mapping[str, Any]],
        owner_token_a: str,
        owner_token_b: str,
        started_at: str,
        ends_at: str,
    ) -> dict[str, Any]:
        owner = _positive(pair_id, "pair_id")
        profile_data = dict(profile)
        step_values = [dict(step) for step in steps]
        if not step_values:
            raise DatabaseError("План продления пуст")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                pair = conn.execute(
                    "SELECT * FROM warmup_pairs WHERE id=?", (owner,)
                ).fetchone()
                if pair is None:
                    raise DatabaseError("Связка не найдена")
                if str(pair["status"] or "") != "completed":
                    raise DatabaseError("Продлить можно только завершённую связку")
                account_a = int(pair["account_a_id"])
                account_b = int(pair["account_b_id"])
                self._ensure_pair_accounts_available(conn, (account_a, account_b))
                week_number = int(pair["week_number"] or 1) + 1
                inserted = self._insert_steps(conn, owner, week_number, step_values)
                if inserted != len(step_values):
                    raise DatabaseError("Warmup extension steps were not fully persisted")
                conn.execute(
                    """UPDATE warmup_pairs SET
                           status='running', week_number=?, profile_seed=?, day_order=?,
                           dialogue_windows=?, reply_min_seconds=?, reply_max_seconds=?,
                           typing_min_seconds=?, typing_max_seconds=?,
                           group_visits_per_day=?, posts_min=?, posts_max=?,
                           reaction_probability_percent=?,
                           private_reaction_probability_percent=?,
                           active_start_hour=?, active_end_hour=?,
                           owner_token_a=?, owner_token_b=?, current_step=0, total_steps=?,
                           last_message_id=NULL, last_sender_account_id=NULL,
                           last_error=NULL, started_at=?, ends_at=?, completed_at=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (
                        week_number,
                        str(profile_data["seed"]),
                        str(profile_data["day_order"]),
                        int(profile_data["dialogue_windows"]),
                        int(profile_data["reply_min_seconds"]),
                        int(profile_data["reply_max_seconds"]),
                        int(profile_data["typing_min_seconds"]),
                        int(profile_data["typing_max_seconds"]),
                        int(profile_data["group_visits_per_day"]),
                        int(profile_data["posts_min"]),
                        int(profile_data["posts_max"]),
                        int(profile_data["reaction_probability_percent"]),
                        int(profile_data["private_reaction_probability_percent"]),
                        int(profile_data["active_start_hour"]),
                        int(profile_data["active_end_hour"]),
                        str(owner_token_a),
                        str(owner_token_b),
                        len(step_values),
                        str(started_at),
                        str(ends_at),
                        owner,
                    ),
                )
                for account_id in (account_a, account_b):
                    conn.execute(
                        """INSERT INTO warmup_accounts(
                               account_id,status,active_pair_id,updated_at)
                           VALUES(?,'active',?,CURRENT_TIMESTAMP)
                           ON CONFLICT(account_id) DO UPDATE SET
                               status='active', active_pair_id=excluded.active_pair_id,
                               transferred_at=NULL, updated_at=CURRENT_TIMESTAMP""",
                        (account_id, owner),
                    )
                row = conn.execute(
                    "SELECT * FROM warmup_pairs WHERE id=?", (owner,)
                ).fetchone()
                return dict(row) if row else {}
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to extend warmup pair: {exc}") from exc

    def enqueue_warmup_step(self, pair_id: object) -> dict[str, Any] | None:
        owner = _positive(pair_id, "pair_id")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                pair = conn.execute(
                    "SELECT status, week_number FROM warmup_pairs WHERE id=?",
                    (owner,),
                ).fetchone()
                if pair is None or str(pair["status"] or "") != "running":
                    return None
                step = conn.execute(
                    """SELECT * FROM warmup_steps
                       WHERE pair_id=? AND week_number=? AND status='pending'
                       ORDER BY sequence_no ASC LIMIT 1""",
                    (owner, int(pair["week_number"])),
                ).fetchone()
                if step is None:
                    return None
                existing_task_id = step["queue_task_id"]
                if existing_task_id is not None:
                    task = conn.execute(
                        "SELECT status FROM tasks WHERE id=?", (int(existing_task_id),)
                    ).fetchone()
                    if task is not None and str(task["status"] or "") in ACTIVE_TASK_STATES:
                        result = dict(step)
                        result["queue_task_id"] = int(existing_task_id)
                        return result
                payload = json.dumps(
                    {
                        "account_id": int(step["actor_account_id"]),
                        "pair_id": owner,
                        "step_id": int(step["id"]),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                cursor = conn.execute(
                    """INSERT INTO tasks(
                           account_id,type,payload,status,max_retries,not_before,
                           created_at,updated_at)
                       VALUES(?,'warmup_step',?,'pending',0,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                    (
                        int(step["actor_account_id"]),
                        payload,
                        str(step["scheduled_at"]),
                    ),
                )
                task_id = int(cursor.lastrowid or 0)
                conn.execute(
                    """UPDATE warmup_steps SET queue_task_id=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='pending'""",
                    (task_id, int(step["id"])),
                )
                result = dict(step)
                result["queue_task_id"] = task_id
                return result
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to enqueue warmup step: {exc}") from exc

    def begin_warmup_step(
        self, step_id: object, *, account_id: object
    ) -> dict[str, Any] | None:
        step_owner = _positive(step_id, "step_id")
        account_owner = _positive(account_id, "account_id")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """SELECT s.*, p.status AS pair_status, p.account_a_id,
                              p.account_b_id, p.owner_token_a, p.owner_token_b,
                              p.last_message_id, p.last_sender_account_id,
                              p.week_number AS active_week_number
                       FROM warmup_steps s
                       JOIN warmup_pairs p ON p.id=s.pair_id
                       WHERE s.id=?""",
                    (step_owner,),
                ).fetchone()
                if row is None:
                    raise DatabaseError("Шаг прогрева не найден")
                data = dict(row)
                if int(data["actor_account_id"]) != account_owner:
                    raise DatabaseError("Шаг прогрева принадлежит другому аккаунту")
                if str(data["pair_status"] or "") != "running":
                    return None
                if int(data["week_number"]) != int(data["active_week_number"]):
                    return None
                current_status = str(data["status"] or "")
                if current_status in {"done", "skipped"}:
                    data["already_finished"] = True
                    return data
                if current_status != "pending":
                    raise DatabaseError(
                        f"Шаг прогрева нельзя запустить из состояния {current_status}"
                    )
                cursor = conn.execute(
                    """UPDATE warmup_steps
                       SET status='running', started_at=CURRENT_TIMESTAMP,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='pending'""",
                    (step_owner,),
                )
                if int(cursor.rowcount or 0) != 1:
                    return None
                data["status"] = "running"
                data["owner_token"] = (
                    str(data["owner_token_a"])
                    if account_owner == int(data["account_a_id"])
                    else str(data["owner_token_b"])
                )
                return data
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to begin warmup step: {exc}") from exc

    def finish_warmup_step(
        self,
        step_id: object,
        *,
        telegram_message_id: int | None = None,
        result_text: str | None = None,
        skipped: bool = False,
    ) -> dict[str, Any]:
        step_owner = _positive(step_id, "step_id")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                step = conn.execute(
                    "SELECT * FROM warmup_steps WHERE id=?", (step_owner,)
                ).fetchone()
                if step is None:
                    raise DatabaseError("Шаг прогрева не найден")
                pair_id = int(step["pair_id"])
                week_number = int(step["week_number"])
                final_status = "skipped" if skipped else "done"
                cursor = conn.execute(
                    """UPDATE warmup_steps
                       SET status=?, telegram_message_id=?, result_text=?,
                           completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='running'""",
                    (
                        final_status,
                        int(telegram_message_id) if telegram_message_id else None,
                        str(result_text or "")[:1000] or None,
                        step_owner,
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    current = conn.execute(
                        "SELECT status FROM warmup_steps WHERE id=?", (step_owner,)
                    ).fetchone()
                    if current is None or str(current["status"] or "") not in {
                        "done",
                        "skipped",
                    }:
                        raise DatabaseError("Шаг прогрева не был завершён")
                if telegram_message_id:
                    conn.execute(
                        """UPDATE warmup_pairs
                           SET last_message_id=?, last_sender_account_id=?,
                               current_step=MAX(current_step,?),
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (
                            int(telegram_message_id),
                            int(step["actor_account_id"]),
                            int(step["sequence_no"]),
                            pair_id,
                        ),
                    )
                else:
                    conn.execute(
                        """UPDATE warmup_pairs
                           SET current_step=MAX(current_step,?),
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (int(step["sequence_no"]), pair_id),
                    )
                next_step = conn.execute(
                    """SELECT id, actor_account_id, scheduled_at
                       FROM warmup_steps
                       WHERE pair_id=? AND week_number=? AND status='pending'
                       ORDER BY sequence_no ASC LIMIT 1""",
                    (pair_id, week_number),
                ).fetchone()
                completed = next_step is None
                pair = conn.execute(
                    "SELECT * FROM warmup_pairs WHERE id=?", (pair_id,)
                ).fetchone()
                if completed:
                    conn.execute(
                        """UPDATE warmup_pairs
                           SET status='completed', completed_at=CURRENT_TIMESTAMP,
                               current_step=total_steps, last_error=NULL,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (pair_id,),
                    )
                    for account_id in (
                        int(pair["account_a_id"]),
                        int(pair["account_b_id"]),
                    ):
                        conn.execute(
                            """UPDATE warmup_accounts
                               SET status='completed', active_pair_id=NULL,
                                   weeks_completed=weeks_completed+1,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE account_id=?""",
                            (account_id,),
                        )
                return {
                    "pair_id": pair_id,
                    "completed": completed,
                    "next_step": dict(next_step) if next_step else None,
                    "pair": dict(pair) if pair else {},
                }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to finish warmup step: {exc}") from exc

    def defer_warmup_step(
        self, step_id: object, *, clear_queue_task: bool = False
    ) -> bool:
        """Return a claimed step to pending without replaying a completed RPC."""
        step_owner = _positive(step_id, "step_id")
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE warmup_steps
                       SET status='pending', started_at=NULL,
                           queue_task_id=CASE WHEN ?=1 THEN NULL ELSE queue_task_id END,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='running'""",
                    (1 if clear_queue_task else 0, step_owner),
                )
                return int(cursor.rowcount or 0) == 1
        except Exception as exc:
            raise DatabaseError(f"Failed to defer warmup step: {exc}") from exc

    def reschedule_warmup_step_after_unknown(
        self,
        step_id: object,
        *,
        delay_seconds: int = 5 * 60,
        message: str = "Результат Telegram не подтверждён; повтор через 5 минут",
    ) -> dict[str, Any]:
        """Create a durable retry task without pausing the pair or blocking a worker."""
        step_owner = _positive(step_id, "step_id")
        delay = max(60, int(delay_seconds or 5 * 60))
        clean = " ".join(str(message or "").split())[:1000]
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                step = conn.execute(
                    """SELECT id,pair_id,actor_account_id,status
                       FROM warmup_steps WHERE id=?""",
                    (step_owner,),
                ).fetchone()
                if step is None:
                    raise DatabaseError("Шаг прогрева не найден")
                if str(step["status"] or "") != "running":
                    raise DatabaseError(
                        "Повтор неизвестного результата разрешён только для запущенного шага"
                    )
                retry_at = str(
                    conn.execute(
                        "SELECT datetime('now', ?)", (f"+{delay} seconds",)
                    ).fetchone()[0]
                )
                payload = json.dumps(
                    {
                        "account_id": int(step["actor_account_id"]),
                        "pair_id": int(step["pair_id"]),
                        "step_id": step_owner,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                cursor = conn.execute(
                    """INSERT INTO tasks(
                           account_id,type,payload,status,max_retries,not_before,
                           created_at,updated_at)
                       VALUES(?,'warmup_step',?,'pending',0,?,
                              CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                    (int(step["actor_account_id"]), payload, retry_at),
                )
                task_id = int(cursor.lastrowid or 0)
                if task_id <= 0:
                    raise DatabaseError("Не удалось создать повтор шага прогрева")
                updated = conn.execute(
                    """UPDATE warmup_steps
                       SET status='pending', queue_task_id=?, result_text=?,
                           started_at=NULL, completed_at=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='running'""",
                    (task_id, clean, step_owner),
                )
                if int(updated.rowcount or 0) != 1:
                    raise DatabaseError(
                        "Состояние шага изменилось во время планирования повтора"
                    )
                conn.execute(
                    """UPDATE warmup_pairs
                       SET last_error=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='running'""",
                    (int(step["pair_id"]),),
                )
                return {
                    "pair_id": int(step["pair_id"]),
                    "task_id": task_id,
                    "retry_at": retry_at,
                }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to reschedule unknown warmup result: {exc}"
            ) from exc

    def fail_warmup_step(
        self,
        step_id: object,
        *,
        message: str,
        uncertain: bool = False,
    ) -> dict[str, Any]:
        step_owner = _positive(step_id, "step_id")
        clean = " ".join(str(message or "Ошибка прогрева").split())[:1000]
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                step = conn.execute(
                    "SELECT pair_id FROM warmup_steps WHERE id=?", (step_owner,)
                ).fetchone()
                if step is None:
                    return {}
                status = "uncertain" if uncertain else "failed"
                conn.execute(
                    """UPDATE warmup_steps
                       SET status=?, result_text=?, completed_at=CURRENT_TIMESTAMP,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('pending','running')""",
                    (status, clean, step_owner),
                )
                conn.execute(
                    """UPDATE warmup_pairs
                       SET status='paused', last_error=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='running'""",
                    (clean, int(step["pair_id"])),
                )
                return {"pair_id": int(step["pair_id"]), "status": status}
        except Exception as exc:
            raise DatabaseError(f"Failed to fail warmup step: {exc}") from exc

    def pause_warmup_pair(self, pair_id: object, reason: str = "Пауза пользователя") -> bool:
        owner = _positive(pair_id, "pair_id")
        clean = " ".join(str(reason or "Пауза пользователя").split())[:500]
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """UPDATE warmup_pairs
                       SET status='paused', last_error=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='running'""",
                    (clean, owner),
                )
                conn.execute(
                    """UPDATE tasks SET status='cancelled', error=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id IN (
                           SELECT queue_task_id FROM warmup_steps
                           WHERE pair_id=? AND status='pending' AND queue_task_id IS NOT NULL
                       ) AND status='pending'""",
                    (clean, owner),
                )
                conn.execute(
                    """UPDATE warmup_steps SET queue_task_id=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE pair_id=? AND status='pending'""",
                    (owner,),
                )
                return int(cursor.rowcount or 0) == 1
        except Exception as exc:
            raise DatabaseError(f"Failed to pause warmup pair: {exc}") from exc

    def resume_warmup_pair(self, pair_id: object) -> bool:
        owner = _positive(pair_id, "pair_id")
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE warmup_pairs
                       SET status='running', last_error=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='paused'
                         AND NOT EXISTS(
                             SELECT 1 FROM warmup_steps
                             WHERE pair_id=? AND week_number=warmup_pairs.week_number
                               AND status IN ('uncertain','failed')
                         )""",
                    (owner, owner),
                )
                return int(cursor.rowcount or 0) == 1
        except Exception as exc:
            raise DatabaseError(f"Failed to resume warmup pair: {exc}") from exc

    def retry_failed_warmup_step(self, pair_id: object) -> bool:
        owner = _positive(pair_id, "pair_id")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                pair = conn.execute(
                    "SELECT week_number,status FROM warmup_pairs WHERE id=?", (owner,)
                ).fetchone()
                if pair is None or str(pair["status"] or "") != "paused":
                    return False
                step = conn.execute(
                    """SELECT id,status FROM warmup_steps
                       WHERE pair_id=? AND week_number=?
                         AND status IN ('failed')
                       ORDER BY sequence_no ASC LIMIT 1""",
                    (owner, int(pair["week_number"])),
                ).fetchone()
                if step is None:
                    return False
                conn.execute(
                    """UPDATE warmup_steps
                       SET status='pending', queue_task_id=NULL, result_text=NULL,
                           started_at=NULL, completed_at=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='failed'""",
                    (int(step["id"]),),
                )
                conn.execute(
                    """UPDATE warmup_pairs
                       SET status='running', last_error=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (owner,),
                )
                return True
        except Exception as exc:
            raise DatabaseError(f"Failed to retry warmup step: {exc}") from exc

    def transfer_warmup_account(self, account_id: object) -> dict[str, Any]:
        owner = _positive(account_id, "account_id")
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                state = conn.execute(
                    "SELECT * FROM warmup_accounts WHERE account_id=?", (owner,)
                ).fetchone()
                if state is None or str(state["status"] or "") != "completed":
                    raise DatabaseError(
                        "Перенести можно только аккаунт с завершённым прогревом"
                    )
                if state["active_pair_id"] is not None:
                    raise DatabaseError("Аккаунт ещё участвует в активной связке")
                conn.execute(
                    """UPDATE warmup_accounts
                       SET status='transferred', transferred_at=CURRENT_TIMESTAMP,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE account_id=?""",
                    (owner,),
                )
                account = conn.execute(
                    """SELECT telegram_account_id, display_name, username, phone_masked,
                              authorized, runtime_state, stopped
                       FROM telegram_accounts WHERE telegram_account_id=?""",
                    (owner,),
                ).fetchone()
                return dict(account) if account else {"telegram_account_id": owner}
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to transfer warmup account: {exc}") from exc

    def is_account_in_active_warmup(self, account_id: object) -> bool:
        owner = _positive(account_id, "account_id")
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT 1 FROM warmup_accounts wa
                       JOIN warmup_pairs p ON p.id=wa.active_pair_id
                       WHERE wa.account_id=? AND wa.status='active'
                         AND p.status IN ('running','paused') LIMIT 1""",
                    (owner,),
                ).fetchone()
                return row is not None
        except Exception as exc:
            raise DatabaseError(f"Failed to inspect active warmup: {exc}") from exc

    def choose_warmup_group_for_account(self, account_id: object) -> dict[str, Any] | None:
        owner = _positive(account_id, "account_id")
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT g.id, g.chat_ref, g.title, g.enabled,
                              ga.membership_state, ga.last_read_message_id,
                              ga.last_reacted_message_id, ga.last_visited_at
                       FROM warmup_groups g
                       LEFT JOIN warmup_group_accounts ga
                         ON ga.group_id=g.id AND ga.account_id=?
                       WHERE g.enabled=1
                         AND COALESCE(ga.membership_state,'unknown') NOT IN ('blocked','unavailable')
                       ORDER BY CASE WHEN ga.last_visited_at IS NULL THEN 0 ELSE 1 END,
                                ga.last_visited_at ASC, g.id ASC LIMIT 1""",
                    (owner,),
                ).fetchone()
                return dict(row) if row else None
        except Exception as exc:
            raise DatabaseError(f"Failed to choose warmup group: {exc}") from exc

    def record_warmup_group_visit(
        self,
        *,
        group_id: object,
        account_id: object,
        membership_state: str,
        last_read_message_id: int | None = None,
        last_reacted_message_id: int | None = None,
        error: str | None = None,
    ) -> None:
        group_owner = _positive(group_id, "group_id")
        account_owner = _positive(account_id, "account_id")
        state = str(membership_state or "unknown")
        if state not in {"unknown", "joined", "requested", "unavailable", "blocked"}:
            state = "unknown"
        try:
            with self.get_connection() as conn:
                conn.execute(
                    """INSERT INTO warmup_group_accounts(
                           group_id,account_id,membership_state,last_read_message_id,
                           last_reacted_message_id,last_visited_at,last_error,updated_at)
                       VALUES(?,?,?,?,?,CURRENT_TIMESTAMP,?,CURRENT_TIMESTAMP)
                       ON CONFLICT(group_id,account_id) DO UPDATE SET
                           membership_state=excluded.membership_state,
                           last_read_message_id=COALESCE(
                               excluded.last_read_message_id,
                               warmup_group_accounts.last_read_message_id
                           ),
                           last_reacted_message_id=COALESCE(
                               excluded.last_reacted_message_id,
                               warmup_group_accounts.last_reacted_message_id
                           ),
                           last_visited_at=CURRENT_TIMESTAMP,
                           last_error=excluded.last_error,
                           updated_at=CURRENT_TIMESTAMP""",
                    (
                        group_owner,
                        account_owner,
                        state,
                        int(last_read_message_id) if last_read_message_id else None,
                        int(last_reacted_message_id) if last_reacted_message_id else None,
                        " ".join(str(error or "").split())[:500] or None,
                    ),
                )
        except Exception as exc:
            raise DatabaseError(f"Failed to record warmup group visit: {exc}") from exc

    def recover_stale_warmup_steps(self) -> int:
        """Resume interrupted warmup actions after five minutes without pausing pairs."""
        try:
            with self.get_connection() as conn:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """SELECT id,pair_id,actor_account_id
                       FROM warmup_steps WHERE status='running'"""
                ).fetchall()
                for row in rows:
                    retry_at = str(
                        conn.execute(
                            "SELECT datetime('now', '+300 seconds')"
                        ).fetchone()[0]
                    )
                    payload = json.dumps(
                        {
                            "account_id": int(row["actor_account_id"]),
                            "pair_id": int(row["pair_id"]),
                            "step_id": int(row["id"]),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    cursor = conn.execute(
                        """INSERT INTO tasks(
                               account_id,type,payload,status,max_retries,not_before,
                               created_at,updated_at)
                           VALUES(?,'warmup_step',?,'pending',0,?,
                                  CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                        (int(row["actor_account_id"]), payload, retry_at),
                    )
                    task_id = int(cursor.lastrowid or 0)
                    if task_id <= 0:
                        raise DatabaseError(
                            "Не удалось восстановить прерванный шаг прогрева"
                        )
                    conn.execute(
                        """UPDATE warmup_steps
                           SET status='pending', queue_task_id=?, result_text=?,
                               started_at=NULL, completed_at=NULL,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status='running'""",
                        (
                            task_id,
                            "Приложение завершилось во время Telegram-действия; повтор через 5 минут",
                            int(row["id"]),
                        ),
                    )
                    conn.execute(
                        """UPDATE warmup_pairs
                           SET status='running', last_error=NULL,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status='running'""",
                        (int(row["pair_id"]),),
                    )
                return len(rows)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to recover stale warmup steps: {exc}") from exc


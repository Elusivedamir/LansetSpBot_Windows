# OBSERVABILITY-PACKAGE-V3
from __future__ import annotations


class GUIServiceAdapter:
    def __init__(self, api):
        self.api = api

    def get_channels(self, account_id=None):
        return self.api.get_channels(account_id=account_id)

    def delete_channels(self, channel_ids):
        return self.api.delete_channels(channel_ids)

    def get_commenting_channels(self):
        return self.api.get_commenting_channels()

    def get_max_channels_per_run(self):
        return self.api.get_max_channels_per_run()

    def get_comment_daily_limit(self, account_id=None):
        return self.api.get_comment_daily_limit(account_id=account_id)

    def set_comment_daily_limit(self, value, *, account_id=None):
        return self.api.set_comment_daily_limit(
            value,
            account_id=account_id,
        )

    def create_task(self, task_type, payload):
        return self.api.create_task(task_type, payload)

    def get_tasks(self):
        return self.api.get_tasks()

    def get_account_observability(self, account_id=None):
        return self.api.get_account_observability(account_id)

    def find_resumable_audience_task(self, account_id=None):
        return self.api.find_resumable_audience_task(account_id)

    def resume_audience_task(self, task_id):
        return self.api.resume_audience_task(task_id)

    def restart_audience_task(self, task_id):
        return self.api.restart_audience_task(task_id)

    def discard_audience_task(self, task_id):
        return self.api.discard_audience_task(task_id)

    def get_active_link_task(self, account_id=None):
        return self.api.get_active_link_task(account_id=account_id)

    def count_unchecked_link_targets(self, account_id=None):
        return self.api.count_unchecked_link_targets(account_id=account_id)

    def pause_link_task(self, task_id):
        return self.api.pause_link_task(task_id)

    def resume_link_task(self, task_id):
        return self.api.resume_link_task(task_id)

    def get_task(self, task_id):
        return self.api.get_task(task_id)

    def close_thread_connection(self):
        return self.api.close_thread_connection()

    def cancel_task(self, task_id):
        return self.api.cancel_task(task_id)

    def start_queue(self):
        return self.api.start_queue()

    def get_queue_unavailable_reason(self):
        return self.api.get_queue_unavailable_reason()

    def get_queue_unavailable_message(self):
        reason = self.get_queue_unavailable_reason()
        return {
            "shutdown_in_progress": (
                "Приложение завершает работу или выполняет заводской сброс. "
                "Дождитесь завершения операции."
            ),
            "local_secret_migration": (
                "Завершается перенос локальных настроек. Повторите действие через минуту."
            ),
            "account_state_pending": (
                "Не завершено сохранение состояния Telegram-аккаунта. "
                "Перезапустите LansetSpBot; состояние будет восстановлено автоматически."
            ),
            "auth_in_progress": (
                "Выполняется подключение или смена Telegram-аккаунта. "
                "Дождитесь завершения авторизации."
            ),
            "account_restricted": (
                "Telegram ограничил активность аккаунта. Проверьте статус через "
                "кнопку @SpamBot в живом журнале."
            ),
            "worker_missing": "Фоновый обработчик не создан.",
        }.get(reason, "Фоновый обработчик недоступен")

    def stop_queue(self):
        return self.api.stop_queue()

    def prepare_shutdown(self):
        return self.api.prepare_shutdown()

    def prepare_factory_reset(self):
        return self.api.prepare_factory_reset()

    def cancel_shutdown(self):
        return self.api.cancel_shutdown()

    def is_secret_migration_running(self):
        return self.api.is_secret_migration_running()

    def is_queue_running(self):
        return self.api.is_queue_running()

    def prepare_account_change(self, timeout_ms=45_000):
        return self.api.prepare_account_change(timeout_ms)

    def save_settings(self, values):
        return self.api.save_settings(values)

    def save_account_settings(self, values, *, account_id=None):
        return self.api.save_account_settings(values, account_id=account_id)

    def get_current_account_id(self):
        return self.api.get_current_account_id()

    def get_settings(self, prefix=None):
        return self.api.get_settings(prefix)

    def save_comment_template(self, comments):
        return self.api.save_comment_template(comments)

    def get_main_comments(self):
        return self.api.get_main_comments()

    def save_comment_profile(self, comments, *, visible_count, account_id=None):
        return self.api.save_comment_profile(
            comments,
            visible_count=visible_count,
            account_id=account_id,
        )

    def get_comment_profile(self, account_id=None):
        return self.api.get_comment_profile(account_id=account_id)

    def import_previous_comment_profile(self, account_id=None):
        return self.api.import_previous_comment_profile(account_id=account_id)

    def get_openai_configuration(self):
        return self.api.get_openai_configuration()

    def save_openai_configuration(self, values):
        return self.api.save_openai_configuration(values)

    def submit_openai_test(self, post_text=None):
        return self.api.submit_openai_test(post_text)

    def get_comment_history(
        self, task_id=None, limit=100, campaign_id=None, account_id=None
    ):
        return self.api.get_comment_history(
            task_id=task_id,
            limit=limit,
            campaign_id=campaign_id,
            account_id=account_id,
        )

    def get_logs(self, level=None, limit=100, account_id=None):
        return self.api.get_logs(
            level=level,
            limit=limit,
            account_id=account_id,
        )

    def start_comment_campaign(
        self, comments, continuous=True, daily_limit=None, comment_source="prepared"
    ):
        return self.api.start_comment_campaign(
            comments,
            continuous=continuous,
            daily_limit=daily_limit,
            comment_source=comment_source,
        )

    def get_comment_campaign_state(self, account_id=None):
        return self.api.get_comment_campaign_state(account_id=account_id)

    def get_comment_campaign_schedule(self, campaign_id=None, limit=200):
        return self.api.get_comment_campaign_schedule(
            campaign_id=campaign_id, limit=limit
        )

    def pause_comment_campaign(self):
        return self.api.pause_comment_campaign()

    def resume_comment_campaign(self):
        return self.api.resume_comment_campaign()

    def stop_comment_campaign(self):
        return self.api.stop_comment_campaign()

    def set_auth_in_progress(self, active):
        return self.api.set_auth_in_progress(active)

    def get_saved_dialogs(self, account_id=None):
        return self.api.get_saved_dialogs(account_id=account_id)

    def start_join_campaign(self):
        return self.api.start_join_campaign()

    def get_join_campaign_state(self, account_id=None):
        return self.api.get_join_campaign_state(account_id=account_id)

    def pause_join_campaign(self):
        return self.api.pause_join_campaign()

    def resume_join_campaign(self):
        return self.api.resume_join_campaign()

    def stop_join_campaign(self):
        return self.api.stop_join_campaign()

    def get_scheduler_error(self):
        return self.api.get_scheduler_error()

    def get_account_restriction_state(self, account_id=None):
        return self.api.get_account_restriction_state(account_id=account_id)

    def confirm_spambot_restriction_cleared(self, account_id=None):
        return self.api.confirm_spambot_restriction_cleared(account_id=account_id)

    def list_telegram_accounts(self):
        return self.api.list_telegram_accounts()

    def can_add_telegram_account(self):
        return self.api.can_add_telegram_account()

    def select_telegram_account(self, account_id):
        return self.api.select_telegram_account(account_id)

    def get_selected_account_id(self):
        return self.api.get_selected_account_id()

    def get_previous_selected_account_id(self):
        return self.api.get_previous_selected_account_id()

    def get_account_settings(self, account_id=None):
        return self.api.get_account_settings(account_id)

    def register_authorized_account(
        self, account, settings, *, pending_session_name
    ):
        return self.api.register_authorized_account(
            account,
            settings,
            pending_session_name=pending_session_name,
        )

    def stop_telegram_account(self, account_id):
        return self.api.stop_telegram_account(account_id)

    def resume_telegram_account(self, account_id):
        return self.api.resume_telegram_account(account_id)

    def disconnect_telegram_account(self, account_id):
        return self.api.disconnect_telegram_account(account_id)

    def delete_telegram_account(self, account_id):
        return self.api.delete_telegram_account(account_id)

    def check_telegram_account_runtime(self, account_id):
        return self.api.check_telegram_account_runtime(account_id)

    def import_comments_from_previous_account(
        self,
        *,
        mode,
        source_account_id=None,
        target_account_id=None,
    ):
        return self.api.import_comments_from_previous_account(
            mode=mode,
            source_account_id=source_account_id,
            target_account_id=target_account_id,
        )

    def import_channels_from_previous_account(
        self,
        *,
        source_account_id=None,
        target_account_id=None,
    ):
        return self.api.import_channels_from_previous_account(
            source_account_id=source_account_id,
            target_account_id=target_account_id,
        )


    def get_warmup_selector_accounts(self):
        return self.api.get_warmup_selector_accounts()

    def get_warmup_overview(self):
        return self.api.get_warmup_overview()

    def create_warmup_pair(self, account_a_id, account_b_id):
        return self.api.create_warmup_pair(account_a_id, account_b_id)

    def add_warmup_group(self, chat_ref, account_id):
        return self.api.add_warmup_group(chat_ref, account_id)

    def populate_warmup_groups_from_synced(self, account_id):
        return self.api.populate_warmup_groups_from_synced(account_id)

    def remove_warmup_group(self, group_id, account_id):
        return self.api.remove_warmup_group(group_id, account_id)

    def pause_warmup_pair(self, pair_id):
        return self.api.pause_warmup_pair(pair_id)

    def resume_warmup_pair(self, pair_id):
        return self.api.resume_warmup_pair(pair_id)

    def retry_failed_warmup_pair(self, pair_id):
        return self.api.retry_failed_warmup_pair(pair_id)

    def archive_paused_warmup_pair(self, pair_id):
        return self.api.archive_paused_warmup_pair(pair_id)

    def extend_warmup_pair(self, pair_id):
        return self.api.extend_warmup_pair(pair_id)

    def transfer_warmup_account(self, account_id):
        return self.api.transfer_warmup_account(account_id)

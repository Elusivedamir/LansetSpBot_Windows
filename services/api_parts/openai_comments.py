from __future__ import annotations

from concurrent.futures import Future
from typing import Any, TYPE_CHECKING, cast

from core.openai_settings import (
    DEFAULT_OPENAI_SYSTEM_PROMPT,
    OPENAI_API_KEY_SECRET,
    CommentGenerationSettings,
    normalize_comment_source,
)

if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:
    class _MixinHost:
        pass


class OpenAICommentAPIMixin(_MixinHost):
    def _strict_openai_key(self) -> str | None:
        owner = int(self.get_current_account_id() or 0)
        if owner > 0 and hasattr(self, "_strict_account_secret"):
            return self._strict_account_secret(owner, OPENAI_API_KEY_SECRET)
        getter = getattr(type(self.secret_store), "get_strict_optional", None)
        if callable(getter):
            value = self.secret_store.get_strict_optional(OPENAI_API_KEY_SECRET)
            return None if value is None else str(value)
        value = self.secret_store.get(OPENAI_API_KEY_SECRET, "")
        return None if value in (None, "") else str(value)

    @staticmethod
    def _mask_api_key(value: str | None) -> str:
        key = str(value or "").strip()
        if not key:
            return ""
        if len(key) <= 8:
            return "•" * len(key)
        return f"{key[:3]}…{key[-4:]}"

    def _openai_database(self):
        owner = int(self.get_current_account_id() or 0)
        return self.database.for_account(owner) if owner > 0 else self.database

    def get_openai_configuration(self) -> dict[str, Any]:
        database = self._openai_database()
        public = dict(database.get_settings("openai."))
        settings = CommentGenerationSettings.from_mapping(public)
        prompt = str(
            public.get("openai.system_prompt") or DEFAULT_OPENAI_SYSTEM_PROMPT
        ).strip()
        source = normalize_comment_source(
            public.get("openai.comment_source") or "prepared"
        )
        key = self._strict_openai_key()
        return {
            "comment_source": source,
            "model": settings.model,
            "system_prompt": prompt,
            "max_words": settings.max_words,
            "temperature": settings.temperature,
            "timeout_seconds": settings.timeout_seconds,
            "max_generation_attempts": settings.max_generation_attempts,
            "manual_approval_required": False,
            "has_api_key": bool(key),
            "api_key_mask": self._mask_api_key(key),
        }

    def save_openai_configuration(self, values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("Настройки OpenAI должны быть объектом")
        owner = int(self.get_current_account_id() or 0)
        if owner <= 0:
            raise ValueError("Сначала выберите Telegram-аккаунт")
        database = self.database.for_account(owner)
        merged = dict(database.get_settings("openai."))
        public_keys = {
            "model": "openai.model",
            "max_words": "openai.max_words",
            "temperature": "openai.temperature",
            "timeout_seconds": "openai.timeout_seconds",
            "max_generation_attempts": "openai.max_generation_attempts",
            "min_post_characters": "openai.min_post_characters",
            "max_post_characters": "openai.max_post_characters",
        }
        for input_key, storage_key in public_keys.items():
            if input_key in values and values.get(input_key) is not None:
                merged[storage_key] = values.get(input_key)
        settings = CommentGenerationSettings.from_mapping(merged)
        system_prompt = str(
            values.get("system_prompt")
            or merged.get("openai.system_prompt")
            or DEFAULT_OPENAI_SYSTEM_PROMPT
        ).strip()
        if not system_prompt:
            raise ValueError("System-промпт OpenAI не может быть пустым")
        if len(system_prompt) > 20_000:
            raise ValueError("System-промпт OpenAI слишком длинный")
        source = normalize_comment_source(
            values.get("comment_source", merged.get("openai.comment_source"))
        )
        public = settings.to_storage_mapping()
        public.update(
            {
                "openai.system_prompt": system_prompt,
                "openai.comment_source": source,
                "openai.manual_approval_required": "0",
            }
        )
        database.set_settings(public)

        raw_key = values.get("api_key")
        if raw_key is not None:
            key = str(raw_key).strip()
            if key:
                if (
                    len(key) < 20
                    or len(key) > 512
                    or any(ch.isspace() for ch in key)
                ):
                    raise ValueError("API-ключ OpenAI имеет некорректный формат")
                self._set_account_secret(owner, OPENAI_API_KEY_SECRET, key)
            elif bool(values.get("clear_api_key")):
                self._set_account_secret(owner, OPENAI_API_KEY_SECRET, None)
        return self.get_openai_configuration()

    def submit_openai_test(self, post_text: str | None = None) -> Future[Any]:
        worker = self.queue_worker
        if worker is None:
            raise RuntimeError("Фоновый обработчик не создан")
        owner = int(self.get_current_account_id() or 0)
        if owner <= 0:
            raise RuntimeError("Сначала выберите Telegram-аккаунт")
        if not worker.isRunning():
            if not self.start_queue():
                raise RuntimeError(self.get_queue_unavailable_message())
        submit = getattr(worker, "submit_utility", None)
        if not callable(submit):
            raise RuntimeError("Эта сборка не поддерживает фоновые тесты OpenAI")
        return cast(
            "Future[Any]",
            submit(
                "openai_test",
                {"post_text": str(post_text or ""), "account_id": owner},
            ),
        )

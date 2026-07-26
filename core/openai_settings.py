from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

DEFAULT_OPENAI_MODEL = "gpt-5.5"
DEFAULT_OPENAI_SYSTEM_PROMPT = """Ты создаёшь короткий осмысленный комментарий к публикации.

Тебе передают два блока данных: <telegram_post> — публикация, и
<author_comment> — комментарий автора кампании. Итоговая реплика должна
сохранять смысл, позицию и тон <author_comment> и при этом явно относиться к
содержанию <telegram_post>. Это одна связная мысль, а не два склеенных куска.

Правила:
1. Ответь на языке публикации.
2. Комментарий должен относиться к фактическому содержанию публикации.
3. Не добавляй ссылки, хэштеги и призывы перейти в профиль.
4. Не выдавай себя за автора публикации.
5. Не придумывай личный опыт, которого нет во входных данных.
6. Не используй фразы: «Как искусственный интеллект», «Важно отметить», «В заключение», «Стоит подчеркнуть».
7. Не повторяй заголовок публикации дословно.
8. Не добавляй неподтверждённые факты.
9. Соблюдай установленное ограничение по количеству слов.
10. Не копируй <author_comment> дословно и не пересказывай публикацию.
11. Верни только готовый комментарий без кавычек и пояснений."""

SOURCE_PREWRITTEN = "prepared"
SOURCE_OPENAI = "openai"
OPENAI_API_KEY_SECRET = "openai.api_key"


@dataclass(frozen=True, slots=True)
class CommentGenerationSettings:
    model: str = DEFAULT_OPENAI_MODEL
    max_words: int = 35
    temperature: float = 0.4
    timeout_seconds: float = 30.0
    max_generation_attempts: int = 1
    # User explicitly requested automatic dispatch after successful generation.
    manual_approval_required: bool = False
    min_post_characters: int = 24
    max_post_characters: int = 12_000

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "CommentGenerationSettings":
        def as_int(key: str, default: int, minimum: int, maximum: int) -> int:
            try:
                parsed = int(values.get(key, default))
            except (TypeError, ValueError, OverflowError):
                parsed = default
            return max(minimum, min(maximum, parsed))

        def as_float(key: str, default: float, minimum: float, maximum: float) -> float:
            try:
                parsed = float(values.get(key, default))
            except (TypeError, ValueError, OverflowError):
                parsed = default
            return max(minimum, min(maximum, parsed))

        model = str(values.get("openai.model") or DEFAULT_OPENAI_MODEL).strip()
        if not model or len(model) > 120:
            model = DEFAULT_OPENAI_MODEL
        return cls(
            model=model,
            max_words=as_int("openai.max_words", 35, 3, 200),
            temperature=as_float("openai.temperature", 0.4, 0.0, 2.0),
            timeout_seconds=as_float("openai.timeout_seconds", 30.0, 5.0, 180.0),
            max_generation_attempts=as_int("openai.max_generation_attempts", 1, 1, 3),
            manual_approval_required=False,
            min_post_characters=as_int("openai.min_post_characters", 24, 8, 500),
            max_post_characters=as_int("openai.max_post_characters", 12_000, 500, 50_000),
        )

    def to_storage_mapping(self) -> dict[str, str]:
        values = asdict(self)
        return {
            "openai.model": str(values["model"]),
            "openai.max_words": str(values["max_words"]),
            "openai.temperature": str(values["temperature"]),
            "openai.timeout_seconds": str(values["timeout_seconds"]),
            "openai.max_generation_attempts": str(values["max_generation_attempts"]),
            "openai.manual_approval_required": "0",
            "openai.min_post_characters": str(values["min_post_characters"]),
            "openai.max_post_characters": str(values["max_post_characters"]),
        }


def normalize_comment_source(value: Any) -> str:
    return SOURCE_OPENAI if str(value or "").strip().lower() == SOURCE_OPENAI else SOURCE_PREWRITTEN

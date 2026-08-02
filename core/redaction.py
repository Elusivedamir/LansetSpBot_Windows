"""Central redaction helpers for errors, journals and exported diagnostics."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

_REDACTED = "<redacted>"

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "proxy_password",
        "proxy_pass",
        "proxy_username",
        "proxy_login",
        "secret",
        "proxy_secret",
        "mtproxy_secret",
        "mtproto_secret",
        "username",
        "api_hash",
        "api_key",
        "openai_api_key",
        "authorization",
        "access_token",
        "bearer_token",
        "token",
        "phone",
        "phone_number",
        "phone_code_hash",
        "authorization_code",
        "auth_code",
        "verification_code",
        "login_code",
        "two_fa",
        "2fa",
        "two_factor_password",
        "session",
        "session_path",
        "session_file",
    }
)

_KEY_TOKEN = (
    r"proxy[_-]?(?:password|pass|username|login|secret)|mt(?:proxy|proto)[_-]?secret|secret|username|password|passwd|pwd|"
    r"api[_-]?(?:hash|key)|openai[_-]?api[_-]?key|authorization|access[_-]?token|bearer[_-]?token|token|phone(?:[_-]?number|[_-]?code[_-]?hash)?|"
    r"auth(?:orization)?[_-]?code|verification[_-]?code|login[_-]?code|"
    r"two[_-]?fa|2fa|two[_-]?factor[_-]?password|"
    r"session(?:[_-]?(?:path|file))?"
)

# Handles Python repr and JSON forms, including quoted keys:
# {'password': 'secret'}, {"session_path": "/tmp/main.session"}
# ``re.DOTALL`` so a quoted value that spans physical lines is redacted whole.
# Without it the non-greedy body stops at the newline, the unquoted branch then
# matches only the first line, and everything after the newline survives into
# the journal.
_QUOTED_KEY_VALUE = re.compile(
    rf"(?i)((?:['\"])?(?:{_KEY_TOKEN})(?:['\"])?\s*[:=]\s*)(['\"])(.*?)(\2)",
    re.DOTALL,
)
_UNQUOTED_KEY_VALUE = re.compile(
    rf"(?i)((?:['\"])?(?:{_KEY_TOKEN})(?:['\"])?\s*[:=]\s*)([^\s,;\]}}]+)"
)

# URI credentials such as socks5://user:password@host.
# The password part is matched greedily up to the LAST "@" that still leaves a
# plausible host behind it. A password containing "@" (users paste raw,
# non-percent-encoded proxy URLs) would otherwise leave its tail in the log.
# The password run also accepts "/" because users paste raw, non
# percent-encoded proxy URLs. The lookahead still anchors on the last "@" that
# leaves a plausible host behind it, so a real path after the host is not
# swallowed.
_URI_CREDENTIALS = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://)([^\s/:@]+):(\S+)(@)(?=[^\s/@]*(?:[/?#]|\s|$))"
)

# Provider credentials and authorization headers may appear without a key=value wrapper.
_OPENAI_KEY = re.compile(r"(?i)\bsk-(?:proj-)?[a-z0-9_-]{8,}\b")
_BEARER_TOKEN = re.compile(r"(?i)(\b(?:authorization\s*:\s*)?bearer\s+)([^\s,;]+)")

# Telegram session files in POSIX, Windows and relative paths. The path itself
# is sensitive even when the filename does not contain a credential.
_SESSION_PATHS = re.compile(
    r"(?i)(?:"
    r"(?:[a-z]:\\(?:[^\\\s'\";,\]}]+\\)*[^\\\s'\";,\]}]+\.session(?:-(?:journal|wal|shm))?)"
    r"|(?:~?/[^\s'\";,\]}]*\.session(?:-(?:journal|wal|shm))?)"
    r"|(?:\b(?:sessions?[\\/])[^\s'\";,\]}]*\.session(?:-(?:journal|wal|shm))?)"
    r")"
)


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _is_sensitive_key(value: Any) -> bool:
    """Match a mapping key against the same vocabulary the text path uses.

    ``sanitize_text`` finds a sensitive token anywhere inside a key, so the
    structured path must not be narrower: ``user_password`` and
    ``password_old`` are as sensitive as ``password``. Matching is done on
    whole underscore-separated tokens rather than on substrings, so unrelated
    diagnostic keys such as ``contains_sessions`` or ``foreign_keys`` keep
    their values.
    """

    normalized = _normalized_key(value)
    if normalized in _SENSITIVE_KEYS:
        return True
    tokens = [token for token in normalized.split("_") if token]
    if any(token in _SENSITIVE_KEYS for token in tokens):
        return True
    # Compound names such as ``proxy_password`` or ``openai_api_key`` are in the
    # set as a whole; also accept any contiguous run of their tokens.
    for start in range(len(tokens)):
        for end in range(start + 2, len(tokens) + 1):
            if "_".join(tokens[start:end]) in _SENSITIVE_KEYS:
                return True
    return False


def sanitize_text(value: Any, *, secrets: Iterable[Any] = ()) -> str:
    """Return text with credentials and local Telegram-session paths removed."""

    text = str(value or "")
    text = _OPENAI_KEY.sub(_REDACTED, text)
    text = _BEARER_TOKEN.sub(
        lambda match: f"{match.group(1)}{_REDACTED}", text
    )
    text = _QUOTED_KEY_VALUE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}{match.group(4)}",
        text,
    )
    text = _UNQUOTED_KEY_VALUE.sub(lambda match: f"{match.group(1)}{_REDACTED}", text)
    text = _URI_CREDENTIALS.sub(
        lambda match: (
            f"{match.group(1)}{_REDACTED}:{_REDACTED}{match.group(4)}"
        ),
        text,
    )
    text = _SESSION_PATHS.sub(_REDACTED, text)
    for secret in secrets:
        raw = str(secret or "")
        if raw:
            text = text.replace(raw, _REDACTED)
    return text


def sanitize_log_text(value: Any, *, secrets: Iterable[Any] = ()) -> str:
    """Sanitize one physical log record and neutralize line/control injection."""

    text = sanitize_text(value, secrets=secrets)
    pieces: list[str] = []
    for char in text:
        code = ord(char)
        if char == "\n":
            pieces.append("\\n")
        elif char == "\r":
            pieces.append("\\r")
        elif code < 32 or code == 127:
            pieces.append(f"\\x{code:02x}")
        else:
            pieces.append(char)
    return "".join(pieces)


def sanitize_data(value: Any, *, secrets: Iterable[Any] = ()) -> Any:
    """Recursively sanitize structured exception/details data.

    The result contains JSON-compatible primitives. Values under known secret
    keys are replaced without first converting the whole object to a string, so
    nested dictionaries and lists cannot bypass redaction.
    """

    return _sanitize_data(value, tuple(secrets), set(), 0)


def _sanitize_data(
    value: Any, explicit: tuple[Any, ...], seen: set[int], depth: int
) -> Any:
    if depth > 24:
        return _REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        identity = id(value)
        if identity in seen:
            return _REDACTED
        seen.add(identity)
        try:
            if isinstance(value, Mapping):
                result: dict[str, Any] = {}
                for key, item in value.items():
                    safe_key = sanitize_text(key, secrets=explicit)
                    if _is_sensitive_key(key):
                        result[safe_key] = _REDACTED
                    else:
                        result[safe_key] = _sanitize_data(
                            item, explicit, seen, depth + 1
                        )
                return result
            return [_sanitize_data(item, explicit, seen, depth + 1) for item in value]
        finally:
            seen.discard(identity)
    if isinstance(value, BaseException):
        return sanitize_exception(value, secrets=explicit)
    return sanitize_text(value, secrets=explicit)


def sanitize_json(value: Any, *, secrets: Iterable[Any] = ()) -> str:
    """Serialize structured data only after recursive redaction."""

    payload: Any = value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {"raw": sanitize_text(value, secrets=secrets)}
    return json.dumps(
        sanitize_data(payload, secrets=secrets),
        ensure_ascii=False,
        sort_keys=True,
        default=lambda item: sanitize_text(item, secrets=secrets),
    )


def sanitize_exception(exc: BaseException, *, secrets: Iterable[Any] = ()) -> str:
    """Render an exception chain without persisting credentials or session paths."""

    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < 8:
        seen.add(id(current))
        parts.append(
            sanitize_text(
                f"{type(current).__name__}: {current}",
                secrets=secrets,
            )
        )
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return " <- caused by: ".join(parts)

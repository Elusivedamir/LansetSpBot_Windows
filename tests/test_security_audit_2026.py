from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from core.local_security import LocalFileSecurityError
from core.private_trace import open_helper_trace
from core.redaction import sanitize_data, sanitize_log_text, sanitize_text
from core.paths import AppPaths


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        database=root / "marlen.db",
        logs=root / "logs",
        sessions=root / "sessions",
        backups=root / "backups",
    )


def test_helper_trace_rejects_symlink_and_is_owner_only(tmp_path: Path) -> None:
    trace_dir = tmp_path / "private"
    trace_dir.mkdir(mode=0o700)
    victim = tmp_path / "victim.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    trace = trace_dir / "helper.log"
    try:
        trace.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    with pytest.raises((LocalFileSecurityError, OSError)):
        open_helper_trace(trace)
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"

    trace.unlink()
    with open_helper_trace(trace) as stream:
        stream.write(b"safe")
    assert trace.read_bytes() == b"safe"
    if os.name != "nt":
        assert stat.S_IMODE(trace.stat().st_mode) == 0o600


def test_redaction_covers_provider_keys_headers_urls_and_nested_data() -> None:
    assert "sk-test" not in sanitize_text("api_key=sk-test-not-a-real-key")
    assert "test-secret-token" not in sanitize_text(
        "Authorization: Bearer test-secret-token"
    )
    uri = sanitize_text("socks5://alice:secret@127.0.0.1:1080")
    assert "alice" not in uri and "secret" not in uri
    sanitized = sanitize_data(
        {
            "api_key": "sk-proj-1234567890",
            "nested": {"authorization": "Bearer abcdefghijk"},
        }
    )
    assert sanitized["api_key"] == "<redacted>"
    assert sanitized["nested"]["authorization"] == "<redacted>"


def test_log_text_neutralizes_forged_lines_and_controls() -> None:
    rendered = sanitize_log_text("channel title\nERROR forged\r\x1b[31m")
    assert "\n" not in rendered and "\r" not in rendered and "\x1b" not in rendered
    assert r"\nERROR forged\r\x1b" in rendered



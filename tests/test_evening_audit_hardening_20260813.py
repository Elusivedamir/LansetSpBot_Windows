from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _method(source: str, name: str, next_name: str) -> str:
    start = source.index(f"    def {name}(")
    end = source.index(f"    def {next_name}(", start)
    return source[start:end]


def test_openai_responses_explicitly_disable_application_state_storage() -> None:
    source = (ROOT / "services/openai_comment_service.py").read_text(
        encoding="utf-8"
    )
    start = source.index("    async def _request(")
    end = source.index("    async def generate_comment(", start)
    request = source[start:end]

    assert "create_response(" in request
    assert "store=False" in request


def test_shutdown_controls_the_warmup_lease_timer() -> None:
    source = (ROOT / "services/api_parts/task_queue.py").read_text(
        encoding="utf-8"
    )
    prepare = _method(source, "prepare_shutdown", "prepare_factory_reset")
    cancel = _method(source, "cancel_shutdown", "get_task")

    assert "self._warmup_lease_timer.stop()" in prepare
    assert "if not self._warmup_lease_timer.isActive():" in cancel
    assert "self._warmup_lease_timer.start()" in cancel


def test_secret_store_regression_awaits_async_runtime_cleanup() -> None:
    source = (ROOT / "tests/test_v4719_final_audit_fixes.py").read_text(
        encoding="utf-8"
    )
    start = source.index(
        "async def test_worker_defers_telegram_tasks_when_secret_store_is_unavailable"
    )
    end = source.index(
        "\ndef test_get_settings_does_not_mask_locked_secret_store", start
    )
    test_body = source[start:end]

    assert "await cleanup()" in test_body


def test_openai_capture_regression_pins_store_false() -> None:
    source = (ROOT / "tests/test_v483_openai_blends_operator_comment.py").read_text(
        encoding="utf-8"
    )
    assert 'assert captured["store"] is False' in source

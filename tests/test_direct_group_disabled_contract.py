from __future__ import annotations

import ast
from pathlib import Path


def _method_source(path: str, name: str) -> str:
    module = ast.parse(Path(path).read_text(encoding="utf-8"))
    method = next(
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )
    return ast.unparse(method)


def test_direct_group_delivery_uses_durable_receipt_contract() -> None:
    rendered = _method_source("services/comment_service.py", "send_direct_message")

    assert "reserve_direct_message_delivery" in rendered
    assert "finalize_direct_message_delivery" in rendered
    assert "mark_direct_message_delivery_uncertain" in rendered
    assert "unknown_result_code='direct_message_result_unknown'" in rendered
    assert "direct_group_disabled" not in rendered


def test_comment_slot_dispatches_direct_group_without_post_route() -> None:
    rendered = _method_source(
        "workers/comment_slot/runner.py", "_send_direct_group_message"
    )

    assert "post_id=None" in rendered
    assert "discussion_message_id=None" in rendered
    assert "send_direct_message" in rendered
    assert "CommentSlotPhase.SEND_STARTED" in rendered


def test_windows_proxy_selector_excludes_mtproxy() -> None:
    source = Path("gui/views/account_view.py").read_text(encoding="utf-8")
    instructions = Path("gui/views/instructions_view.py").read_text(
        encoding="utf-8"
    )

    assert 'addItems(["SOCKS5", "SOCKS4", "HTTP"])' in source
    assert 'addItems(["SOCKS5", "SOCKS4", "HTTP", "MTPROXY"])' not in source
    assert "SOCKS5, SOCKS4 и HTTP" in instructions
    assert "и MTProxy" not in instructions

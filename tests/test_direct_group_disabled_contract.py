from __future__ import annotations

import ast
from pathlib import Path


def test_direct_group_delivery_body_is_unconditionally_disabled() -> None:
    source = Path("services/comment_service.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    method = next(
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "send_direct_message"
    )
    executable = list(method.body)
    if (
        executable
        and isinstance(executable[0], ast.Expr)
        and isinstance(executable[0].value, ast.Constant)
        and isinstance(executable[0].value.value, str)
    ):
        executable = executable[1:]

    assert len(executable) == 1
    assert isinstance(executable[0], ast.Raise)
    rendered = ast.unparse(executable[0])
    assert "NonRetryableTelegramError" in rendered
    assert "direct_group_disabled" in rendered

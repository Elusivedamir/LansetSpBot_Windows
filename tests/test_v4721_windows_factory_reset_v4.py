from __future__ import annotations

import ast
from pathlib import Path


RUNTIME = Path("core/factory_reset_runtime.py")
MAIN = Path("main.py")


def test_windows_parent_wait_uses_process_handle_not_os_kill() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_wait_for_parent_exit_windows"
    )
    rendered = ast.get_source_segment(source, function) or ""
    assert "OpenProcess" in rendered
    assert "WaitForSingleObject" in rendered
    calls_os_kill = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
        and node.func.attr == "kill"
        for node in ast.walk(function)
    )
    assert not calls_os_kill


def test_windows_reset_retries_only_transient_lock_failures() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    assert "_reset_with_windows_lock_retries" in source
    assert '"winerror 32"' in source
    assert "exc.profile_restored is False" in source
    assert "FACTORY_RESET_RETRY" in source


def test_reset_result_is_shown_before_application_container_starts() -> None:
    source = MAIN.read_text(encoding="utf-8")
    result_index = source.index("factory_reset_result = consume_factory_reset_result")
    dialog_index = source.index("QMessageBox.critical(", result_index)
    container_index = source.index(
        "container = ApplicationContainer(config)", result_index
    )
    assert result_index < dialog_index < container_index
    startup_block = source[result_index:container_index]
    assert "QTimer.singleShot" not in startup_block

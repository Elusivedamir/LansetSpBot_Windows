from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _class_methods(path: str, class_name: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == class_name)
    return {item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_account_view_keeps_layout_shell_and_moves_behavior():
    methods = _class_methods("gui/views/account_view.py", "AccountView")
    assert "__init__" in methods
    assert "_sync_dynamic_layout" in methods
    assert "_select_account" not in methods
    assert "request_code" not in methods
    assert (ROOT / "gui/views/account_parts/account_ops.py").exists()
    assert (ROOT / "gui/views/account_parts/auth_flow.py").exists()


def test_commenting_view_keeps_ui_shell_and_moves_domain_behavior():
    methods = _class_methods("gui/views/commenting_view.py", "CommentingView")
    assert "__init__" in methods
    assert "start_campaign" not in methods
    assert "load_openai_configuration" not in methods
    assert "_save_daily_limit" not in methods
    assert (ROOT / "gui/views/commenting_parts/campaign.py").exists()
    assert (ROOT / "gui/views/commenting_parts/openai_panel.py").exists()


def test_repository_facades_keep_public_class_names_but_delegate_methods():
    account_methods = _class_methods("storage/db_accounts.py", "AccountRepositoryMixin")
    channel_methods = _class_methods("storage/db_channels.py", "ChannelRepositoryMixin")
    assert "register_telegram_account" not in account_methods
    assert "get_account_setting" not in account_methods
    assert "delete_channels_transactional" not in channel_methods
    assert "get_channels_for_commenting" not in channel_methods


def test_queue_worker_moves_only_support_concerns_not_execution_core():
    methods = _class_methods("workers/queue_worker.py", "QueueWorker")
    assert "run" in methods
    assert "_account_rpc_cooldown_remaining" not in methods
    assert "_set_active_task" not in methods
    assert (ROOT / "workers/queue_parts/cooldowns.py").exists()
    assert (ROOT / "workers/queue_parts/state.py").exists()


def test_large_files_are_materially_reduced_without_changing_entrypoint_names():
    assert (ROOT / "gui/views/account_view.py").stat().st_size < 45_000
    assert (ROOT / "gui/views/commenting_view.py").stat().st_size < 50_000
    assert (ROOT / "storage/db_accounts.py").stat().st_size < 20_000
    assert (ROOT / "storage/db_channels.py").stat().st_size < 25_000
    assert (ROOT / "workers/queue_worker.py").stat().st_size < 50_000

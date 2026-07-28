from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_launchers_install_the_hash_locked_openai_graph() -> None:
    paths = (
        ROOT / "RUN_FROM_SOURCE_WINDOWS.ps1",
        ROOT / "RUN_FROM_SOURCE_WINDOWS_DIRECT_314.ps1",
        ROOT / "build" / "build_windows_x64.ps1",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8-sig")
        assert "requirements-openai.lock" in text
        openai_lines = [
            line for line in text.splitlines() if "OpenAI" in line or "openai.lock" in line
        ]
        assert any("--require-hashes" in line for line in openai_lines), path
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "requirements-openai.lock" in requirements
    assert "requirements-openai.txt" not in requirements


def test_release_build_checks_lock_and_uses_it_for_sbom() -> None:
    text = (ROOT / "build" / "build_windows_x64.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "generate_openai_lock.py" in text
    assert "--check" in text
    sbom_line = next(line for line in text.splitlines() if "generate_sbom.py" in line)
    assert "requirements-openai.lock" in sbom_line
    assert "requirements-openai.txt" not in sbom_line


def test_manifest_falls_back_to_the_committed_path_set_without_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module(
        "v506_generate_manifest", ROOT / "tools" / "generate_manifest.py"
    )
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "nested" / "b.txt").write_text("b", encoding="utf-8")
    manifest = tmp_path / "SHA256SUMS.txt"
    manifest.write_text(
        "0" * 64 + "  a.txt\n" + "1" * 64 + "  nested/b.txt\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "MANIFEST", manifest)
    monkeypatch.setattr(module, "_git_tracked_files", lambda: (_ for _ in ()).throw(FileNotFoundError()))

    assert module.tracked_files() == ["a.txt", "nested/b.txt"]


def test_manifest_fallback_rejects_missing_or_escaping_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module(
        "v506_generate_manifest_unsafe", ROOT / "tools" / "generate_manifest.py"
    )
    manifest = tmp_path / "SHA256SUMS.txt"
    manifest.write_text("0" * 64 + "  ../outside.txt\n", encoding="utf-8")
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "MANIFEST", manifest)
    monkeypatch.setattr(module, "_git_tracked_files", lambda: (_ for _ in ()).throw(FileNotFoundError()))

    with pytest.raises(RuntimeError, match="unsafe"):
        module.tracked_files()


def test_openai_lock_renderer_merges_platform_wheel_hashes() -> None:
    module = _load_module(
        "v506_openai_lock", ROOT / "tools" / "generate_openai_lock.py"
    )
    first = {
        "openai": {"name": "openai", "version": "2.48.0", "hashes": {"a" * 64}},
        "jiter": {"name": "jiter", "version": "0.12.0", "hashes": {"b" * 64}},
    }
    second = {
        "openai": {"name": "openai", "version": "2.48.0", "hashes": {"a" * 64}},
        "jiter": {"name": "jiter", "version": "0.12.0", "hashes": {"c" * 64}},
    }

    merged = module._merge_graphs([first, second])
    rendered = module._render_lock(merged)

    assert "openai==2.48.0" in rendered
    assert "--hash=sha256:" + "b" * 64 in rendered
    assert "--hash=sha256:" + "c" * 64 in rendered


def test_openai_lock_rejects_graph_drift_between_supported_pythons() -> None:
    module = _load_module(
        "v506_openai_lock_drift", ROOT / "tools" / "generate_openai_lock.py"
    )
    first = {
        "openai": {"name": "openai", "version": "2.48.0", "hashes": {"a" * 64}}
    }
    second = {
        "openai": {"name": "openai", "version": "2.48.0", "hashes": {"a" * 64}},
        "extra": {"name": "extra", "version": "1", "hashes": {"b" * 64}},
    }
    with pytest.raises(RuntimeError, match="graphs differ"):
        module._merge_graphs([first, second])


def test_critical_coverage_includes_multiaccount_and_account_gui() -> None:
    module = _load_module(
        "v506_coverage_gate", ROOT / "tools" / "check_critical_coverage.py"
    )
    assert "multiaccount_runtime" in module.LINE_GROUP_THRESHOLDS
    assert "account_gui_lifecycle" in module.LINE_GROUP_THRESHOLDS
    multi_files, _minimum = module.LINE_GROUP_THRESHOLDS["multiaccount_runtime"]
    gui_files, _minimum = module.LINE_GROUP_THRESHOLDS["account_gui_lifecycle"]
    assert "services/account_sessions.py" in multi_files
    assert "services/multiaccount_scheduler.py" in multi_files
    assert "services/api_parts/accounts.py" in multi_files
    assert "storage/db_accounts.py" in multi_files
    assert "gui/views/account_view.py" in gui_files

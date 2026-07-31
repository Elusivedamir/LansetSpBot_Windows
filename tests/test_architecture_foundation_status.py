from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTAL_MODULES = {
    "core.campaign_state",
    "storage.atomic_workflow",
    "services.telegram_request_policy",
    "services.application_facade",
    "core.startup_pipeline",
    "core.rpc_audit",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_foundation_modules_are_explicitly_experimental():
    for module in EXPERIMENTAL_MODULES:
        path = PROJECT_ROOT.joinpath(*module.split(".")).with_suffix(".py")
        source = path.read_text(encoding="utf-8")
        assert 'ARCHITECTURE_STATUS = "experimental"' in source


def test_production_entrypoints_do_not_claim_experimental_foundations():
    production_roots = [
        PROJECT_ROOT / "main.py",
        *PROJECT_ROOT.glob("gui/**/*.py"),
        *PROJECT_ROOT.glob("workers/**/*.py"),
        *PROJECT_ROOT.glob("services/**/*.py"),
        *PROJECT_ROOT.glob("storage/**/*.py"),
        *PROJECT_ROOT.glob("core/**/*.py"),
    ]
    experimental_paths = {
        PROJECT_ROOT.joinpath(*module.split(".")).with_suffix(".py").resolve()
        for module in EXPERIMENTAL_MODULES
    }
    violations: list[str] = []
    for path in production_roots:
        if not path.is_file() or path.resolve() in experimental_paths:
            continue
        imported = _imports(path) & EXPERIMENTAL_MODULES
        if imported:
            violations.append(f"{path.relative_to(PROJECT_ROOT)}: {sorted(imported)}")
    assert violations == []

from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_readme_installs_openai_only_from_hash_locked_file():
    readme = (PROJECT_ROOT / "README.txt").read_text(encoding="utf-8")
    assert "--require-hashes -r requirements-openai.lock" in readme
    assert not re.search(r"pip install[^\n]*-r requirements-openai\.txt", readme)


def test_runtime_input_documents_openai_lock_as_install_contract():
    source = (PROJECT_ROOT / "requirements-runtime.in").read_text(encoding="utf-8")
    assert "requirements-openai.lock" in source
    assert "must never be installed directly" in source


def test_readme_documents_canonical_and_legacy_profile_paths():
    readme = (PROJECT_ROOT / "README.txt").read_text(encoding="utf-8")
    assert "%APPDATA%\\LansetSpBot" in readme
    assert "%APPDATA%\\Marlen is the legacy location" in readme
    assert "LANSETSPBOT_DATA_DIR" in readme
    assert "MARLEN_DATA_DIR" in readme

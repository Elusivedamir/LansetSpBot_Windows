from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "requirements-openai.txt"
LOCK = ROOT / "requirements-openai.lock"
GENERATOR = ROOT / "tools" / "generate_openai_lock.py"
COLORAMA_WHEEL_SHA256 = (
    "4f1d9991f5acc0ca119f9d443620b77f9d6b33703e51011c16baf57afb285fc6"
)


def _load_generator():
    spec = importlib.util.spec_from_file_location("v509_openai_lock", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_marker_dependency_is_an_explicit_source_pin() -> None:
    module = _load_generator()
    pins = module._read_source_pins(SOURCE)

    assert "openai==2.48.0" in pins
    assert "colorama==0.4.6" in pins
    assert module.REQUIRED_WINDOWS_SOURCE_PINS == {"colorama": "0.4.6"}


def test_colorama_windows_wheel_is_hash_locked() -> None:
    module = _load_generator()
    locked = module.parse_lock(LOCK)

    assert locked["colorama"]["version"] == "0.4.6"
    assert COLORAMA_WHEEL_SHA256 in locked["colorama"]["hashes"]


def test_generator_fails_closed_without_windows_marker_pin(tmp_path: Path) -> None:
    module = _load_generator()
    source = tmp_path / "requirements-openai.txt"
    source.write_text("openai==2.48.0\n", encoding="utf-8")

    with pytest.raises(
        RuntimeError,
        match=r"Missing required Windows OpenAI source pin: colorama==0\.4\.6",
    ):
        module._read_source_pins(source)

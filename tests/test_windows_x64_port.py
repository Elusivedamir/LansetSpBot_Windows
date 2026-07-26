from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_source_launcher_is_complete_and_x64_scoped() -> None:
    batch = (ROOT / "1_RUN_LANSETSPBOT_WINDOWS.bat").read_text(encoding="utf-8")
    launcher = (ROOT / "RUN_FROM_SOURCE_WINDOWS.ps1").read_text(encoding="utf-8-sig")

    assert "ExecutionPolicy Bypass" in batch
    assert "RUN_FROM_SOURCE_WINDOWS.ps1" in batch
    assert "Is64BitOperatingSystem" in launcher
    assert '"-3.13"' in launcher
    assert '"-3.13-64"' in launcher
    assert '"-3.14"' in launcher
    assert '"-3.14-64"' in launcher
    assert "Python 3.13 or Python 3.14 x64 could not be started." in launcher
    assert "windows-source-launcher-v5-python-$($Python.Version)" in launcher
    assert "sys.executable" in launcher
    assert "struct.calcsize('P')" in launcher
    assert "json.dumps" not in launcher
    assert "ConvertFrom-Json" not in launcher
    assert "Python Launcher $tag" in launcher
    assert "Detection details:" in launcher
    assert ".venv-windows-x64" in launcher
    assert "--require-hashes" in launcher
    assert "requirements-runtime.lock" in launcher
    assert (
        "main.py --self-test" not in launcher
    )  # invocation is argument-safe, not shell text
    assert "$VenvPython $MainScript --self-test" in launcher
    assert "Start-Process -FilePath $VenvPythonw" in launcher

    readme = (ROOT / "WINDOWS_X64_README.txt").read_text(encoding="utf-8")
    assert "автоматически" in readme
    assert "Python 3.13 x64" in readme
    assert "Python 3.14 x64" in readme


def test_windows_pyinstaller_spec_is_not_a_macos_bundle() -> None:
    spec = (ROOT / "build" / "LansetSpBot.windows.spec").read_text(encoding="utf-8")

    assert "BUNDLE(" not in spec
    assert "console=False" in spec
    assert "LansetSpBot.ico" in spec
    assert "windows_version_info.txt" in spec
    assert "name=app_name" in spec
    assert '"build/assets"' in spec


def test_windows_build_pipeline_has_native_self_tests_and_release_zip() -> None:
    build = (ROOT / "build" / "build_windows_x64.ps1").read_text(encoding="utf-8-sig")

    assert "Python 3.13 x64" in build
    assert "-m pytest -q" in build
    assert "-m ruff check" in build
    assert "-m mypy" in build
    assert "$BuiltExe --self-test" in build
    assert "LansetSpBot Проверка" in build
    assert "Compress-Archive" in build


def test_windows_version_generator_is_valid_python() -> None:
    path = ROOT / "build" / "generate_windows_version_info.py"
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_windows_icon_and_hash_locked_build_graph_are_present() -> None:
    icon = ROOT / "build" / "assets" / "LansetSpBot.ico"
    lock = (ROOT / "requirements-build-windows-x64.lock").read_text(encoding="utf-8")

    assert icon.is_file() and icon.stat().st_size > 10_000
    for package in (
        "pyinstaller==6.21.0",
        "pefile==2024.8.26",
        "pywin32-ctypes==0.2.3",
    ):
        assert package in lock
    assert lock.count("--hash=sha256:") == 7


def test_windows_direct_python314_fallback_avoids_native_c_probe() -> None:
    batch = (ROOT / "2_RUN_LANSETSPBOT_DIRECT_PY314.bat").read_text(encoding="utf-8")
    launcher = (ROOT / "RUN_FROM_SOURCE_WINDOWS_DIRECT_314.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "RUN_FROM_SOURCE_WINDOWS_DIRECT_314.ps1" in batch
    assert "-3.14 -m venv" in launcher
    assert "-3.14 --version" in launcher
    assert " -c " not in launcher
    assert "windows-direct-python-314-v1" in launcher
    assert "$VenvPython $MainScript --self-test" in launcher

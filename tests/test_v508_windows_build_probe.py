from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "build" / "build_windows_x64.ps1"


def test_windows_powershell_python_probe_preserves_pointer_format_literal() -> None:
    text = BUILD_SCRIPT.read_text(encoding="utf-8-sig")

    expected = (
        '$probe = "import struct,sys; assert sys.version_info[:2] == (3,13); '
        "assert 8*struct.calcsize('P') == 64; print(sys.executable)\""
    )
    assert expected in text

    # Windows PowerShell 5.1 strips embedded double quotes while forwarding
    # native-process arguments. This old form reaches Python as calcsize(P).
    assert 'struct.calcsize("P")' not in text


def test_build_probe_is_executed_through_python_launcher_313_x64() -> None:
    text = BUILD_SCRIPT.read_text(encoding="utf-8-sig")
    assert '$PythonArgs = @("-3.13-64")' in text
    assert "$PythonExecutable = & $py.Source @PythonArgs -c $probe" in text
    assert "Python 3.13 x64 is required for the reproducible Windows build." in text

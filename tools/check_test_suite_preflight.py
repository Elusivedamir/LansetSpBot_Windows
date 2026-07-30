from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
FAILURES: list[str] = []

SLOW_PATTERNS = (
    "range(1000)",
    "range(1040)",
    "time.sleep(180",
    "waitForDone(180_000",
)


def check_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    try:
        ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        FAILURES.append(f"{path.relative_to(ROOT)}: syntax error: {exc}")
        return

    if "def test_" not in text:
        return

    for pattern in SLOW_PATTERNS:
        if pattern in text and "with db.get_connection()" not in text:
            FAILURES.append(
                f"{path.relative_to(ROOT)}: heavy pattern {pattern!r} "
                "without an explicit transaction boundary"
            )


def main() -> int:
    test_files = sorted(TESTS.rglob("test_*.py"))
    if not test_files:
        print("No tests discovered", file=sys.stderr)
        return 1

    for path in test_files:
        check_file(path)

    watchdog = (ROOT / "tools" / "pytest_ci_watchdog.py").read_text(
        encoding="utf-8"
    )
    required = (
        "DEFAULT_TEST_TIMEOUT_SECONDS",
        "SLOW_TEST_TIMEOUT_SECONDS",
        "_timeout_for_node",
    )
    for name in required:
        if name not in watchdog:
            FAILURES.append(f"tools/pytest_ci_watchdog.py: missing {name}")

    if FAILURES:
        print("Test-suite preflight failed:", file=sys.stderr)
        for failure in FAILURES:
            print(f" - {failure}", file=sys.stderr)
        return 1

    print(f"Test-suite preflight passed for {len(test_files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

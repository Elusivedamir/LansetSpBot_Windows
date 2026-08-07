from __future__ import annotations

from tools.check_critical_coverage import (
    BRANCH_GROUP_MEMBER_MINIMUM,
    LINE_GROUP_MEMBER_MINIMUM,
    _check_group_member_branches,
    _check_group_member_lines,
)


def _entry(
    *,
    statements: int = 100,
    covered_lines: int = 100,
    branches: int = 20,
    covered_branches: int = 20,
) -> dict[str, object]:
    return {
        "summary": {
            "num_statements": statements,
            "covered_lines": covered_lines,
            "num_branches": branches,
            "covered_branches": covered_branches,
            "percent_covered": float(covered_lines),
        }
    }


def test_line_member_floor_catches_gap_hidden_by_group_average() -> None:
    files: dict[str, object] = {
        "large.py": _entry(statements=900, covered_lines=900),
        "critical.py": _entry(statements=100, covered_lines=0),
    }

    failures = _check_group_member_lines(
        files,
        group_name="runtime",
        filenames=("large.py", "critical.py"),
        minimum=LINE_GROUP_MEMBER_MINIMUM,
    )

    assert failures == [
        "runtime:critical.py lines: 0.0% "
        f"< {LINE_GROUP_MEMBER_MINIMUM:.1f}%"
    ]


def test_branch_member_floor_catches_gap_and_skips_branchless_files() -> None:
    files: dict[str, object] = {
        "covered.py": _entry(branches=40, covered_branches=40),
        "critical.py": _entry(branches=20, covered_branches=0),
        "constants.py": _entry(branches=0, covered_branches=0),
    }

    failures = _check_group_member_branches(
        files,
        group_name="runtime",
        filenames=("covered.py", "critical.py", "constants.py"),
        minimum=BRANCH_GROUP_MEMBER_MINIMUM,
    )

    assert failures == [
        "runtime:critical.py branches: 0.0% "
        f"< {BRANCH_GROUP_MEMBER_MINIMUM:.1f}%"
    ]

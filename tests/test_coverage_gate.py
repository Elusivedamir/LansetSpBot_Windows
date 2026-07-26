from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.check_critical_coverage import (
    BRANCH_GROUP_THRESHOLDS,
    BRANCH_THRESHOLDS,
    LINE_GROUP_THRESHOLDS,
    THRESHOLDS,
)


def _payload(path_style: str) -> dict[str, object]:
    files: dict[str, object] = {}
    for filename, threshold in THRESHOLDS.items():
        if path_style == "windows":
            report_name = filename.replace("/", "\\")
        elif path_style == "absolute_windows":
            report_name = "C:\\workspace\\marlen\\" + filename.replace("/", "\\")
        else:
            report_name = filename
        files[report_name] = {
            "summary": {
                "percent_covered": threshold,
                "num_statements": 100,
                "covered_lines": int(threshold),
                "num_branches": 100,
                "covered_branches": 100,
            }
        }
    for filename, threshold in BRANCH_THRESHOLDS.items():
        if path_style == "windows":
            report_name = filename.replace("/", "\\")
        elif path_style == "absolute_windows":
            report_name = "C:\\workspace\\marlen\\" + filename.replace("/", "\\")
        else:
            report_name = filename
        entry = files.setdefault(report_name, {"summary": {}})
        summary = entry["summary"]
        summary.setdefault("percent_covered", 100.0)
        summary.setdefault("num_statements", 100)
        summary.setdefault("covered_lines", 100)
        summary["num_branches"] = 100
        summary["covered_branches"] = int(threshold)
    for _group_name, (filenames, _threshold) in LINE_GROUP_THRESHOLDS.items():
        for filename in filenames:
            if path_style == "windows":
                report_name = filename.replace("/", "\\")
            elif path_style == "absolute_windows":
                report_name = "C:\\workspace\\marlen\\" + filename.replace("/", "\\")
            else:
                report_name = filename
            files.setdefault(
                report_name,
                {
                    "summary": {
                        "percent_covered": 100.0,
                        "num_statements": 100,
                        "covered_lines": 100,
                        "num_branches": 100,
                        "covered_branches": 100,
                    }
                },
            )
    for _group_name, (filenames, _threshold) in BRANCH_GROUP_THRESHOLDS.items():
        for filename in filenames:
            if path_style == "windows":
                report_name = filename.replace("/", "\\")
            elif path_style == "absolute_windows":
                report_name = "C:\\workspace\\marlen\\" + filename.replace("/", "\\")
            else:
                report_name = filename
            entry = files.setdefault(report_name, {"summary": {}})
            summary = entry["summary"]
            summary.setdefault("percent_covered", 100.0)
            summary.setdefault("num_statements", 100)
            summary.setdefault("covered_lines", 100)
            summary["num_branches"] = 100
            summary["covered_branches"] = 100
    return {"files": files}


def _run_gate(
    tmp_path: Path, payload: dict[str, object]
) -> subprocess.CompletedProcess[str]:
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "tools/check_critical_coverage.py", str(report)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_coverage_gate_accepts_windows_separators(tmp_path: Path) -> None:
    result = _run_gate(tmp_path, _payload("windows"))
    assert result.returncode == 0, result.stderr


def test_coverage_gate_accepts_absolute_windows_paths(tmp_path: Path) -> None:
    result = _run_gate(tmp_path, _payload("absolute_windows"))
    assert result.returncode == 0, result.stderr

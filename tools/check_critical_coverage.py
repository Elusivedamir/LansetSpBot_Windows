#!/usr/bin/env python3
"""Fail CI when production-critical modules lose meaningful test coverage."""

from __future__ import annotations

import json
import sys
from pathlib import Path

LINE_THRESHOLDS = {
    "core/composition.py": 90.0,
    "workers/handler_registry.py": 80.0,
    "storage/database.py": 85.0,
    "storage/db_tasks.py": 55.0,
    "storage/db_channels.py": 60.0,
    "workers/queue_worker.py": 40.0,
    "workers/handlers/warmup_step.py": 40.0,
}

LINE_GROUP_THRESHOLDS = {
    "telegram_service": (
        (
            "services/telegram/transport.py",
            "services/telegram/dialogs.py",
            "services/telegram/membership.py",
            "services/telegram/messaging.py",
            "services/telegram/posts.py",
        ),
        54.0,
    ),
    "service_api": (
        (
            "services/api.py",
            "services/api_parts/task_queue.py",
            "services/api_parts/settings.py",
            "services/api_parts/comments.py",
            "services/api_parts/joins.py",
        ),
        50.0,
    ),
    "comment_campaign_repository": (
        (
            "storage/comment_campaigns/history.py",
            "storage/comment_campaigns/campaigns.py",
            "storage/comment_campaigns/schedule.py",
            "storage/comment_campaigns/finalization.py",
            "storage/comment_campaigns/reconciliation.py",
        ),
        45.0,
    ),
    "join_campaign_repository": (
        (
            "storage/join_campaigns/saved_dialogs.py",
            "storage/join_campaigns/campaigns.py",
            "storage/join_campaigns/schedule.py",
            "storage/join_campaigns/finalization.py",
            "storage/join_campaigns/recovery.py",
            "storage/join_campaigns/guards.py",
        ),
        45.0,
    ),
    "database_schema": (
        (
            "storage/schema/core.py",
            "storage/schema/legacy.py",
            "storage/schema/v14.py",
            "storage/schema/v15.py",
            "storage/schema/bootstrap.py",
        ),
        70.0,
    ),
    "multiaccount_runtime": (
        (
            "services/account_context.py",
            "services/account_runtime_manager.py",
            "services/account_sessions.py",
            "services/multiaccount_scheduler.py",
            "services/api_parts/accounts.py",
            "storage/account_database_view.py",
            "storage/db_accounts.py",
            "storage/migrations/multiaccount_v31.py",
        ),
        55.0,
    ),
    "account_gui_lifecycle": (
        (
            "gui/account_manager_panel.py",
            "gui/gui_service_adapter.py",
            "gui/views/account_view.py",
        ),
        65.0,
    ),
    "warmup_runtime": (
        (
            "core/warmup_planner.py",
            "core/warmup_scenarios.py",
            "services/api_parts/warmup.py",
            "storage/db_warmup.py",
            "workers/handlers/warmup_step.py",
        ),
        45.0,
    ),
}

# A group-level average must not let a heavily tested large module hide an
# effectively untested member of the same critical subsystem.
LINE_GROUP_MEMBER_MINIMUM = 30.0

# The most stateful modules must exercise decision paths, not only execute lines.
BRANCH_THRESHOLDS = {
    "workers/handlers/join_slot.py": 30.0,
    "workers/queue_worker.py": 30.0,
    "workers/handlers/warmup_step.py": 12.0,
}

BRANCH_GROUP_THRESHOLDS = {
    "comment_slot_state_machine": (
        (
            "workers/comment_slot/handler.py",
            "workers/comment_slot/finalization.py",
        ),
        30.0,
    ),
    "comment_campaign_repository": (
        (
            "storage/comment_campaigns/history.py",
            "storage/comment_campaigns/campaigns.py",
            "storage/comment_campaigns/schedule.py",
            "storage/comment_campaigns/finalization.py",
            "storage/comment_campaigns/reconciliation.py",
        ),
        25.0,
    ),
    "join_campaign_repository": (
        (
            "storage/join_campaigns/saved_dialogs.py",
            "storage/join_campaigns/campaigns.py",
            "storage/join_campaigns/schedule.py",
            "storage/join_campaigns/finalization.py",
            "storage/join_campaigns/recovery.py",
            "storage/join_campaigns/guards.py",
        ),
        25.0,
    ),
    "multiaccount_runtime": (
        (
            "services/account_context.py",
            "services/account_runtime_manager.py",
            "services/account_sessions.py",
            "services/multiaccount_scheduler.py",
            "services/api_parts/accounts.py",
            "storage/account_database_view.py",
            "storage/db_accounts.py",
            "storage/migrations/multiaccount_v31.py",
        ),
        40.0,
    ),
    "account_gui_lifecycle": (
        (
            "gui/account_manager_panel.py",
            "gui/gui_service_adapter.py",
            "gui/views/account_view.py",
        ),
        35.0,
    ),
    "warmup_runtime": (
        (
            "core/warmup_planner.py",
            "services/api_parts/warmup.py",
            "storage/db_warmup.py",
            "workers/handlers/warmup_step.py",
        ),
        28.0,
    ),
}

BRANCH_GROUP_MEMBER_MINIMUM = 10.0

# Backward-compatible public name used by existing tests and tooling.
THRESHOLDS = LINE_THRESHOLDS


def _normalize_report_path(filename: str) -> str:
    """Return a platform-independent coverage filename.

    coverage.py emits backslashes on Windows even when ``relative_files`` is
    enabled. CI thresholds are intentionally written with POSIX separators,
    so normalize both separators, leading ``./`` segments and case here.
    """

    normalized = filename.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.casefold()


def _find_coverage_entry(files: dict[str, object], filename: str) -> object | None:
    wanted = _normalize_report_path(filename)
    normalized_files = {
        _normalize_report_path(report_name): entry
        for report_name, entry in files.items()
    }
    exact = normalized_files.get(wanted)
    if exact is not None:
        return exact

    # Be tolerant of absolute filenames produced by unusual coverage settings.
    suffix = "/" + wanted
    matches = [
        entry
        for report_name, entry in normalized_files.items()
        if report_name.endswith(suffix)
    ]
    return matches[0] if len(matches) == 1 else None


def _summary_counts(
    files: dict[str, object], filename: str
) -> tuple[int, int, int, int] | None:
    entry = _find_coverage_entry(files, filename)
    if not isinstance(entry, dict):
        return None
    summary = entry.get("summary", {})
    if not isinstance(summary, dict):
        return None
    return (
        int(summary.get("num_statements", 0)),
        int(summary.get("covered_lines", 0)),
        int(summary.get("num_branches", 0)),
        int(summary.get("covered_branches", 0)),
    )


def _aggregate_summary(
    files: dict[str, object], filenames: tuple[str, ...]
) -> tuple[int, int, int, int] | None:
    statements = covered_lines = branches = covered_branches = 0
    for filename in filenames:
        counts = _summary_counts(files, filename)
        if counts is None:
            return None
        file_statements, file_covered_lines, file_branches, file_covered_branches = counts
        statements += file_statements
        covered_lines += file_covered_lines
        branches += file_branches
        covered_branches += file_covered_branches
    return statements, covered_lines, branches, covered_branches


def _check_group_member_lines(
    files: dict[str, object],
    *,
    group_name: str,
    filenames: tuple[str, ...],
    minimum: float,
) -> list[str]:
    failures: list[str] = []
    for filename in filenames:
        counts = _summary_counts(files, filename)
        if counts is None:
            continue
        statements, covered_lines, _branches, _covered_branches = counts
        if statements <= 0:
            continue
        actual = covered_lines * 100.0 / statements
        print(
            f"{group_name}:{filename} lines: {actual:.1f}% "
            f"(member minimum {minimum:.1f}%)"
        )
        if actual + 1e-9 < minimum:
            failures.append(
                f"{group_name}:{filename} lines: {actual:.1f}% < {minimum:.1f}%"
            )
    return failures


def _check_group_member_branches(
    files: dict[str, object],
    *,
    group_name: str,
    filenames: tuple[str, ...],
    minimum: float,
) -> list[str]:
    failures: list[str] = []
    for filename in filenames:
        counts = _summary_counts(files, filename)
        if counts is None:
            continue
        _statements, _covered_lines, branches, covered_branches = counts
        # Files without decisions should not fail a branch floor.
        if branches <= 0:
            continue
        actual = covered_branches * 100.0 / branches
        print(
            f"{group_name}:{filename} branches: {actual:.1f}% "
            f"(member minimum {minimum:.1f}%)"
        )
        if actual + 1e-9 < minimum:
            failures.append(
                f"{group_name}:{filename} branches: {actual:.1f}% < {minimum:.1f}%"
            )
    return failures


def main() -> int:
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not read {report_path}: {exc}", file=sys.stderr)
        return 2

    raw_files = payload.get("files", {})
    if not isinstance(raw_files, dict):
        print("ERROR: coverage report has no valid files mapping", file=sys.stderr)
        return 2

    files: dict[str, object] = raw_files
    failures: list[str] = []
    for filename, minimum in LINE_THRESHOLDS.items():
        entry = _find_coverage_entry(files, filename)
        if not entry:
            failures.append(f"{filename}: missing from coverage report")
            continue
        if not isinstance(entry, dict):
            failures.append(f"{filename}: invalid coverage entry")
            continue
        summary = entry.get("summary", {})
        if not isinstance(summary, dict):
            failures.append(f"{filename}: invalid coverage summary")
            continue
        statements = int(summary.get("num_statements", 0))
        covered_lines = int(summary.get("covered_lines", 0))
        if statements > 0:
            actual = covered_lines * 100.0 / statements
        else:
            actual = float(summary.get("percent_covered", 0.0))
        print(f"{filename} lines: {actual:.1f}% (minimum {minimum:.1f}%)")
        if actual + 1e-9 < minimum:
            failures.append(f"{filename} lines: {actual:.1f}% < {minimum:.1f}%")

    for group_name, (filenames, minimum) in LINE_GROUP_THRESHOLDS.items():
        aggregate = _aggregate_summary(files, filenames)
        if aggregate is None:
            failures.append(
                f"{group_name}: one or more files missing from coverage report"
            )
            continue
        statements, covered_lines, _branches, _covered_branches = aggregate
        actual = covered_lines * 100.0 / statements if statements > 0 else 0.0
        print(f"{group_name} lines: {actual:.1f}% (minimum {minimum:.1f}%)")
        if actual + 1e-9 < minimum:
            failures.append(f"{group_name} lines: {actual:.1f}% < {minimum:.1f}%")
        failures.extend(
            _check_group_member_lines(
                files,
                group_name=group_name,
                filenames=filenames,
                minimum=LINE_GROUP_MEMBER_MINIMUM,
            )
        )

    for filename, minimum in BRANCH_THRESHOLDS.items():
        entry = _find_coverage_entry(files, filename)
        if not entry or not isinstance(entry, dict):
            failures.append(f"{filename}: missing branch coverage entry")
            continue
        summary = entry.get("summary", {})
        if not isinstance(summary, dict):
            failures.append(f"{filename}: invalid branch coverage summary")
            continue
        branches = int(summary.get("num_branches", 0))
        covered = int(summary.get("covered_branches", 0))
        if branches <= 0:
            failures.append(
                f"{filename}: branch data missing; run coverage with branch=True"
            )
            continue
        actual = covered * 100.0 / branches
        print(f"{filename} branches: {actual:.1f}% (minimum {minimum:.1f}%)")
        if actual + 1e-9 < minimum:
            failures.append(f"{filename} branches: {actual:.1f}% < {minimum:.1f}%")

    for group_name, (filenames, minimum) in BRANCH_GROUP_THRESHOLDS.items():
        aggregate = _aggregate_summary(files, filenames)
        if aggregate is None:
            failures.append(f"{group_name}: one or more branch files missing")
            continue
        _statements, _covered_lines, branches, covered = aggregate
        if branches <= 0:
            failures.append(
                f"{group_name}: branch data missing; run coverage with branch=True"
            )
            continue
        actual = covered * 100.0 / branches
        print(f"{group_name} branches: {actual:.1f}% (minimum {minimum:.1f}%)")
        if actual + 1e-9 < minimum:
            failures.append(f"{group_name} branches: {actual:.1f}% < {minimum:.1f}%")
        failures.extend(
            _check_group_member_branches(
                files,
                group_name=group_name,
                filenames=filenames,
                minimum=BRANCH_GROUP_MEMBER_MINIMUM,
            )
        )

    if failures:
        print("Critical coverage gate failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

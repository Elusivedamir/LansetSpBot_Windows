from __future__ import annotations

import pytest

from tools.enforce_dependency_audit_policy import vulnerable_dependencies


def test_dependency_audit_policy_accepts_empty_vulnerability_lists():
    assert vulnerable_dependencies(
        {"dependencies": [{"name": "example", "version": "1.0", "vulns": []}]}
    ) == []


def test_dependency_audit_policy_rejects_any_unresolved_vulnerability():
    dependency = {
        "name": "example",
        "version": "1.0",
        "vulns": [{"id": "GHSA-test"}],
    }
    assert vulnerable_dependencies({"dependencies": [dependency]}) == [dependency]


def test_dependency_audit_policy_fails_closed_on_invalid_schema():
    with pytest.raises(ValueError):
        vulnerable_dependencies([])
    with pytest.raises(ValueError):
        vulnerable_dependencies({"dependencies": [{"name": "example"}]})

from __future__ import annotations

from pathlib import Path


def test_observability_is_integrated_directly_without_runtime_monkeypatch():
    root = Path(__file__).resolve().parents[1]
    account = (root / "gui/views/account_view.py").read_text(encoding="utf-8")
    audience = (root / "gui/views/audience_parser_view.py").read_text(encoding="utf-8")
    commenting = (root / "gui/views/commenting_view.py").read_text(encoding="utf-8")
    combined = "\n".join((account, audience, commenting))

    assert "AccountHealthCard" in account
    assert "find_resumable_audience_task" in audience
    assert "Продолжить" in audience
    assert "Начать заново" in audience
    assert "campaign_stats_label" in commenting
    assert "# OBSERVABILITY-PACKAGE-V3" in combined
    assert "observability_runtime" not in combined
    assert "MethodType" not in combined
    assert "install_account_observability" not in combined
    assert "install_audience_observability" not in combined
    assert "install_commenting_observability" not in combined

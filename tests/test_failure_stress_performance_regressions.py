from __future__ import annotations

from pathlib import Path


def test_audience_parser_overview_does_not_run_in_gui_thread():
    root = Path(__file__).resolve().parents[1]
    source = (root / "gui" / "views" / "audience_parser_view.py").read_text(
        encoding="utf-8"
    )
    refresh = source.split(
        "    def _refresh_account_options(self) -> None:", 1
    )[1].split("    def _periodic_account_refresh", 1)[0]

    assert "self._account_refresh_job" in source
    assert "BackgroundCall(" in refresh
    assert "QThreadPool.globalInstance().start(job)" in refresh
    assert "rows = self._workflow_accounts()" not in refresh
    assert "self.account_refresh_timer.setInterval(10_000)" in source


def test_links_large_table_uses_model_and_background_loading():
    root = Path(__file__).resolve().parents[1]
    source = (root / "gui" / "views" / "links_view.py").read_text(
        encoding="utf-8"
    )
    load = source.split("    def load_channels(self):", 1)[1]

    assert "class LinkTableModel(QAbstractTableModel):" in source
    assert "self.table = QTableView()" in source
    assert "QTableWidgetItem" not in source
    assert "self._load_job: BackgroundCall | None = None" in source
    assert "QThreadPool.globalInstance().start(job)" in load
    assert "view.link_model.replace_rows(rows)" in load


def test_release_gate_searches_bundle_recursively_for_dev_packages():
    root = Path(__file__).resolve().parents[1]
    source = (root / "build" / "build_windows_x64.ps1").read_text(
        encoding="utf-8"
    )
    assert "Get-ChildItem -LiteralPath $BundleRoot -Recurse -Force" in source
    assert "mypy" in source
    assert "setuptools" in source

from __future__ import annotations

from pathlib import Path


def test_sidebar_navigation_is_debounced_and_does_not_fan_out_refreshes():
    root = Path(__file__).resolve().parents[1]
    source = (root / "gui" / "main_window.py").read_text(encoding="utf-8")

    assert "self._page_activation_timer.setSingleShot(True)" in source
    assert "self._page_activation_timer.setInterval(120)" in source
    assert "def _activate_pending_page(self) -> None:" in source
    assert "if self._active_page_index == index:" in source
    assert "previous = self._page_views[self._active_page_index]" in source
    assert "self._pending_page_index = index" in source
    assert "self._page_activation_timer.start()" in source

    # Regression guard for the old O(number-of-pages) activation fan-out on
    # every click. The only all-page loop left is the one-time initialization.
    change_page = source.split("    def _change_page(self, index: int):", 1)[1]
    change_page = change_page.split("    def _activate_pending_page", 1)[0]
    assert "for page_index, view in enumerate(self._page_views):" not in change_page
    assert change_page.count("for view in self._page_views:") == 1

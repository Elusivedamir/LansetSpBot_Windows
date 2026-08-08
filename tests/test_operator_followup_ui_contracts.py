from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _literal_assignment(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"assignment {name} not found in {path}")


def test_commenting_uses_real_internal_sections_not_scroll_anchor() -> None:
    source = (ROOT / "gui" / "views" / "commenting_view.py").read_text(
        encoding="utf-8"
    )
    assert "self.comments_section = QWidget()" in source
    assert "self.comments_section.setVisible(show_comments)" in source
    assert "self.campaign_section.setVisible(not show_comments)" in source
    assert "self.campaign_section.hide()" in source
    assert "def _show_section" in source
    assert "ensureWidgetVisible" not in source
    assert "def _jump_to_section" not in source
    assert "campaign_settings_layout.addWidget(self.continuous)" in source
    assert "campaign_settings_layout.addWidget(self.daily_limit_slider)" in source
    assert "campaign_section_layout.addWidget(self.campaign_settings_card)" in source
    assert "comments_layout.addWidget(self.continuous)" not in source
    assert "comments_layout.addWidget(self.daily_limit_slider)" not in source


def test_instruction_capture_targets_current_sidebar_indices() -> None:
    path = ROOT / "tools" / "capture_instruction_screenshots.py"
    assert _literal_assignment(path, "PAGES") == (
        (0, "01_account.png"),
        (2, "02_channels.png"),
        (3, "03_links.png"),
        (4, "04_comments.png"),
        (7, "05_instructions.png"),
    )


def test_instruction_capture_explicitly_loads_cyrillic_windows_font() -> None:
    source = (ROOT / "tools" / "capture_instruction_screenshots.py").read_text(
        encoding="utf-8"
    )
    assert "QT_QPA_FONTDIR" in source
    assert "QFontDatabase.addApplicationFont" in source
    assert '"Segoe UI"' in source
    assert "inFontUcs4" in source
    assert "_force_capture_font(window, capture_font_family)" in source


def test_single_shareable_log_and_export_launcher_contract() -> None:
    logging_source = (ROOT / "core" / "logging_setup.py").read_text(
        encoding="utf-8"
    )
    db_source = (ROOT / "storage" / "db_settings.py").read_text(
        encoding="utf-8"
    )
    build_source = (ROOT / "build" / "build_windows_x64.ps1").read_text(
        encoding="utf-8"
    )
    assert "FILE_LOG_BACKUP_COUNT = 0" in logging_source
    assert "FILE_LOG_RETAIN_BYTES = 12 * 1024 * 1024" in logging_source
    assert "def mirror_activity_log" in logging_source
    assert "mirror_activity_log(" in db_source
    assert "3_EXPORT_TEST_LOG.bat" in build_source
    assert "LansetSpBot_TEST_LOG.txt" in build_source

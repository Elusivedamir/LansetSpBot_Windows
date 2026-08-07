from __future__ import annotations

from gui.theme import (
    DEFAULT_THEME_KEY,
    TELEGRAM_PREMIUM_QSS,
    THEME_OPTIONS,
    THEME_STYLESHEETS,
    normalize_theme_key,
    theme_stylesheet,
)


EXPECTED_THEMES = (
    ("velvet-night", "Velvet Night"),
    ("aurora-prestige", "Aurora Prestige 2.0"),
    ("telegram-obsidian", "Telegram Obsidian"),
    ("midnight-glass", "Midnight Glass"),
    ("cyber-azure", "Cyber Azure"),
    ("crystal-premium", "Crystal Premium"),
)


def test_velvet_night_is_the_default_and_all_six_themes_are_registered() -> None:
    assert DEFAULT_THEME_KEY == "velvet-night"
    assert THEME_OPTIONS == EXPECTED_THEMES
    assert tuple(THEME_STYLESHEETS) == tuple(key for key, _label in EXPECTED_THEMES)
    assert TELEGRAM_PREMIUM_QSS == theme_stylesheet(DEFAULT_THEME_KEY)


def test_every_theme_keeps_critical_widget_and_popup_contracts() -> None:
    required = (
        "QMainWindow, QWidget#rootWindow",
        "QListWidget#navigation",
        "QPushButton#primaryButton",
        "QPushButton#accountDeleteButton",
        "QComboBox#themeSelector",
        "QComboBox QAbstractItemView",
        "QAbstractSpinBox",
        "QPlainTextEdit#activityLog",
    )
    for key, _label in EXPECTED_THEMES:
        stylesheet = theme_stylesheet(key)
        assert len(stylesheet) > 10_000
        for selector in required:
            assert selector in stylesheet, f"{key}: missing {selector}"


def test_unknown_or_empty_theme_fails_closed_to_velvet_night() -> None:
    assert normalize_theme_key(None) == DEFAULT_THEME_KEY
    assert normalize_theme_key("") == DEFAULT_THEME_KEY
    assert normalize_theme_key("not-a-theme") == DEFAULT_THEME_KEY
    assert normalize_theme_key(" CYBER-AZURE ") == "cyber-azure"


def test_every_theme_finishes_with_red_off_green_on_toggle_contract() -> None:
    for key, _label in EXPECTED_THEMES:
        stylesheet = theme_stylesheet(key)
        unchecked = stylesheet.rfind("QCheckBox::indicator:unchecked {")
        checked = stylesheet.rfind("QCheckBox::indicator:checked {")
        assert unchecked >= 0
        assert checked >= 0
        assert "#9C3542" in stylesheet[unchecked : unchecked + 180]
        assert "#238B57" in stylesheet[checked : checked + 180]

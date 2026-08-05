from __future__ import annotations

# Central Aurora Premium tokens. Do not duplicate active colors in view modules.
AURORA_GREEN = "#39FF14"
AURORA_GREEN_HOVER = "#4CFF2B"
AURORA_GREEN_GLOW = "#66FF47"
AURORA_BACKGROUND = "#050817"
CARD_BACKGROUND = "rgba(10, 17, 34, 224)"
CARD_BACKGROUND_STRONG = "rgba(8, 13, 27, 242)"
CARD_BORDER = "rgba(126, 165, 255, 72)"
TEXT_PRIMARY = "#F6FAFF"
TEXT_MUTED = "#A7B4C7"
DANGER = "#FF4D67"
DANGER_HOVER = "#FF667C"


def _build_qss() -> str:
    return f"""
QMainWindow, QWidget#rootWindow, QWidget#contentHost {{
    background: transparent;
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 14px;
}}
QWidget {{
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI", Arial, sans-serif;
}}
QWidget#auroraBackground {{ background: {AURORA_BACKGROUND}; }}
QFrame#sidebar {{
    background: rgba(6, 11, 25, 232);
    border-right: 1px solid rgba(92, 131, 224, 80);
}}
QLabel#brandMark {{
    min-width: 72px; max-width: 72px; min-height: 44px; max-height: 44px;
    border-radius: 14px;
    background: rgba(13, 26, 48, 220);
    border: 1px solid rgba(86, 149, 255, 96);
    color: {AURORA_GREEN}; font-size: 22px; font-weight: 900;
    qproperty-alignment: AlignCenter;
}}
QLabel#brandTitle {{ color: #FFFFFF; font-size: 20px; font-weight: 900; }}
QLabel#brandSubtitle, QLabel#pageSubtitle, QLabel#mutedText {{
    color: {TEXT_MUTED}; font-size: 13px;
}}
QLabel#pageTitle {{ color: #FFFFFF; font-size: 30px; font-weight: 850; }}
QLabel#cardTitle, QLabel#statusTitle {{ color: #FFFFFF; font-size: 16px; font-weight: 750; }}
QLabel#selectedAccountIdentity {{ color: #FFFFFF; font-size: 17px; font-weight: 800; }}
QLabel#dialogText {{ color: #D8E2EF; font-size: 14px; }}
QLabel#dangerTitle {{ color: {DANGER_HOVER}; font-size: 17px; font-weight: 850; }}
QLabel#dangerText {{ color: #FFB4C0; font-size: 13px; }}
QLabel#statusDotOnline, QLabel#accountStateOnline {{ color: {AURORA_GREEN}; font-size: 24px; }}
QLabel#statusDotOffline, QLabel#accountStateDisconnected, QLabel#accountStateStopped {{ color: #8793A4; font-size: 24px; }}
QLabel#accountStatePaused {{ color: #FFE45C; font-size: 24px; }}
QLabel#accountStateWarning {{ color: #FFA94D; font-size: 24px; }}
QLabel#accountStateError {{ color: {DANGER}; font-size: 24px; }}
QLabel#accountStateText {{ color: {TEXT_PRIMARY}; font-size: 15px; font-weight: 750; }}

QFrame#card, QFrame#statusCard, QFrame#infoCard, QFrame#accountManagerCard,
QFrame#selectedAccountCard, QFrame#collapsibleCard, QFrame#activityPanel {{
    background: {CARD_BACKGROUND};
    border: 1px solid {CARD_BORDER};
    border-radius: 16px;
}}
QFrame#dangerCard {{
    background: rgba(37, 9, 20, 220);
    border: 1px solid rgba(255, 77, 103, 140);
    border-radius: 16px;
}}
QFrame#authChallengeCard, QFrame#commentVariantRow {{
    background: rgba(5, 11, 24, 225);
    border: 1px solid rgba(88, 132, 220, 78);
    border-radius: 12px;
}}
QStackedWidget#contentStack, QScrollArea#pageScroll,
QScrollArea#pageScroll > QWidget > QWidget, QStackedWidget#instructionStack,
QScrollArea#instructionStepScroll, QScrollArea#instructionStepScroll > QWidget > QWidget {{
    background: transparent; border: none;
}}

QListWidget#navigation {{ background: transparent; border: none; outline: none; padding: 4px; font-size: 16px; font-weight: 700; }}
QListWidget#navigation::item {{ color: #C5D2E5; padding: 14px 16px; margin: 3px 0; border-radius: 12px; }}
QListWidget#navigation::item:hover {{ background: rgba(57, 255, 20, 22); color: {AURORA_GREEN_HOVER}; border: 1px solid rgba(57, 255, 20, 74); }}
QListWidget#navigation::item:selected {{ background: rgba(57, 255, 20, 32); color: {AURORA_GREEN}; border: 1px solid rgba(57, 255, 20, 130); }}

QLineEdit, QPlainTextEdit, QTextEdit, QTextBrowser, QComboBox, QAbstractSpinBox {{
    background: rgba(4, 9, 21, 230);
    border: 1px solid rgba(120, 151, 205, 92);
    border-radius: 10px; padding: 9px 11px; color: #FFFFFF;
    selection-background-color: rgba(57, 255, 20, 110);
}}
QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover, QTextBrowser:hover,
QComboBox:hover, QAbstractSpinBox:hover {{ border: 1px solid rgba(135, 170, 235, 150); }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QTextBrowser:focus,
QComboBox:focus, QAbstractSpinBox:focus {{ border: 1px solid {AURORA_GREEN}; background: rgba(5, 12, 25, 245); }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox QAbstractItemView {{
    background: #E6EBF1; border: 1px solid #94A4B8; border-radius: 9px;
    padding: 4px; color: #17202A; outline: none;
    selection-background-color: {AURORA_GREEN}; selection-color: #061006;
}}
QComboBox QAbstractItemView::item {{ min-height: 28px; padding: 4px 10px; }}
QComboBox QAbstractItemView::item:disabled {{ color: #687488; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 20px; border: none; background: transparent; }}
QAbstractSpinBox::up-arrow, QAbstractSpinBox::down-arrow {{ width: 8px; height: 8px; }}

QPushButton {{ min-height: 40px; border-radius: 10px; padding: 0 17px; font-size: 14px; font-weight: 750; }}
QPushButton#primaryButton, QPushButton#saveButton {{
    background: {AURORA_GREEN}; color: #061006; border: 1px solid {AURORA_GREEN_GLOW};
}}
QPushButton#primaryButton:hover, QPushButton#saveButton:hover {{
    background: {AURORA_GREEN_HOVER}; border: 1px solid #B1FFA3;
}}
QPushButton#primaryButton:pressed, QPushButton#saveButton:pressed {{ background: #29D80E; padding-top: 1px; }}
QPushButton#secondaryButton {{ background: rgba(24, 37, 62, 232); color: #EAF1FA; border: 1px solid rgba(114, 153, 220, 112); }}
QPushButton#secondaryButton:hover {{ background: rgba(35, 54, 88, 242); border: 1px solid {AURORA_GREEN}; color: {AURORA_GREEN_HOVER}; }}
QPushButton#secondaryButton:pressed {{ background: rgba(17, 30, 51, 245); }}
QPushButton#dangerButton {{ background: #A7233C; color: #FFFFFF; border: 1px solid #FF667C; }}
QPushButton#dangerButton:hover {{ background: #C02B47; border: 1px solid #FF9AAA; }}
QPushButton#dangerButton:pressed {{ background: #82172D; }}
QPushButton#tinyButton, QPushButton#sidebarHelpButton {{
    min-height: 28px; max-height: 30px; padding: 0 10px; font-size: 12px;
    background: rgba(19, 31, 53, 218); color: #B9C8DB;
    border: 1px solid rgba(101, 137, 198, 80); border-radius: 8px;
}}
QPushButton#tinyButton:hover, QPushButton#sidebarHelpButton:hover {{ color: {AURORA_GREEN}; border: 1px solid {AURORA_GREEN}; }}
QPushButton#accountDeleteButton {{ min-width: 44px; max-width: 44px; padding: 0; background: rgba(112, 24, 43, 180); border: 1px solid #D94A65; color: #FFFFFF; }}
QPushButton#accountDeleteButton:hover {{ background: #B52844; border: 1px solid #FF8CA0; }}
QPushButton:disabled {{ background: rgba(35, 43, 57, 220); border: 1px solid #46505F; color: #778294; }}
QPushButton[busy="true"] {{ background: rgba(57, 255, 20, 35); border: 1px solid {AURORA_GREEN}; color: {AURORA_GREEN}; }}

QCheckBox {{ spacing: 10px; font-weight: 650; color: #E4ECF7; }}
QCheckBox::indicator {{ width: 42px; height: 22px; border-radius: 11px; background: #9C3542; border: 1px solid #D65B70; }}
QCheckBox::indicator:checked {{ background: {AURORA_GREEN}; border: 1px solid {AURORA_GREEN_GLOW}; }}
QCheckBox::indicator:checked:hover {{ background: {AURORA_GREEN_HOVER}; }}
QCheckBox::indicator:disabled {{ background: #454C58; border: 1px solid #68717F; }}

QProgressBar {{ min-height: 18px; max-height: 18px; border: 1px solid rgba(102, 140, 205, 100); border-radius: 9px; background: rgba(5, 12, 24, 230); color: transparent; }}
QProgressBar::chunk {{ background: {AURORA_GREEN}; border-radius: 8px; }}
QTableWidget, QTableView {{
    background: rgba(4, 11, 24, 220); alternate-background-color: rgba(8, 18, 35, 225);
    border: 1px solid rgba(101, 139, 207, 86); border-radius: 12px;
    gridline-color: rgba(70, 99, 151, 68); selection-background-color: rgba(57, 255, 20, 38); selection-color: #FFFFFF;
}}
QTableWidget::item, QTableView::item {{ padding: 8px; border-bottom: 1px solid rgba(64, 91, 140, 60); }}
QHeaderView::section {{ background: rgba(14, 29, 52, 245); color: #C3D2E7; border: none; border-bottom: 1px solid rgba(83, 120, 187, 90); padding: 10px 8px; font-weight: 750; }}

QLabel#activityTitle {{ color: #FFFFFF; font-size: 13px; font-weight: 900; letter-spacing: 1px; }}
QLabel#activityBadge {{ color: {AURORA_GREEN}; background: rgba(57, 255, 20, 20); border: 1px solid rgba(57, 255, 20, 95); border-radius: 9px; padding: 4px 9px; font-size: 12px; font-weight: 700; }}
QLabel#activityNext {{ color: #B1C9E8; font-size: 13px; font-weight: 650; }}
QPlainTextEdit#activityLog {{ background: rgba(2, 8, 17, 235); border: 1px solid rgba(74, 113, 180, 86); border-radius: 9px; padding: 8px 10px; color: #D1E0F1; font-family: "Cascadia Mono", "Consolas", "Segoe UI", monospace; font-size: 12px; }}

QDialog, QDialog#helpDialog, QDialog#factoryResetDialog, QDialog#dangerConfirmDialog,
QDialog#accountSourceDialog, QDialog#instructionImageDialog {{
    background: #0A1222; color: {TEXT_PRIMARY};
}}
QMessageBox {{ background: #0A1222; }}
QMessageBox QLabel {{ color: {TEXT_PRIMARY}; min-width: 280px; }}
QMessageBox QPushButton {{ min-width: 110px; }}
QToolTip {{ background: #101A2C; border: 1px solid #45658E; border-radius: 7px; padding: 6px 8px; color: #F4F8FF; }}
QMenu {{ background: #0C1528; border: 1px solid #34527C; border-radius: 9px; padding: 5px; color: {TEXT_PRIMARY}; }}
QMenu::item {{ padding: 7px 16px; border-radius: 7px; }}
QMenu::item:selected {{ background: rgba(57, 255, 20, 34); color: {AURORA_GREEN}; }}
QSplitter#mainSplitter::handle, QSplitter#contentSplitter::handle {{ background: rgba(60, 91, 143, 80); }}
QScrollBar:vertical {{ width: 11px; background: transparent; margin: 2px; }}
QScrollBar::handle:vertical {{ min-height: 30px; border-radius: 5px; background: rgba(100, 132, 184, 110); }}
QScrollBar::handle:vertical:hover {{ background: rgba(57, 255, 20, 130); }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QLabel#openAiTitle, QComboBox#commentSourceCombo[openAiSelected="true"], QLabel#saveStatusSaved {{ color: {AURORA_GREEN}; font-weight: 850; }}
QLabel#saveStatusDirty {{ color: #FFE176; font-weight: 700; }}
"""


AURORA_PRESTIGE_QSS = _build_qss()
TELEGRAM_PREMIUM_QSS = AURORA_PRESTIGE_QSS

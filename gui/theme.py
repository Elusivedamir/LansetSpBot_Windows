TELEGRAM_PREMIUM_QSS = r"""
QMainWindow, QWidget#rootWindow {
    background: #090A0C;
    color: #F7FAFD;
    font-family: "Inter", "Segoe UI", "SF Pro Display", Arial;
    font-size: 14px;
}
QWidget {
    color: #F7FAFD;
    font-family: "Inter", "Segoe UI", "SF Pro Display", Arial;
}
QFrame#sidebar {
    background: #15171B;
    border-right: 1px solid #292C33;
}
QLabel#brandMark {
    min-width: 72px;
    max-width: 72px;
    min-height: 44px;
    max-height: 44px;
    border-radius: 14px;
    background: #15171B;
    color: #FFFFFF;
    font-size: 22px;
    font-weight: 900;
    qproperty-alignment: AlignCenter;
}
QLabel#brandTitle {
    color: #FFFFFF;
    font-size: 20px;
    font-weight: 900;
    letter-spacing: 0.6px;
}
QLabel#brandSubtitle, QLabel#pageSubtitle, QLabel#mutedText {
    color: #9A9FA9;
    font-size: 13px;
}
QLabel#pageTitle {
    color: #FFFFFF;
    font-size: 31px;
    font-weight: 850;
}
QLabel#cardTitle, QLabel#statusTitle {
    color: #FFFFFF;
    font-size: 16px;
    font-weight: 750;
}
QLabel#statusDotOffline {
    color: #777D88;
    font-size: 25px;
}
QLabel#statusDotOnline {
    color: #22C55E;
    font-size: 25px;
}
QFrame#accountManagerCard {
    background: #15171B;
    border: 1px solid #30343C;
    border-radius: 17px;
}
QLabel#accountStateOnline {
    color: #22C55E;
    font-size: 24px;
    font-weight: 900;
}
QLabel#accountStatePaused {
    color: #FACC15;
    font-size: 24px;
}
QLabel#accountStateWarning {
    color: #FB923C;
    font-size: 24px;
}
QLabel#accountStateError {
    color: #EF4444;
    font-size: 24px;
}
QLabel#accountStateStopped, QLabel#accountStateDisconnected {
    color: #9CA3AF;
    font-size: 24px;
}
QLabel#accountStateText {
    color: #F7FAFD;
    font-size: 15px;
    font-weight: 750;
}
QListWidget#navigation {
    background: transparent;
    border: none;
    outline: none;
    padding: 4px 4px;
    font-size: 16px;
    font-weight: 700;
}
QListWidget#navigation::item {
    color: #39FF14;
    padding: 15px 17px;
    margin: 4px 0;
    border-radius: 13px;
}
QListWidget#navigation::item:hover {
    background: rgba(48, 78, 103, 0.48);
    color: #39FF14;
}
QListWidget#navigation::item:selected {
    background: #15171B;
    color: #39FF14;
}
QPushButton#sidebarHelpButton {
    min-height: 25px;
    max-height: 27px;
    min-width: 0;
    border: none;
    border-radius: 7px;
    padding: 0 8px;
    background: transparent;
    color: #8298AA;
    font-size: 11px;
    font-weight: 600;
    text-align: left;
}
QPushButton#sidebarHelpButton:hover {
    background: rgba(48, 78, 103, 0.42);
    color: #FFFFFF;
}
QPushButton#sidebarHelpButton:pressed {
    background: rgba(42, 171, 238, 0.24);
}
QDialog#helpDialog {
    background: #14161A;
}
QStackedWidget#contentStack {
    background: #090A0C;
}
QScrollArea#pageScroll, QScrollArea#pageScroll > QWidget > QWidget {
    background: #090A0C;
    border: none;
}
QFrame#card, QFrame#statusCard, QFrame#infoCard {
    background: #15171B;
    border: 1px solid #30343C;
    border-radius: 17px;
}
QFrame#statusCard {
    border: 1px solid #3A3F49;
}
QFrame#authChallengeCard {
    background: #101216;
    border: 1px solid #3C4657;
    border-radius: 13px;
    margin-top: 4px;
}
QFrame#commentVariantRow {
    background: #101216;
    border: 1px solid #303640;
    border-radius: 12px;
}
QFrame#infoCard {
    background: #15171B;
    border: 1px solid #46546B;
}
QFrame#dangerCard {
    background: #15171B;
    border: 1px solid #78414A;
    border-radius: 17px;
}
QLabel#dangerTitle {
    color: #D98D96;
    font-size: 16px;
    font-weight: 850;
}
QLabel#dangerText {
    color: #CBA7AB;
    font-size: 13px;
}
QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox {
    background: #0F1114;
    border: 1px solid #343942;
    border-radius: 11px;
    padding: 10px 12px;
    color: #FFFFFF;
    selection-background-color: #6E84AD;
    font-size: 14px;
}
QLineEdit:hover, QPlainTextEdit:hover, QComboBox:hover, QAbstractSpinBox:hover {
    border: 1px solid #4A515D;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {
    border: 1px solid #7F93B8;
    background: #101216;
}
QComboBox::drop-down {
    border: none;
    width: 28px;
}
QComboBox#commentSourceCombo[openAiSelected="true"] {
    color: #39FF14;
    font-weight: 850;
}
QLabel#openAiTitle {
    color: #39FF14;
    font-size: 16px;
    font-weight: 850;
}
/* Popups are separate top-level windows. Without their own rules they fall
   back to the system palette, which on this dark theme rendered near-white
   text on a near-white background: the combo box list and the tray menu were
   effectively unreadable. */
QComboBox QAbstractItemView {
    background: #E6EBF1;
    border: 1px solid #AAB5C2;
    border-radius: 10px;
    padding: 4px;
    color: #17202A;
    outline: none;
    selection-background-color: #C3CFDC;
    selection-color: #101820;
}
QComboBox QAbstractItemView::item {
    min-height: 28px;
    padding: 4px 10px;
    color: #17202A;
}
QComboBox QAbstractItemView::item:selected,
QComboBox QAbstractItemView::item:hover {
    background: #C3CFDC;
    color: #101820;
}
QComboBox QAbstractItemView::item:disabled {
    color: #7A8490;
}
QMenu {
    background: #15171B;
    border: 1px solid #343942;
    border-radius: 10px;
    padding: 6px;
    color: #F7FAFD;
}
QMenu::item {
    background: transparent;
    padding: 8px 18px;
    border-radius: 8px;
    color: #F7FAFD;
}
QMenu::item:selected {
    background: #2A3550;
    color: #FFFFFF;
}
QMenu::item:disabled {
    color: #6E7480;
}
QMenu::separator {
    height: 1px;
    background: #292C33;
    margin: 6px 8px;
}
QToolTip {
    background: #15171B;
    border: 1px solid #343942;
    border-radius: 8px;
    padding: 6px 8px;
    color: #F7FAFD;
}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    background: transparent;
    border: none;
    width: 18px;
}
QAbstractSpinBox::up-arrow, QAbstractSpinBox::down-arrow {
    width: 9px;
    height: 9px;
}
QCheckBox {
    spacing: 10px;
    font-weight: 650;
    color: #E0E2E6;
}
QCheckBox::indicator {
    width: 42px;
    height: 22px;
    border-radius: 11px;
    background: #9C3542;
    border: 1px solid #E96674;
}
QCheckBox::indicator:unchecked:hover {
    background: #B23E4C;
    border: 1px solid #FF7B87;
}
QCheckBox::indicator:checked {
    background: #238B57;
    border: 1px solid #65D69A;
}
QCheckBox::indicator:checked:hover {
    background: #2AA868;
    border: 1px solid #78E8AF;
}
QCheckBox::indicator:disabled {
    background: #4A4E55;
    border: 1px solid #676C75;
}
QPushButton {
    min-height: 42px;
    border-radius: 11px;
    padding: 0 20px;
    font-size: 15px;
    font-weight: 750;
}
QPushButton#primaryButton {
    background: #15171B;
    color: #FFFFFF;
    border: 1px solid #8698B9;
}
QPushButton#primaryButton:hover {
    background: #15171B;
}
QPushButton#primaryButton:pressed {
    background: #596F97;
}
QPushButton#secondaryButton {
    background: #202329;
    color: #ECEEF1;
    border: 1px solid #444B57;
}
QPushButton#secondaryButton:hover {
    background: #292D34;
    border: 1px solid #596270;
}
QPushButton#dangerButton {
    background: #8E343F;
    color: #FFFFFF;
    border: 1px solid #B95C67;
}
QPushButton#dangerButton:hover {
    background: #9C3B47;
    border: 1px solid #FF7380;
}
QPushButton#dangerButton:pressed {
    background: #7F1825;
}
QPushButton#saveButton {
    background: #123247;
    color: #DDF6FF;
    border: 1px solid #6E84AD;
    min-width: 210px;
}
QPushButton#saveButton:hover {
    background: #17425C;
    border: 1px solid #58C8FF;
}
QPushButton#saveButton:pressed {
    background: #0F2B3D;
}
QLabel#saveStatusSaved {
    color: #61DDAA;
    font-size: 13px;
    font-weight: 700;
}
QLabel#saveStatusDirty {
    color: #F2C66D;
    font-size: 13px;
    font-weight: 700;
}
QPushButton#tinyButton {
    min-height: 28px;
    max-height: 28px;
    padding: 0 12px;
    font-size: 12px;
    background: #182A38;
    color: #AFC5D5;
    border: 1px solid #30495C;
    border-radius: 9px;
}
QPushButton#tinyButton:hover {
    color: #FFFFFF;
    border: 1px solid #4C718B;
}
QPushButton:disabled {
    background: #223340;
    border: 1px solid #2B3E4D;
    color: #667D8E;
}
QStackedWidget#instructionStack {
    background: transparent;
}
QScrollArea#instructionStepScroll, QScrollArea#instructionStepScroll > QWidget > QWidget {
    background: transparent;
    border: none;
}
QLabel#instructionImage {
    background: #0F1114;
    border: 1px solid #23384A;
    border-radius: 14px;
    padding: 10px;
}
QLabel#instructionImage:hover, QLabel#instructionImage:focus {
    background: #121923;
    border: 1px solid #6E84AD;
}
QLabel#instructionImageHint {
    color: #8ECFF0;
    font-size: 12px;
    font-weight: 650;
}
QDialog#instructionImageDialog {
    background: #090A0C;
}
QScrollArea#instructionImageScroll,
QScrollArea#instructionImageScroll > QWidget > QWidget {
    background: #0B0D10;
    border: 1px solid #30343C;
}
QLabel#instructionImagePreview {
    background: #0B0D10;
}
QProgressBar {
    min-height: 18px;
    max-height: 18px;
    border: 1px solid #213545;
    border-radius: 9px;
    background: #172633;
    color: transparent;
}
QProgressBar::chunk {
    background: #15171B;
    border-radius: 8px;
}
QTableWidget, QTableView {
    background: #0B1621;
    alternate-background-color: #0E1C28;
    border: 1px solid #223748;
    border-radius: 13px;
    gridline-color: #213342;
    selection-background-color: #1C4F6E;
    selection-color: #FFFFFF;
}
QTableWidget::item, QTableView::item {
    padding: 9px;
    border-bottom: 1px solid #1C2E3B;
}
QHeaderView::section {
    background: #132330;
    color: #93AABD;
    border: none;
    border-bottom: 1px solid #2D4659;
    padding: 11px 9px;
    font-weight: 750;
}
QFrame#activityPanel {
    background: #15171B;
    border-top: 1px solid #294458;
    border-bottom: 1px solid #152634;
}
QLabel#activityTitle {
    color: #FFFFFF;
    font-size: 13px;
    font-weight: 900;
    letter-spacing: 1px;
}
QLabel#activityBadge {
    color: #B9ECFF;
    background: #12364B;
    border: 1px solid #236488;
    border-radius: 9px;
    padding: 4px 9px;
    font-size: 12px;
    font-weight: 700;
}
QLabel#activityNext {
    color: #8ECFF0;
    font-size: 13px;
    font-weight: 650;
}
QPlainTextEdit#activityLog {
    background: #09141E;
    border: 1px solid #1D3343;
    border-radius: 10px;
    padding: 9px 11px;
    color: #C9DCE8;
    font-family: "JetBrains Mono", "Cascadia Mono", "SFMono-Regular", monospace;
    font-size: 12px;
}
QSplitter#mainSplitter::handle {
    background: #203444;
}
QSplitter#contentSplitter::handle {
    background: #0A1520;
    border-top: 1px solid #21394A;
    border-bottom: 1px solid #101D28;
}
QSplitter#contentSplitter::handle:hover {
    background: #15364C;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #365165;
    min-height: 30px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover {
    background: #4B6D84;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QMessageBox {
    background: #111F2C;
}
QToolTip {
    background: #142432;
    color: #FFFFFF;
    border: 1px solid #34556A;
    padding: 6px;
}
"""


# AURORA-PRESTIGE-V1
AURORA_PRESTIGE_QSS = r"""
QMainWindow, QWidget#rootWindow {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #070914, stop:0.48 #0A0D1A, stop:1 #10112A);
    color: #F7F8FF;
    font-family: "Segoe UI Variable Display", "Segoe UI Variable Text", "Segoe UI", "Inter", Arial;
    font-size: 14px;
}
QWidget {
    color: #F7F8FF;
    font-family: "Segoe UI Variable Text", "Segoe UI", "Inter", Arial;
}
QFrame#sidebar {
    background: qlineargradient(x1:0, y1:0, x2:0.95, y2:1,
        stop:0 #11132A, stop:0.55 #0B1023, stop:1 #12102A);
    border-right: 1px solid #343068;
}
QLabel#brandMark {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #1A2149, stop:0.5 #432F86, stop:1 #164D73);
    color: #FFFFFF;
    border: 1px solid #6B63D8;
    border-radius: 14px;
    font-size: 21px;
    font-weight: 800;
}
QLabel#brandTitle {
    color: #FFFFFF;
    font-size: 18px;
    font-weight: 800;
    letter-spacing: 0.2px;
}
QLabel#brandSubtitle {
    color: #A7AEC8;
    font-size: 12px;
}
QLabel#pageSubtitle, QLabel#mutedText {
    color: #A7AEC8;
}
QLabel#pageTitle {
    color: #FFFFFF;
    font-size: 32px;
    font-weight: 800;
}
QLabel#cardTitle, QLabel#statusTitle {
    color: #FFFFFF;
    font-weight: 750;
}
QListWidget#navigation {
    background: transparent;
    border: none;
    outline: none;
    padding: 4px;
    font-size: 15px;
    font-weight: 700;
}
QListWidget#navigation::item {
    color: #C8CDE2;
    padding: 15px 17px;
    margin: 4px 0;
    border-radius: 13px;
}
QListWidget#navigation::item:hover {
    color: #FFFFFF;
    background: rgba(77, 75, 164, 0.35);
}
QListWidget#navigation::item:selected {
    color: #FFFFFF;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #5546C8, stop:0.55 #404AB9, stop:1 #176D99);
    border: 1px solid #8078F0;
}
QStackedWidget#contentStack,
QScrollArea#pageScroll,
QScrollArea#pageScroll > QWidget > QWidget {
    background: transparent;
    border: none;
}
QFrame#card, QFrame#statusCard, QFrame#infoCard,
QFrame#accountManagerCard, QFrame#dangerCard {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(18, 22, 48, 238), stop:0.55 rgba(17, 20, 43, 242), stop:1 rgba(26, 19, 55, 238));
    border: 1px solid #363A70;
    border-radius: 18px;
}
QFrame#statusCard {
    border: 1px solid #5557A6;
}
QFrame#infoCard {
    border: 1px solid #3A6592;
}
QFrame#dangerCard {
    border: 1px solid #713A5E;
}
QFrame#authChallengeCard, QFrame#commentVariantRow, QFrame#proxyCard {
    background: #0E1228;
    border: 1px solid #363B70;
    border-radius: 13px;
}
QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox, QTimeEdit {
    background: rgba(8, 11, 27, 225);
    border: 1px solid #3C416F;
    border-radius: 11px;
    padding: 10px 12px;
    color: #F8F9FF;
    selection-background-color: #6358D8;
    font-size: 14px;
}
QLineEdit:hover, QPlainTextEdit:hover, QComboBox:hover,
QAbstractSpinBox:hover, QTimeEdit:hover {
    border: 1px solid #6465B8;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
QAbstractSpinBox:focus, QTimeEdit:focus {
    background: #0D122B;
    border: 1px solid #56C9FF;
}
QComboBox QAbstractItemView {
    background: #E6EBF1;
    color: #17202A;
    border: 1px solid #AAB5C2;
    border-radius: 10px;
    selection-background-color: #C3CFDC;
    selection-color: #101820;
    outline: none;
}
QComboBox QAbstractItemView::item {
    min-height: 30px;
    padding: 5px 10px;
    color: #17202A;
}
QComboBox QAbstractItemView::item:selected,
QComboBox QAbstractItemView::item:hover {
    background: #C3CFDC;
    color: #101820;
}
QPushButton {
    min-height: 42px;
    border-radius: 11px;
    padding: 0 20px;
    font-size: 14px;
    font-weight: 700;
}
QPushButton#primaryButton, QPushButton#saveButton {
    color: #FFFFFF;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #3B5ED7, stop:0.5 #5A43C9, stop:1 #1E8EBD);
    border: 1px solid #8078F0;
}
QPushButton#primaryButton:hover, QPushButton#saveButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4773F4, stop:0.5 #7357E8, stop:1 #28A9D9);
    border: 1px solid #A7A3FF;
}
QPushButton#secondaryButton {
    color: #EBEDFF;
    background: #171B38;
    border: 1px solid #4B4F86;
}
QPushButton#secondaryButton:hover {
    color: #FFFFFF;
    background: #202650;
    border: 1px solid #7273C5;
}
QPushButton#dangerButton {
    color: #FFFFFF;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #8C2F58, stop:1 #B63C69);
    border: 1px solid #E06A91;
}
QPushButton#dangerButton:hover {
    background: #C24472;
    border: 1px solid #FF8BAD;
}
QPushButton#accountDeleteButton {
    min-width: 48px;
    max-width: 48px;
    min-height: 44px;
    max-height: 44px;
    padding: 0;
    background: #401B31;
    border: 1px solid #C6537C;
    border-radius: 11px;
}
QPushButton#accountDeleteButton:hover {
    background: #7B294B;
    border: 1px solid #FF7EA7;
}
QPushButton#spamBotButton {
    min-height: 30px;
    max-height: 32px;
    padding: 0 14px;
    color: #EAF8FF;
    background: #17264B;
    border: 1px solid #4C8EDB;
    border-radius: 9px;
    font-size: 12px;
}
QPushButton#spamBotButton:hover {
    background: #213A70;
    border: 1px solid #6DCBFF;
}
QPushButton#proxyToggle {
    color: #DDF8FF;
    font-weight: 750;
}
QCheckBox {
    spacing: 10px;
    color: #E5E7F8;
    font-weight: 650;
}
QCheckBox::indicator {
    width: 42px;
    height: 22px;
    border-radius: 11px;
    background: #572340;
    border: 1px solid #C0557C;
}
QCheckBox::indicator:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #3B7FD8, stop:1 #5C4DD0);
    border: 1px solid #8EDBFF;
}
QTableWidget, QTableView {
    background: #0B1024;
    alternate-background-color: #10152D;
    border: 1px solid #343969;
    border-radius: 13px;
    gridline-color: #252A50;
    selection-background-color: #3B3D8B;
    selection-color: #FFFFFF;
}
QHeaderView::section {
    background: #171C3A;
    color: #ADB7DE;
    border: none;
    border-bottom: 1px solid #41467F;
    padding: 11px 9px;
    font-weight: 700;
}
QProgressBar {
    background: #11162E;
    border: 1px solid #303665;
    border-radius: 9px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #5547CF, stop:0.5 #376FCA, stop:1 #28C5D5);
    border-radius: 8px;
}
QFrame#activityPanel {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #0D1228, stop:0.6 #10142F, stop:1 #15102D);
    border-top: 1px solid #3B477F;
    border-bottom: 1px solid #24294C;
}
QLabel#activityBadge {
    color: #DFF9FF;
    background: #172E4F;
    border: 1px solid #3E78A8;
}
QLabel#activityNext {
    color: #8EDBFF;
}
QPlainTextEdit#activityLog {
    background: #080D1E;
    border: 1px solid #2E3A67;
    color: #C9D6F2;
    font-family: "Cascadia Mono", "JetBrains Mono", "Consolas", monospace;
}
QLabel#statusDotOnline, QLabel#accountStateOnline,
QLabel#saveStatusSaved, QLabel#openAiTitle {
    color: #43E6BE;
}
QLabel#statusDotOffline, QLabel#accountStateDisconnected,
QLabel#accountStateStopped {
    color: #7F88A8;
}
QMenu, QDialog#helpDialog, QDialog#instructionImageDialog, QMessageBox {
    background: #10142B;
    color: #F7F8FF;
}
QMenu {
    border: 1px solid #454A83;
}
QMenu::item:selected {
    background: #3D3B91;
}
QLabel#instructionImage {
    background: #0B1022;
    border: 1px solid #384475;
    border-radius: 14px;
}
QLabel#instructionImage:hover, QLabel#instructionImage:focus {
    border: 1px solid #73D5FF;
    background: #101735;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #484B83;
    min-height: 30px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover {
    background: #6869B0;
}
QSplitter#mainSplitter::handle, QSplitter#contentSplitter::handle {
    background: #252A52;
}
QToolTip {
    background: #171B36;
    color: #FFFFFF;
    border: 1px solid #555B9B;
    border-radius: 8px;
    padding: 6px 8px;
}
QPushButton:disabled {
    background: #202541;
    border: 1px solid #303654;
    color: #6F7795;
}
"""

BASE_PREMIUM_QSS = TELEGRAM_PREMIUM_QSS


# The selector is deliberately more specific than the general QComboBox popup
# rules. Ordinary application lists remain high-contrast light popups, while
# the theme picker visually belongs to the selected premium skin.
DARK_THEME_SELECTOR_QSS = r"""
QComboBox#themeSelector {
    min-width: 196px;
    min-height: 38px;
    max-height: 38px;
    padding: 0 14px;
    border-radius: 19px;
    background: rgba(26, 14, 39, 0.94);
    border: 1px solid #7E397D;
    color: #FFF7FF;
    font-size: 13px;
    font-weight: 750;
}
QComboBox#themeSelector:hover, QComboBox#themeSelector:focus {
    border: 1px solid #F05ACD;
    background: rgba(47, 18, 55, 0.98);
}
QComboBox#themeSelector::drop-down {
    border: none;
    width: 30px;
}
QComboBox#themeSelector QAbstractItemView {
    background: #160C1D;
    color: #F8EBF8;
    border: 1px solid #633064;
    border-radius: 10px;
    padding: 5px;
    outline: none;
    selection-background-color: #7D246B;
    selection-color: #FFFFFF;
}
QComboBox#themeSelector QAbstractItemView::item {
    min-height: 31px;
    padding: 4px 10px;
}
"""

LIGHT_THEME_SELECTOR_QSS = r"""
QComboBox#themeSelector {
    min-width: 196px;
    min-height: 38px;
    max-height: 38px;
    padding: 0 14px;
    border-radius: 19px;
    background: #FFFFFF;
    border: 1px solid #B8B5FF;
    color: #5146D8;
    font-size: 13px;
    font-weight: 750;
}
QComboBox#themeSelector:hover, QComboBox#themeSelector:focus {
    border: 1px solid #7167F0;
    background: #F8F7FF;
}
QComboBox#themeSelector::drop-down {
    border: none;
    width: 30px;
}
QComboBox#themeSelector QAbstractItemView {
    background: #FFFFFF;
    color: #20243A;
    border: 1px solid #C8CBE3;
    border-radius: 10px;
    padding: 5px;
    outline: none;
    selection-background-color: #E4E2FF;
    selection-color: #342CA8;
}
QComboBox#themeSelector QAbstractItemView::item {
    min-height: 31px;
    padding: 4px 10px;
}
"""


# Main skin requested by the operator. It intentionally overrides every
# prominent Aurora/legacy accent, so no green navigation colour leaks through.
VELVET_NIGHT_QSS = r"""
QMainWindow, QWidget#rootWindow {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #090611, stop:0.48 #100817, stop:1 #170A1D);
    color: #F8F0FA;
    font-family: "Segoe UI Variable Text", "Segoe UI", "Inter", Arial;
}
QWidget {
    color: #F8F0FA;
    font-family: "Segoe UI Variable Text", "Segoe UI", "Inter", Arial;
}
QFrame#sidebar {
    background: qlineargradient(x1:0, y1:0, x2:0.92, y2:1,
        stop:0 #13091B, stop:0.58 #110817, stop:1 #1C0A23);
    border-right: 1px solid #442248;
}
QLabel#brandMark {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #4B185A, stop:0.52 #8B266F, stop:1 #C63C94);
    color: #FFFFFF;
    border: 1px solid #DE5BC2;
}
QLabel#brandTitle { color: #FFFFFF; }
QLabel#brandSubtitle, QLabel#pageSubtitle, QLabel#mutedText { color: #B9A9BE; }
QLabel#pageTitle { color: #FFFFFF; }
QLabel#cardTitle, QLabel#statusTitle { color: #FFF9FF; }
QListWidget#navigation::item {
    color: #D8CBDD;
    padding: 15px 17px;
    margin: 4px 0;
    border-radius: 13px;
}
QListWidget#navigation::item:hover {
    color: #FFFFFF;
    background: rgba(124, 35, 103, 0.28);
}
QListWidget#navigation::item:selected {
    color: #FFFFFF;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4B185D, stop:0.52 #7A236F, stop:1 #9E285F);
    border-left: 3px solid #F150C8;
}
QStackedWidget#contentStack,
QScrollArea#pageScroll, QScrollArea#pageScroll > QWidget > QWidget {
    background: transparent;
}
QFrame#card, QFrame#statusCard, QFrame#infoCard,
QFrame#accountManagerCard {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(31, 16, 39, 0.96), stop:1 rgba(43, 18, 47, 0.92));
    border: 1px solid #502A56;
    border-radius: 17px;
}
QFrame#statusCard { border: 1px solid #633166; }
QFrame#authChallengeCard, QFrame#commentVariantRow, QFrame#proxyCard {
    background: #160D1C;
    border: 1px solid #4A2850;
    border-radius: 12px;
}
QFrame#dangerCard {
    background: #21101F;
    border: 1px solid #73314E;
}
QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox {
    background: #130C19;
    border: 1px solid #49304F;
    color: #FFF8FF;
    selection-background-color: #A72F82;
}
QLineEdit:hover, QPlainTextEdit:hover, QComboBox:hover, QAbstractSpinBox:hover {
    border: 1px solid #765078;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {
    border: 1px solid #E050BC;
    background: #180D1E;
}
QPushButton#primaryButton, QPushButton#saveButton {
    color: #FFFFFF;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #54156D, stop:0.5 #86216F, stop:1 #B21E63);
    border: 1px solid #C13FA4;
}
QPushButton#primaryButton:hover, QPushButton#saveButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #70208D, stop:0.5 #A52D89, stop:1 #D12B7A);
    border: 1px solid #F06DD1;
}
QPushButton#primaryButton:pressed, QPushButton#saveButton:pressed {
    background: #681C68;
}
QPushButton#secondaryButton {
    background: #29182F;
    color: #F6EAF8;
    border: 1px solid #57345D;
}
QPushButton#secondaryButton:hover {
    background: #38203E;
    border: 1px solid #8D4C8B;
}
QPushButton#dangerButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #741A46, stop:1 #A81755);
    color: #FFFFFF;
    border: 1px solid #C34872;
}
QPushButton#dangerButton:hover {
    background: #BE205F;
    border: 1px solid #F16A98;
}
QPushButton#accountDeleteButton {
    min-width: 42px;
    max-width: 42px;
    min-height: 40px;
    max-height: 40px;
    padding: 0;
    background: rgba(53, 24, 52, 0.9);
    border: 1px solid #693769;
    border-radius: 11px;
}
QPushButton#accountDeleteButton:hover {
    background: #6E2148;
    border: 1px solid #E35E99;
}
QPushButton#spamBotButton, QPushButton#tinyButton {
    background: #321631;
    color: #F2C7EA;
    border: 1px solid #6B3567;
}
QPushButton#spamBotButton:hover, QPushButton#tinyButton:hover {
    color: #FFFFFF;
    border: 1px solid #E05BC0;
}
QCheckBox::indicator:checked {
    background: #C32F9A;
    border: 1px solid #F27AD4;
}
QProgressBar { background: #211126; border: 1px solid #4D2A52; }
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #8E2A8D, stop:1 #E33C9E);
}
QTableWidget, QTableView {
    background: #100B16;
    alternate-background-color: #17101D;
    border: 1px solid #4B2B50;
    gridline-color: #352039;
    selection-background-color: #63265A;
}
QHeaderView::section {
    background: #211226;
    color: #CBB9CF;
    border-bottom: 1px solid #513055;
}
QFrame#activityPanel {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #160C1D, stop:1 #211024);
    border-top: 1px solid #5A2B5A;
}
QLabel#activityBadge {
    color: #FFD8F6;
    background: #4D1745;
    border: 1px solid #933879;
}
QLabel#activityNext { color: #EE8FD6; }
QPlainTextEdit#activityLog {
    background: #100A15;
    border: 1px solid #422247;
    color: #DCCFE0;
}
QSplitter#mainSplitter::handle, QSplitter#contentSplitter::handle {
    background: #2B1730;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: #B42D8F;
}
"""


TELEGRAM_OBSIDIAN_QSS = r"""
QMainWindow, QWidget#rootWindow { background: #080E17; color: #EEF4FF; }
QWidget { color: #EEF4FF; }
QFrame#sidebar { background: #0C1521; border-right: 1px solid #243750; }
QLabel#brandMark { background: #172843; border: 1px solid #4F75B8; }
QLabel#brandSubtitle, QLabel#pageSubtitle, QLabel#mutedText { color: #91A0B7; }
QListWidget#navigation::item { color: #C6D1E2; }
QListWidget#navigation::item:hover { background: rgba(53, 84, 128, 0.36); color: #FFFFFF; }
QListWidget#navigation::item:selected {
    color: #FFFFFF;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #24436D, stop:1 #263454);
    border-left: 3px solid #6EA7FF;
}
QFrame#card, QFrame#statusCard, QFrame#infoCard, QFrame#accountManagerCard {
    background: #101A27; border: 1px solid #26374D; border-radius: 17px;
}
QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox {
    background: #0A131F; border: 1px solid #2A3B51; color: #F4F8FF;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {
    border: 1px solid #6799E8; background: #0D1825;
}
QPushButton#primaryButton, QPushButton#saveButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4169E1, stop:1 #5146B9); border: 1px solid #7189FF; color: #FFFFFF;
}
QPushButton#primaryButton:hover, QPushButton#saveButton:hover {
    background: #5577E8; border: 1px solid #9AB0FF;
}
QPushButton#secondaryButton { background: #1A2737; border: 1px solid #33465E; color: #E4EBF5; }
QPushButton#secondaryButton:hover { background: #24354A; border: 1px solid #55739A; }
QPushButton#accountDeleteButton { background: #182332; border: 1px solid #35465B; }
QPushButton#accountDeleteButton:hover { background: #542A3A; border: 1px solid #C55E7A; }
QFrame#activityPanel { background: #0D1723; border-top: 1px solid #2C4159; }
QPlainTextEdit#activityLog { background: #08121C; border: 1px solid #26394D; color: #C7D3E2; }
QLabel#activityBadge { background: #17284A; border: 1px solid #355B99; color: #CFE1FF; }
QLabel#activityNext { color: #7FAEFF; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: #567DE0; }
"""


MIDNIGHT_GLASS_QSS = r"""
QMainWindow, QWidget#rootWindow {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #060D1D, stop:0.55 #0A1730, stop:1 #111A3C);
    color: #F2F6FF;
}
QWidget { color: #F2F6FF; }
QFrame#sidebar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(15, 29, 58, 0.96), stop:1 rgba(12, 24, 48, 0.94));
    border-right: 1px solid #3A5795;
}
QLabel#brandMark { background: #1B3A79; border: 1px solid #7196FF; }
QLabel#brandSubtitle, QLabel#pageSubtitle, QLabel#mutedText { color: #A8B6D3; }
QListWidget#navigation::item { color: #C9D5EE; }
QListWidget#navigation::item:hover { background: rgba(65, 105, 205, 0.25); color: #FFFFFF; }
QListWidget#navigation::item:selected {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(41, 101, 214, 0.86), stop:1 rgba(66, 57, 161, 0.78));
    color: #FFFFFF; border: 1px solid #668CFF;
}
QFrame#card, QFrame#statusCard, QFrame#infoCard, QFrame#accountManagerCard {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(19, 42, 82, 0.86), stop:1 rgba(17, 30, 67, 0.82));
    border: 1px solid #405E9C; border-radius: 17px;
}
QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox {
    background: rgba(7, 20, 43, 0.82); border: 1px solid #3B5487; color: #F8FAFF;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {
    border: 1px solid #6C94FF; background: rgba(12, 28, 58, 0.94);
}
QPushButton#primaryButton, QPushButton#saveButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #2465D8, stop:1 #344EB1); border: 1px solid #6A8FFF; color: #FFFFFF;
}
QPushButton#primaryButton:hover, QPushButton#saveButton:hover { background: #3479E5; border: 1px solid #90ADFF; }
QPushButton#secondaryButton { background: rgba(40, 61, 106, 0.72); border: 1px solid #506DA4; color: #E7EEFF; }
QPushButton#secondaryButton:hover { background: rgba(56, 82, 137, 0.9); border: 1px solid #7899D7; }
QPushButton#accountDeleteButton { background: rgba(44, 54, 92, 0.8); border: 1px solid #596EA7; }
QPushButton#accountDeleteButton:hover { background: #602D50; border: 1px solid #D468A4; }
QFrame#activityPanel { background: rgba(10, 25, 54, 0.9); border-top: 1px solid #4362A1; }
QPlainTextEdit#activityLog { background: rgba(4, 15, 35, 0.88); border: 1px solid #34528A; color: #CFD9EF; }
QLabel#activityBadge { background: #172E5B; border: 1px solid #3D6EBA; color: #DCE9FF; }
QLabel#activityNext { color: #88B1FF; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: #527BE2; }
"""


CYBER_AZURE_QSS = r"""
QMainWindow, QWidget#rootWindow { background: #020A13; color: #E9FAFF; }
QWidget { color: #E9FAFF; }
QFrame#sidebar { background: #03101C; border-right: 1px solid #075078; }
QLabel#brandMark { background: #05263A; border: 1px solid #00BDF5; color: #EFFFFF; }
QLabel#brandSubtitle, QLabel#pageSubtitle, QLabel#mutedText { color: #8DA9B8; }
QListWidget#navigation::item { color: #BBD5E2; }
QListWidget#navigation::item:hover { background: rgba(0, 165, 225, 0.16); color: #FFFFFF; }
QListWidget#navigation::item:selected {
    color: #FFFFFF;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #053F61, stop:1 #06192B);
    border-left: 3px solid #00CFFF; border-top: 1px solid #0879AA; border-bottom: 1px solid #0879AA;
}
QFrame#card, QFrame#statusCard, QFrame#infoCard, QFrame#accountManagerCard {
    background: #05111E; border: 1px solid #08628E; border-radius: 13px;
}
QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox {
    background: #020C16; border: 1px solid #075078; color: #ECFBFF;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {
    border: 1px solid #00CFFF; background: #041320;
}
QPushButton#primaryButton, QPushButton#saveButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #0079B8, stop:1 #00AEE9); border: 1px solid #00D9FF; color: #FFFFFF;
}
QPushButton#primaryButton:hover, QPushButton#saveButton:hover { background: #00AEE9; border: 1px solid #8BEAFF; }
QPushButton#secondaryButton { background: #061B2B; border: 1px solid #0A638E; color: #D8F6FF; }
QPushButton#secondaryButton:hover { background: #092A40; border: 1px solid #00BCEB; }
QPushButton#accountDeleteButton { background: #061827; border: 1px solid #08749E; }
QPushButton#accountDeleteButton:hover { background: #4E1730; border: 1px solid #FF4E8B; }
QFrame#activityPanel { background: #03111E; border-top: 1px solid #08739E; }
QPlainTextEdit#activityLog { background: #010A12; border: 1px solid #07577D; color: #B9DCE8; }
QLabel#activityBadge { background: #04334A; border: 1px solid #008DBD; color: #BDF4FF; }
QLabel#activityNext { color: #2EDBFF; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: #00BFEA; }
"""


CRYSTAL_PREMIUM_QSS = r"""
QMainWindow, QWidget#rootWindow { background: #F5F7FC; color: #20243A; }
QWidget { color: #20243A; }
QFrame#sidebar { background: #FBFCFF; border-right: 1px solid #D9DEEA; }
QLabel#brandMark { background: #F0EEFF; color: #5E50E6; border: 1px solid #C8C2FF; }
QLabel#brandTitle { color: #1F2436; }
QLabel#brandSubtitle, QLabel#pageSubtitle, QLabel#mutedText { color: #6F788E; }
QLabel#pageTitle, QLabel#cardTitle, QLabel#statusTitle { color: #171C2D; }
QListWidget#navigation::item { color: #596277; }
QListWidget#navigation::item:hover { background: #F0F1FF; color: #4C43D0; }
QListWidget#navigation::item:selected {
    color: #5146D8; background: #EDEBFF; border-left: 3px solid #6C5CE7;
}
QStackedWidget#contentStack, QScrollArea#pageScroll,
QScrollArea#pageScroll > QWidget > QWidget { background: #F5F7FC; }
QFrame#card, QFrame#statusCard, QFrame#infoCard, QFrame#accountManagerCard {
    background: #FFFFFF; border: 1px solid #DDE2EC; border-radius: 17px;
}
QFrame#authChallengeCard, QFrame#commentVariantRow, QFrame#proxyCard {
    background: #F8F9FD; border: 1px solid #D9DEEA;
}
QFrame#dangerCard { background: #FFF9FA; border: 1px solid #E8C8CE; }
QLabel#dangerTitle { color: #A43D51; }
QLabel#dangerText { color: #825761; }
QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox {
    background: #FFFFFF; border: 1px solid #CFD5E2; color: #20243A;
    selection-background-color: #D9D5FF;
}
QLineEdit:hover, QPlainTextEdit:hover, QComboBox:hover, QAbstractSpinBox:hover { border: 1px solid #AEB6C8; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {
    border: 1px solid #7167F0; background: #FFFFFF;
}
QPushButton#primaryButton, QPushButton#saveButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #6457EE, stop:1 #795FEF); border: 1px solid #756AF0; color: #FFFFFF;
}
QPushButton#primaryButton:hover, QPushButton#saveButton:hover { background: #7568F2; border: 1px solid #A39BFF; }
QPushButton#secondaryButton { background: #FFFFFF; border: 1px solid #CBD1DF; color: #343A50; }
QPushButton#secondaryButton:hover { background: #F5F4FF; border: 1px solid #8E85F4; color: #5146D8; }
QPushButton#dangerButton { background: #FFF7F8; border: 1px solid #E7A8B4; color: #B43851; }
QPushButton#dangerButton:hover { background: #FFECEF; border: 1px solid #D96980; }
QPushButton#accountDeleteButton { background: #FFFFFF; border: 1px solid #D4D9E5; }
QPushButton#accountDeleteButton:hover { background: #FFF0F3; border: 1px solid #D96C83; }
QPushButton:disabled { background: #F0F2F6; border: 1px solid #DFE3EB; color: #9CA4B5; }
QCheckBox { color: #3A4055; }
QTableWidget, QTableView { background: #FFFFFF; alternate-background-color: #F8F9FC; border: 1px solid #D8DEEA; color: #22283A; }
QHeaderView::section { background: #F0F2F8; color: #596277; border-bottom: 1px solid #D5DAE5; }
QProgressBar { background: #ECEFF6; border: 1px solid #D5DAE7; }
QProgressBar::chunk { background: #6E61EB; }
QFrame#activityPanel { background: #FFFFFF; border-top: 1px solid #D7DCE8; }
QLabel#activityTitle { color: #20243A; }
QLabel#activityBadge { background: #F0EEFF; border: 1px solid #C3BCFF; color: #5B4DDC; }
QLabel#activityNext { color: #6B63D8; }
QPlainTextEdit#activityLog { background: #FBFCFF; border: 1px solid #D8DEEA; color: #3D455B; }
QSplitter#mainSplitter::handle, QSplitter#contentSplitter::handle { background: #E0E4EC; }
QScrollBar { background: #F2F4F8; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: #8C82EE; }
QMenu { background: #FFFFFF; border: 1px solid #D2D8E4; color: #20243A; }
QMenu::item { color: #20243A; }
QMenu::item:selected { background: #E9E7FF; color: #4439BA; }
QToolTip { background: #FFFFFF; border: 1px solid #C9CFDC; color: #20243A; }
"""


SEMANTIC_TOGGLE_STATE_QSS = r"""
/* Functional state always wins over a skin accent: red = off, green = on. */
QCheckBox::indicator:unchecked {
    background: #9C3542;
    border: 1px solid #E96674;
}
QCheckBox::indicator:unchecked:hover {
    background: #B23E4C;
    border: 1px solid #FF7B87;
}
QCheckBox::indicator:checked {
    background: #238B57;
    border: 1px solid #65D69A;
}
QCheckBox::indicator:checked:hover {
    background: #2AA868;
    border: 1px solid #78E8AF;
}
QCheckBox::indicator:disabled {
    background: #4A4E55;
    border: 1px solid #676C75;
}
"""


DEFAULT_THEME_KEY = "velvet-night"
THEME_OPTIONS = (
    ("velvet-night", "Velvet Night"),
    ("aurora-prestige", "Aurora Prestige 2.0"),
    ("telegram-obsidian", "Telegram Obsidian"),
    ("midnight-glass", "Midnight Glass"),
    ("cyber-azure", "Cyber Azure"),
    ("crystal-premium", "Crystal Premium"),
)

THEME_STYLESHEETS = {
    "velvet-night": BASE_PREMIUM_QSS + VELVET_NIGHT_QSS + DARK_THEME_SELECTOR_QSS,
    "aurora-prestige": BASE_PREMIUM_QSS + AURORA_PRESTIGE_QSS + DARK_THEME_SELECTOR_QSS,
    "telegram-obsidian": BASE_PREMIUM_QSS + TELEGRAM_OBSIDIAN_QSS + DARK_THEME_SELECTOR_QSS,
    "midnight-glass": BASE_PREMIUM_QSS + MIDNIGHT_GLASS_QSS + DARK_THEME_SELECTOR_QSS,
    "cyber-azure": BASE_PREMIUM_QSS + CYBER_AZURE_QSS + DARK_THEME_SELECTOR_QSS,
    "crystal-premium": BASE_PREMIUM_QSS + CRYSTAL_PREMIUM_QSS + LIGHT_THEME_SELECTOR_QSS,
}


def normalize_theme_key(value: object) -> str:
    key = str(value or "").strip().casefold()
    return key if key in THEME_STYLESHEETS else DEFAULT_THEME_KEY


def theme_stylesheet(value: object) -> str:
    return THEME_STYLESHEETS[normalize_theme_key(value)] + SEMANTIC_TOGGLE_STATE_QSS


# Backwards-compatible public name used by existing windows and tests.
TELEGRAM_PREMIUM_QSS = theme_stylesheet(DEFAULT_THEME_KEY)

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
    min-width: 52px;
    max-width: 52px;
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
    color: #65B58C;
    font-size: 25px;
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
    color: #B0B4BC;
    padding: 15px 17px;
    margin: 4px 0;
    border-radius: 13px;
}
QListWidget#navigation::item:hover {
    background: rgba(48, 78, 103, 0.48);
    color: #FFFFFF;
}
QListWidget#navigation::item:selected {
    background: #15171B;
    color: #FFFFFF;
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

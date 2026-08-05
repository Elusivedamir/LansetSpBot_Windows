from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.version import APP_NAME, BUILD_ID, __version__
from .activity_panel import ActivityPanel
from .resources import asset_path
from .theme import TELEGRAM_PREMIUM_QSS
from .views.account_view import AccountView
from .views.channels_view import ChannelsView
from .views.commenting_view import CommentingView
from .views.links_view import LinksView
from .views.audience_parser_view import AudienceParserView
from .views.instructions_view import InstructionsView
from .views.target_audience_view import TargetAudienceView


class MainWindow(QMainWindow):
    MENU_ICONS = (
        "account.svg",
        "channels.svg",
        "links.svg",
        "comments.svg",
        "audience.svg",
        "target.svg",
        "instructions.svg",
    )
    SIDEBAR_MAX_WIDTH = 360
    SUPPORT_CONTACT = "@lansetp"
    SUPPORT_URL = "https://t.me/lansetp"

    @staticmethod
    def _asset_icon(name: str) -> QIcon:
        return QIcon(str(asset_path("icons", name)))

    def __init__(self, adapter, queue_worker=None, config=None):
        super().__init__()
        self.adapter = adapter
        self.queue_worker = queue_worker
        self.config = config
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 860)
        # The old 1040px minimum made the window effectively fixed on smaller
        # laptops. All important content now remains usable from 760px upward.
        self.setMinimumSize(760, 620)
        # Ordinary window controls, stated explicitly: minimize puts the window
        # on the taskbar and leaves the application running, which is the only
        # way to get it off screen now that closing really closes.
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setStyleSheet(TELEGRAM_PREMIUM_QSS)
        self.setWindowIcon(QIcon(str(asset_path("lansetspbot.png"))))

        root = QWidget()
        root.setObjectName("rootWindow")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.horizontal_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.horizontal_splitter.setObjectName("mainSplitter")
        self.horizontal_splitter.setChildrenCollapsible(False)
        self.horizontal_splitter.setHandleWidth(1)

        self._sidebar_fitted = False
        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setMinimumWidth(150)
        self.sidebar.setMaximumWidth(310)
        self.sidebar_layout = QVBoxLayout(self.sidebar)
        self.sidebar_layout.setContentsMargins(20, 26, 20, 20)
        sidebar_layout = self.sidebar_layout
        sidebar_layout.setSpacing(10)

        brand_row = QHBoxLayout()
        brand_mark = QLabel("LSB")
        brand_mark.setFixedWidth(84)
        brand_mark.setObjectName("brandMark")
        # Keep the compact product badge readable on Windows/Qt.
        brand_mark.setMaximumWidth(max(brand_mark.maximumWidth(), 72))
        brand_mark.setMinimumWidth(72)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        self.brand = QLabel(APP_NAME.upper())
        self.brand.setObjectName("brandTitle")
        self.brand_subtitle = QLabel("Панель управления")
        self.brand_subtitle.setObjectName("brandSubtitle")
        brand_text.addWidget(self.brand)
        brand_text.addWidget(self.brand_subtitle)
        brand_row.addWidget(brand_mark)
        brand_row.addLayout(brand_text, 1)
        sidebar_layout.addLayout(brand_row)
        sidebar_layout.addSpacing(20)

        self.menu = QListWidget()
        self.menu.setObjectName("navigation")
        self.menu.setIconSize(QSize(20, 20))
        self._menu_full = [
            "Аккаунт",
            "Каналы",
            "Связки",
            "Комментирование",
            "Парсинг аудитории",
            "Режим поиска ЦА",
            "Инструкция",
        ]
        self._menu_compact = [
            "Аккаунт",
            "Каналы",
            "Связки",
            "Комменты",
            "Парсинг",
            "Поиск ЦА",
            "Инструкция",
        ]
        for label, icon_name in zip(self._menu_full, self.MENU_ICONS):
            item = QListWidgetItem(self._asset_icon(icon_name), label)
            item.setToolTip(label)
            self.menu.addItem(item)
        self.menu.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar_layout.addWidget(self.menu, 1)

        self.version_label = QLabel(
            f"Версия {__version__} · {BUILD_ID}\nДанные хранятся локально"
        )
        self.version_label.setObjectName("brandSubtitle")
        self.version_label.setWordWrap(True)
        sidebar_layout.addWidget(self.version_label)

        self.help_button = QPushButton("Помощь")
        self.help_button.setIcon(self._asset_icon("help.svg"))
        self.help_button.setObjectName("sidebarHelpButton")
        self.help_button.setToolTip(f"Контакт поддержки: {self.SUPPORT_CONTACT}")
        self.help_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.help_button.clicked.connect(self._show_help_dialog)
        sidebar_layout.addWidget(self.help_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.stack = QStackedWidget()
        self.stack.setObjectName("contentStack")
        self.account_view = AccountView(adapter, config)
        self.channels_view = ChannelsView(adapter, queue_worker)
        self.links_view = LinksView(adapter)
        self.commenting_view = CommentingView(adapter)
        self.audience_parser_view = AudienceParserView(adapter)
        self.target_audience_view = TargetAudienceView()
        self.instructions_view = InstructionsView(adapter)
        self.account_view.account_changed.connect(
            self.commenting_view.handle_account_changed
        )
        for view in (
            self.account_view,
            self.channels_view,
            self.links_view,
            self.commenting_view,
            self.audience_parser_view,
            self.target_audience_view,
            self.instructions_view,
        ):
            scroll = QScrollArea()
            scroll.setObjectName("pageScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            scroll.setWidget(view)
            self.stack.addWidget(scroll)

        self.activity_panel = ActivityPanel(adapter)
        self.account_view.account_changed.connect(
            self.channels_view.handle_account_changed
        )
        self.account_view.account_changed.connect(
            self.links_view.handle_account_changed
        )
        self.account_view.account_changed.connect(
            self.audience_parser_view.handle_account_changed
        )
        self.account_view.account_changed.connect(
            self.activity_panel.handle_account_changed
        )
        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self.vertical_splitter.setObjectName("contentSplitter")
        self.vertical_splitter.setChildrenCollapsible(False)
        self.vertical_splitter.setHandleWidth(7)
        self.vertical_splitter.addWidget(self.stack)
        self.vertical_splitter.addWidget(self.activity_panel)
        self.vertical_splitter.setStretchFactor(0, 1)
        self.vertical_splitter.setStretchFactor(1, 0)
        self.vertical_splitter.setSizes([620, 205])

        self.horizontal_splitter.addWidget(self.sidebar)
        self.horizontal_splitter.addWidget(self.vertical_splitter)
        self.horizontal_splitter.setStretchFactor(0, 0)
        self.horizontal_splitter.setStretchFactor(1, 1)
        self.horizontal_splitter.setSizes([270, 1010])

        self.menu.currentRowChanged.connect(self._change_page)
        self.menu.setCurrentRow(0)
        root_layout.addWidget(self.horizontal_splitter)
        self.setCentralWidget(root)
        self._suspended_runtime_timers: list[object] = []

    def _show_help_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setObjectName("helpDialog")
        dialog.setWindowTitle("Помощь")
        dialog.setModal(True)
        dialog.setMinimumWidth(360)

        title = QLabel(f"Поддержка {APP_NAME}")
        title.setObjectName("cardTitle")
        contact = QLabel(
            f'Контакт: <a href="{self.SUPPORT_URL}">{self.SUPPORT_CONTACT}</a>'
        )
        contact.setObjectName("pageSubtitle")
        contact.setOpenExternalLinks(True)
        contact.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)

        open_button = QPushButton("Открыть Telegram")
        open_button.setObjectName("primaryButton")
        open_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(self.SUPPORT_URL))
        )
        close_button = QPushButton("Закрыть")
        close_button.setObjectName("secondaryButton")
        close_button.clicked.connect(dialog.accept)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addWidget(open_button)
        actions.addWidget(close_button)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(contact)
        layout.addLayout(actions)
        dialog.exec()

    def _runtime_refresh_timers(self) -> tuple[object, ...]:
        """Return GUI timers that may touch services or SQLite.

        These timers are deliberately separate from the application shutdown
        poller.  They must be stopped before the reset executor removes files,
        because modal dialogs run a nested Qt event loop and can otherwise fire
        while the schema is between deletion and recreation.
        """

        return (
            self.activity_panel.timer,
            self.activity_panel.countdown_timer,
            self.channels_view.timer,
            self.channels_view.watcher.timer,
            self.links_view.watcher.timer,
            self.commenting_view.refresh_timer,
            self.commenting_view.countdown_timer,
            self.commenting_view.limit_save_timer,
            self.audience_parser_view.watcher.timer,
        )

    def suspend_runtime_updates(self) -> None:
        if self._suspended_runtime_timers:
            return
        for timer in self._runtime_refresh_timers():
            is_active = getattr(timer, "isActive", None)
            stop = getattr(timer, "stop", None)
            if callable(is_active) and callable(stop) and bool(is_active()):
                self._suspended_runtime_timers.append(timer)
                stop()

    def resume_runtime_updates(self) -> None:
        timers = tuple(self._suspended_runtime_timers)
        self._suspended_runtime_timers.clear()
        for timer in timers:
            start = getattr(timer, "start", None)
            if callable(start):
                start()

    def _widen_sidebar_to_fit_menu(self) -> None:
        """Give the navigation the width its longest entry actually needs.

        "Комментирование" was rendering as "Комментирова…". The width required
        depends on the font, the icon and the stylesheet's padding, so it is
        measured from the laid-out items instead of hard-coded - a fixed number
        would clip again after any theme, font or DPI change.

        The measurement runs once, on first show. ``visualItemRect`` reports the
        larger of the item hint and the viewport, so re-measuring after the
        sidebar has grown would feed the new width back in and widen it again.
        """

        if self._sidebar_fitted or self.width() < 900:
            return
        needed_for_items = max(
            (
                self.menu.visualItemRect(self.menu.item(index)).width()
                for index in range(self.menu.count())
            ),
            default=0,
        )
        if needed_for_items <= 0:
            return
        self._sidebar_fitted = True
        margins = self.sidebar_layout.contentsMargins()
        needed = (
            needed_for_items
            + margins.left()
            + margins.right()
            + 2 * self.menu.frameWidth()
            + 4
        )
        if needed <= self.sidebar.width():
            return
        needed = min(needed, self.SIDEBAR_MAX_WIDTH)
        self.sidebar.setMaximumWidth(max(self.sidebar.maximumWidth(), needed))
        self.sidebar.setMinimumWidth(needed)
        sizes = self.horizontal_splitter.sizes()
        total = sum(sizes) if sizes else max(1, self.width())
        self.horizontal_splitter.setSizes([needed, max(1, total - needed)])

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self._widen_sidebar_to_fit_menu()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        compact = self.width() < 900
        self.brand.setVisible(not compact)
        self.brand_subtitle.setVisible(not compact)
        self.version_label.setVisible(not compact)
        self.sidebar_layout.setContentsMargins(
            12 if compact else 20,
            22 if compact else 26,
            12 if compact else 20,
            16 if compact else 20,
        )
        labels = self._menu_compact if compact else self._menu_full
        for index, label in enumerate(labels):
            self.menu.item(index).setText(label)
        self.activity_panel.set_compact(compact)
        for view in (
            self.account_view,
            self.channels_view,
            self.links_view,
            self.commenting_view,
            self.audience_parser_view,
            self.target_audience_view,
            self.instructions_view,
        ):
            setter = getattr(view, "set_compact_mode", None)
            if callable(setter):
                setter(compact)
        if compact:
            self.sidebar.setMaximumWidth(175)
            sizes = self.horizontal_splitter.sizes()
            total = sum(sizes) if sizes else max(1, self.width())
            if not sizes or sizes[0] > 175:
                self.horizontal_splitter.setSizes([165, max(1, total - 165)])
        else:
            self.sidebar.setMaximumWidth(max(310, self.sidebar.minimumWidth()))

    def _change_page(self, index: int):
        self.stack.setCurrentIndex(index)
        page_views = (
            self.account_view,
            self.channels_view,
            self.links_view,
            self.commenting_view,
            self.audience_parser_view,
            self.target_audience_view,
            self.instructions_view,
        )
        for page_index, view in enumerate(page_views):
            setter = getattr(view, "set_page_active", None)
            if callable(setter):
                setter(page_index == index)

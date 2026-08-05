from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QRadialGradient
from PySide6.QtWidgets import QWidget


class AuroraBackgroundWidget(QWidget):
    """One lightweight scalable aurora canvas shared by the whole main window."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("auroraBackground")
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(False)
        self._paint_count = 0

    @property
    def paint_count(self) -> int:
        return self._paint_count

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        del event
        self._paint_count += 1
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = self.rect()

        base = QLinearGradient(0, 0, rect.width(), rect.height())
        base.setColorAt(0.0, QColor("#050817"))
        base.setColorAt(0.48, QColor("#071326"))
        base.setColorAt(1.0, QColor("#10071F"))
        painter.fillRect(rect, base)

        layers = (
            (0.16, 0.18, 0.62, "#165DFF", 78),
            (0.78, 0.20, 0.55, "#7A35FF", 66),
            (0.52, 0.78, 0.70, "#00B7FF", 44),
            (0.92, 0.78, 0.48, "#B026FF", 34),
        )
        extent = float(max(rect.width(), rect.height(), 1))
        for x, y, radius, color, alpha in layers:
            gradient = QRadialGradient(
                QPointF(rect.width() * x, rect.height() * y), extent * radius
            )
            glow = QColor(color)
            glow.setAlpha(alpha)
            transparent = QColor(color)
            transparent.setAlpha(0)
            gradient.setColorAt(0.0, glow)
            gradient.setColorAt(1.0, transparent)
            painter.fillRect(rect, gradient)
        painter.end()

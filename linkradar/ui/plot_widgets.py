"""One bounded, DPI-aware chart surface; cursor moves do not rescan history."""
import math
from PySide6.QtCore import QLineF,QRectF,Qt
from PySide6.QtGui import QColor,QPainter,QPen,QPixmap
from PySide6.QtWidgets import QWidget
from linkradar.core.time_labels import format_timestamp


class CachedPlot(QWidget):
    def __init__(self):
        super().__init__();self._generation=0;self._surface=None;self._surface_key=None

    def invalidate_chart(self):
        self._generation+=1;self._surface=None

    def paintEvent(self,event):
        ratio=self.devicePixelRatioF()
        key=(self._generation,self.width(),self.height(),ratio,self.font().key(),
             getattr(self,'message',None),getattr(self,'title',None))
        if self._surface is None or key!=self._surface_key:
            image=QPixmap(math.ceil(self.width()*ratio),math.ceil(self.height()*ratio));image.setDevicePixelRatio(ratio)
            painter=QPainter(image);painter.setFont(self.font())
            try:self.draw_chart(painter)
            finally:painter.end()
            self._surface=image;self._surface_key=key
        painter=QPainter(self)
        painter.drawPixmap(0,0,self._surface)
        self.draw_overlay(painter)

    def draw_overlay(self,painter):
        pass

    def draw_time_cursor(self, painter, plot_rect, x, timestamp):
        label = format_timestamp(timestamp)
        color = QColor('#60a5fa')
        painter.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
        painter.drawLine(QLineF(x, plot_rect.top(), x, plot_rect.bottom() + 2))

        width = min(painter.fontMetrics().horizontalAdvance(label) + 12, self.width() - 4)
        left = max(2, min(x - width / 2, self.width() - width - 2))
        badge = QRectF(left, plot_rect.bottom() + 2, width, 17)
        painter.setPen(QPen(color, 1))
        painter.setBrush(QColor('#203f67'))
        painter.drawRoundedRect(badge, 3, 3)
        painter.setPen(QColor('#f0f6ff'))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)


class TimePlot(CachedPlot):
    """Cursor position tracks the current time axis, including after an update."""

    def __init__(self):
        super().__init__()
        self.points = []
        self.start, self.end = 0, 1
        self._cursor_x = None
        self.setMouseTracking(True)

    def _cursor_position(self, rect):
        x = max(rect.left(), min(rect.right(), self._cursor_x))
        timestamp = self.start + (x - rect.left()) / rect.width() * (self.end - self.start)
        return x, timestamp

    def track_time_cursor(self, event):
        self._cursor_x = event.position().x()
        self.update()
        return self._cursor_position(self.plot_rect())[1]

    def draw_overlay(self, painter):
        if self._cursor_x is None or not self.points:
            return
        rect = self.plot_rect()
        x, timestamp = self._cursor_position(rect)
        self.draw_time_cursor(painter, rect, x, timestamp)

    def leaveEvent(self, event):
        self._cursor_x = None
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event):
        self._cursor_x = None
        super().hideEvent(event)

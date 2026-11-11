"""One bounded, DPI-aware chart surface; cursor moves do not rescan history."""
import math
from PySide6.QtGui import QPainter,QPixmap
from PySide6.QtWidgets import QWidget


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

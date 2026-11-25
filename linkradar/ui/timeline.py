"""Shared bounded history navigation; moving to the right resumes live following."""
import math
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QHBoxLayout, QScrollBar, QToolButton


class Timeline(QWidget):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.focus_end = None
        self.begin = self.end = 0
        self.period = 300
        row = QHBoxLayout(self); row.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollBar(Qt.Orientation.Horizontal)
        self.scroll.setAccessibleName('Прокрутка истории графика')
        self.scroll.setToolTip('Переместите ползунок для просмотра истории; справа — текущие измерения')
        self.scroll.valueChanged.connect(self.seek)
        row.addWidget(self.scroll, 1)
        self.live = QToolButton(); self.live.setText('Сейчас')
        self.setFixedHeight(22);self.scroll.setFixedHeight(18);self.live.setFixedHeight(22)
        self.live.setStyleSheet('QToolButton{padding:0 6px;}')
        self.live.setAccessibleName('Вернуться к текущим измерениям')
        self.live.clicked.connect(self.follow); row.addWidget(self.live)
        self.set_bounds(0,0,self.period)

    def set_bounds(self, begin, end, period):
        self.begin = max(begin, end - 12 * 3600)
        self.end = end; self.period = period
        earliest = min(end, self.begin + period)
        if earliest>=end:self.focus_end=None
        if self.focus_end is not None:
            self.focus_end = min(end, max(earliest, self.focus_end))
        self.scroll.blockSignals(True)
        self.scroll.setRange(0, max(0, math.ceil(end - earliest)))
        self.scroll.setPageStep(max(1, int(period)))
        self.scroll.setSingleStep(max(1, int(period / 10)))
        self.scroll.setValue(self.scroll.maximum() if self.focus_end is None else round(self.focus_end - earliest))
        self.scroll.blockSignals(False)
        self.live.setEnabled(self.focus_end is not None)

    def seek(self, value):
        self.focus_end = None if value == self.scroll.maximum() else min(self.end, self.begin + self.period) + value
        self.live.setEnabled(self.focus_end is not None)
        self.changed.emit()

    def follow(self):
        self.focus_end = None
        self.set_bounds(self.begin, self.end, self.period)
        self.changed.emit()

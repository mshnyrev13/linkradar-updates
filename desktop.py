"""Native Qt desktop UI. Direct monitoring engine; no HTTP server or browser."""
import json
import math
import os
import sys
import time
import threading
import queue
from pathlib import Path
from urllib.parse import urlsplit
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QRectF, QUrl, QtMsgType, qInstallMessageHandler
from PySide6.QtGui import QPainter, QColor, QPen, QIcon, QPainterPath, QDesktopServices, QAction
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QPlainTextEdit, QToolButton, QMessageBox, QStyledItemDelegate, QGridLayout, QSplitter, QSizePolicy, QFileDialog, QTabWidget, QMenuBar, QMenu)
from time_labels import format_timestamp
import error_log
import server
import network_info
import speed_test
import session_export
import export_transfer
import ui_tasks
import update_checker
from log_widgets import update_lines,format_events
from plot_sampling import nearest_point
from plot_widgets import CachedPlot
import background_engine
import mac_background
import mac_capture
import service_paths
import graphene_port
from port_widgets import PortPanel
from traffic_widgets import TrafficPanel,capture_icon
from stream_widgets import StreamPanel
from about_dialog import AboutDialog
from app_identity import NAME

ROOT = Path(__file__).resolve().parent
# Graphene and the lower diagnostics row share vertical space. Route and latency
# are adjacent columns within the lower row.
BLOCK_WEIGHTS=(1,1.7)
SIDEBAR_WIDTH=180
PERIODS = [(60,'1 мин'),(300,'5 мин'),(600,'10 мин'),(900,'15 мин'),(1200,'20 мин'),
           (1800,'30 мин'),(3600,'1 час'),(7200,'2 часа'),(21600,'6 часов'),(43200,'12 часов')]

def fmt(n, digits=1):
    return '—' if n is None else f'{n:.{digits}f}'.replace('.', ',')

def endpoint(value):
    value=value.strip()
    if value.lower().startswith(('rtmp://','rtmps://')):
        u=urlsplit(value)
        if u.username or u.password: raise ValueError('Укажите адрес без логина и пароля')
        return server.host_value(u.hostname or ''), u.port or (443 if u.scheme=='rtmps' else 1935)
    return server.host_value(value),1935

class Snapshot(QThread):
    ready=Signal(object)
    failed=Signal(str)
    def __init__(self, monitor, sid, period, selected_node=None, event_after=0, revision=0):
        super().__init__();self.monitor=monitor;self.sid=sid;self.period=period;self.selected_node=selected_node;self.event_after=event_after;self.revision=revision;self.stop=threading.Event()
    def run(self):
        try:
            result=self.monitor.snapshot(self.sid,self.period,compact=True,selected_node=self.selected_node,event_after=self.event_after,cancel=self.stop)
            result['_ui_revision']=self.revision;result['_ui_period']=self.period
            if not self.stop.is_set():self.ready.emit(result)
        except Exception as exc:
            if self.stop.is_set():return
            error_log.error('Не удалось получить снимок измерений',exc,key='snapshot');self.failed.emit(str(exc))

class ExportWorker(QThread):
    failed=Signal(str)
    def __init__(self,store,service,sid,path,names,barrier=None,warning=None):
        super().__init__();self.store=store;self.service=service;self.sid=sid;self.path=path;self.names=names;self.stop=threading.Event();self.barrier=barrier;self.warning=warning
    def run(self):
        try:
            while self.barrier and not self.barrier.wait(.1):
                if self.stop.is_set():raise InterruptedError('Экспорт отменён')
            warning=self.warning() if self.warning else None
            prefix='ВНИМАНИЕ: часть данных сессии не записана. '+' '.join(error_log.scrub(str(warning)).split())[:500]+'\n' if warning else ''
            if self.service:self.service.export_to(self.sid,self.path,self.names,self.stop,prefix=prefix)
            else:
                import io
                def write(output):
                    wrapper=io.TextIOWrapper(output,encoding='utf-8',write_through=True)
                    try:
                        wrapper.write(prefix);session_export.write_txt(self.store,self.sid,wrapper,self.names,self.stop)
                    finally:wrapper.flush();wrapper.detach()
                export_transfer.atomic_write(self.path,write)
        except InterruptedError:pass
        except Exception as exc:
            error_log.error('Экспорт TXT',exc);self.failed.emit(str(exc))

class Discovery(QThread):
    ready=Signal(object)
    def __init__(self):
        super().__init__();self.stop=threading.Event();self.selected=None
    def run(self):
        try:network_info.discover(self.stop,lambda:self.selected,self.ready.emit)
        except Exception as exc:error_log.error('Сбор сетевых сведений остановлен',exc)

class SpeedWorker(QThread):
    progress=Signal(object)
    ready=Signal(object)
    failed=Signal(str)
    def __init__(self,interface=None):
        super().__init__();self.stop=threading.Event();self.interface=interface
    def run(self):
        try:self.ready.emit(speed_test.SpeedTest(self.stop,self.progress.emit,interface=self.interface).run())
        except speed_test.Cancelled:self.failed.emit('Измерение отменено')
        except Exception as exc:
            error_log.error('Тест скорости',exc);self.failed.emit('Не удалось измерить скорость: проверьте доступ к интернетометру')

def latency_bands(painter,rect,maximum):
    thresholds=(100,200)
    for low,high,color in [(0,thresholds[0],'#17352b'),(thresholds[0],thresholds[1],'#403820'),(thresholds[1],maximum,'#40252f')]:
        low=min(maximum,low);high=min(maximum,high)
        if high>low:painter.fillRect(QRectF(rect.left(),rect.bottom()-high/maximum*rect.height(),rect.width(),(high-low)/maximum*rect.height()),QColor(color))

def route_scale(hops):
    peak=max([v for h in hops for v in (h['stats']['max'],h['stats']['avg'],h['last'].get('latency')) if v is not None]+[1])
    return max(10,math.ceil(peak*1.03))

class RouteHeader(QHeaderView):
    def __init__(self,parent):
        super().__init__(Qt.Orientation.Horizontal,parent);self.scale=50;self.setMinimumHeight(28)
    def paintSection(self,painter,rect,section):
        if section!=8:return super().paintSection(painter,rect,section)
        painter.save();painter.fillRect(rect,QColor('#1a2638'));painter.setPen(QColor('#91a2bb'))
        r=QRectF(rect).adjusted(8,0,-8,0)
        painter.drawText(r,Qt.AlignmentFlag.AlignCenter,'Latency')
        painter.drawText(r,Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,'0 мс')
        painter.drawText(r,Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter,fmt(self.scale,0)+' мс')
        painter.restore()

class RouteTable(QTableWidget):
    focused=Signal(int)
    def keyPressEvent(self,event):
        super().keyPressEvent(event)
        if event.key() in (Qt.Key.Key_Up,Qt.Key.Key_Down,Qt.Key.Key_Home,Qt.Key.Key_End,Qt.Key.Key_PageUp,Qt.Key.Key_PageDown):self.focused.emit(self.currentRow())
    def resizeEvent(self,event):
        super().resizeEvent(event)
        compact=self.width()<560
        for column in (1,3):
            if self.isColumnHidden(column)!=compact:self.setColumnHidden(column,compact)
    def paintEvent(self,event):
        super().paintEvent(event)
        painter=QPainter(self.viewport());painter.setRenderHint(QPainter.RenderHint.Antialiasing);painter.setPen(QPen(QColor('#00c98d'),2));last=None
        for row in range(self.rowCount()):
            item=self.item(row,8);data=item.data(Qt.ItemDataRole.UserRole) if item else None
            if not data or data[1] is None:continue
            rect=QRectF(self.visualItemRect(item)).adjusted(8,0,-8,0)
            point=(rect.left()+rect.width()*min(1,data[1]/data[4]),rect.center().y())
            if last:painter.drawLine(int(last[0]),int(last[1]),int(point[0]),int(point[1]))
            last=point

class RouteSummary(QWidget):
    def __init__(self,table):
        super().__init__();self.table=table;self.stats={};self.current=None;self.setFixedHeight(26)
        table.horizontalHeader().sectionResized.connect(self.update)
        table.horizontalScrollBar().valueChanged.connect(self.update)
        self.setToolTip('Итог конечного сервера за выбранный период, независимо от выбранного hop. Cur — последнее измерение.')
    def paintEvent(self,event):
        painter=QPainter(self);painter.fillRect(self.rect(),QColor('#1a2638'));painter.setPen(QColor('#dce6f4'))
        font=painter.font();font.setBold(True);painter.setFont(font);header=self.table.horizontalHeader()
        def cell(col):return QRectF(header.sectionViewportPosition(col)+self.table.frameWidth(),0,header.sectionSize(col),self.height()).adjusted(4,0,-4,0)
        label=cell(2) if self.table.isColumnHidden(3) else QRectF(cell(2).left(),0,cell(3).right()-cell(2).left(),self.height())
        value_font=painter.font();font=painter.font();font.setPointSizeF(max(8,font.pointSizeF()-1));painter.setFont(font)
        painter.drawText(label,Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter,'Round Trip (ms)')
        painter.setFont(value_font)
        for col,value in [(1,str(self.stats.get('count','—'))),(4,fmt(self.stats.get('avg'))),(5,fmt(self.stats.get('min'))),(6,fmt(self.current))]:
            if not self.table.isColumnHidden(col):painter.drawText(cell(col),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,value)

class RangeDelegate(QStyledItemDelegate):
    def paint(self,painter,option,index):
        data=index.data(Qt.ItemDataRole.UserRole)
        if not data: return super().paint(painter,option,index)
        painter.save();r=QRectF(option.rect).adjusted(8,5,-8,-5)
        if option.state & __import__('PySide6.QtWidgets',fromlist=['QStyle']).QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect,QColor('#203f67'))
        minimum,average,maximum,current,scale=data
        x=lambda v:r.left()+r.width()*min(1,max(0,v/scale))
        for low,high,color in [(0,100,'#08382b'),(100,200,'#463b1b'),(200,scale,'#45232b')]:
            if min(scale,high)>min(scale,low):
                left=option.rect.left() if low==0 else x(low)
                right=option.rect.right()+1 if high>=scale else x(high)
                painter.fillRect(QRectF(left,option.rect.top(),right-left,option.rect.height()),QColor(color))
        painter.setPen(QPen(QColor('#92a5b7'),1));y=r.center().y()
        if minimum is not None:
            painter.drawLine(int(x(minimum)),int(y),int(x(maximum)),int(y))
            for v in (minimum,maximum): painter.drawLine(int(x(v)),int(y-4),int(x(v)),int(y+4))
            if maximum>scale:
                painter.drawLine(int(r.right()-4),int(y-4),int(r.right()),int(y));painter.drawLine(int(r.right()-4),int(y+4),int(r.right()),int(y))
        if average is not None:
            painter.setBrush(QColor('#00c98d'));painter.setPen(Qt.PenStyle.NoPen);painter.drawEllipse(QRectF(x(average)-3,y-3,6,6))
        if current is not None:
            painter.setPen(QPen(QColor('#42b4ff'),3));painter.drawLine(int(x(current)),int(y-6),int(x(current)),int(y+6))
        painter.restore()

def jitter_scale(peak):
    upper=max(1,peak*1.15);power=10**math.floor(math.log10(upper))
    return next(multiplier*power for multiplier in (1,2,5,10) if multiplier*power>=upper)

class Graph(CachedPlot):
    hover=Signal(str)
    zoom=Signal(int)
    def __init__(self,jitter=False):
        super().__init__();self.jitter=jitter;self.points=[];self.start=0;self.end=1;self.cursor=None;self.load_windows=[]
        self.setMouseTracking(True);self.setMinimumHeight(50 if jitter else 96);self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Expanding)
        self.setToolTip('Jitter — сглаженное изменение соседних RTT (EWMA, alpha=1/16), мс. ICMP jitter не определяет качество видеопотока.' if jitter else 'Цветовые зоны — ориентиры ICMP RTT 100/200 мс. Они не определяют качество RTMP. Оранжевый пунктир — локальная ошибка проверки; красная отметка — нет ICMP-ответа.')
    def set_data(self,points,end,period):
        self.points=points;self.end=end;self.start=end-period;self.invalidate_chart();self.update()
    def draw_chart(self,p):
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(),QColor('#141d2b'));r=QRectF(48,10,self.width()-66,self.height()-(20 if self.jitter else 35))
        key='jitter' if self.jitter else 'latency';peak=max([q.get(key) or 0 for q in self.points]+[0])
        maximum=jitter_scale(peak) if self.jitter else max(50,math.ceil(peak*1.15/50)*50)
        x=lambda ts:r.left()+(ts-self.start)/max(1,self.end-self.start)*r.width()
        y=lambda v:r.bottom()-v/maximum*r.height()
        if not self.jitter:latency_bands(p,r,maximum)
        for begin,end in self.load_windows:
            left=max(self.start,begin);right=min(self.end,end or time.time())
            if right>left:p.fillRect(QRectF(x(left),r.top(),x(right)-x(left),r.height()),QColor(37,99,235,45))
        p.setPen(QColor('#91a2bb'))
        ticks=(3 if self.height()<110 else 5) if self.jitter else (4 if self.height()<160 else 6)
        for i in range(ticks):
            v=maximum*i/(ticks-1);yy=y(v)
            p.setPen(QPen(QColor('#2b3a50'),1,Qt.PenStyle.DashLine));p.drawLine(int(r.left()),int(yy),int(r.right()),int(yy))
            p.setPen(QColor('#91a2bb'));p.drawText(QRectF(0,yy-8,42,16),Qt.AlignmentFlag.AlignRight,fmt(v,(2 if maximum<5 else 1 if maximum<50 else 0) if self.jitter else 0))
        p.drawText(54,10,'Jitter, мс' if self.jitter else 'мс')
        if not self.jitter and self.points:
            label_width=max(60,p.fontMetrics().horizontalAdvance('00:00:00')+8)
            time_ticks=max(2,min(7,int(r.width()/(label_width*1.5+8))+1))
            for i in range(time_ticks):
                ts=self.start+(self.end-self.start)*i/(time_ticks-1)
                left=r.left() if i==0 else r.right()-label_width if i==time_ticks-1 else x(ts)-label_width/2
                alignment=Qt.AlignmentFlag.AlignLeft if i==0 else Qt.AlignmentFlag.AlignRight if i==time_ticks-1 else Qt.AlignmentFlag.AlignCenter
                p.drawText(QRectF(left,r.bottom()+5,label_width,18),alignment,format_timestamp(ts))
        last=None
        for q in self.points:
            value=q.get(key)
            if value is None or q['status']!='ok':
                last=None
                if not self.jitter:
                    local_error=q['status']=='error'
                    p.setPen(QPen(QColor('#d39d45' if local_error else '#ef6275'),1,Qt.PenStyle.DashLine if local_error else Qt.PenStyle.SolidLine));p.drawLine(int(x(q['ts'])),int(r.top()),int(x(q['ts'])),int(r.bottom()))
                continue
            xx,yy=x(q['ts']),y(value)
            p.setPen(QPen(QColor('#f59e75' if self.jitter else '#dce6f4'),1.3))
            if last and not q.get('break_before') and q['ts']-last[2]<max(3,(self.end-self.start)/200):
                p.drawLine(int(last[0]),int(last[1]),int(xx),int(yy))
            else:p.drawEllipse(QRectF(xx-1,yy-1,2,2))
            last=(xx,yy,q['ts'])
        if not self.points:p.setPen(QColor('#91a2bb'));p.drawText(r,Qt.AlignmentFlag.AlignCenter,'Ожидаем измерения')
    def draw_overlay(self,p):
        if self.cursor is not None:
            p.setPen(QPen(QColor('#60a5fa'),1,Qt.PenStyle.DashLine))
            p.drawLine(int(self.cursor),10,int(self.cursor),self.height()-(10 if self.jitter else 25))
    def mouseMoveEvent(self,event):
        if not self.points:return
        xx=min(self.width()-18,max(48,event.position().x()));ts=self.start+(xx-48)/max(1,self.width()-66)*(self.end-self.start)
        q=nearest_point(self.points,ts,key=lambda point:point['ts']);self.cursor=xx;self.update()
        status=fmt(q.get('latency'),2)+' мс' if q['status']=='ok' else {'error':'локальная ошибка проверки','unreachable':'цель недоступна (ICMP)','refused':'соединение отклонено'}.get(q['status'],'нет ответа')
        text=format_timestamp(q['ts'])+' · RTT '+status+' · Jitter '+fmt(q.get('jitter'),2)+' мс'
        self.setToolTip(text);self.hover.emit(text)
    def leaveEvent(self,event):self.cursor=None;self.update()
    def wheelEvent(self,event):self.zoom.emit(1 if event.angleDelta().y()<0 else -1);event.accept()

STYLE='''
QWidget{background:#0e1420;color:#dce6f4;font-family:"Arial","Segoe UI";font-size:12px;}
QLineEdit,QComboBox{background:#141d2b;border:1px solid #344459;border-radius:6px;padding:7px;}
QPushButton{background:#2563eb;border:1px solid #3b82f6;border-radius:6px;padding:8px 14px;}
QPushButton:disabled{background:#263449;color:#91a2bb;}
QLabel#card{background:#172437;border:1px solid #27374e;border-radius:6px;padding:7px;font-size:15px;}
QTableWidget{background:#141d2b;alternate-background-color:#172437;border:1px solid #263449;gridline-color:#263449;selection-background-color:#203f67;}
QHeaderView::section{background:#1a2638;color:#91a2bb;border:0;padding:6px;}
QPlainTextEdit{background:#141d2b;border:1px solid #263449;}
QToolButton{border:0;color:#91a2bb;padding:6px;text-align:left;}
QTabWidget::pane{border:0;}
QTabBar::tab{background:#141d2b;color:#91a2bb;border:1px solid #263449;border-bottom:0;border-top-left-radius:6px;border-top-right-radius:6px;padding:5px 16px;margin-right:2px;}
QTabBar::tab:selected{background:#1a2638;color:#dce6f4;}
'''

class ProgressButton(QPushButton):
    """One actionable control with an accessible, in-button overall progress."""
    def __init__(self,text):
        super().__init__(text);self.progress=0;self.setFixedHeight(32);self.setMinimumWidth(180)
    def set_progress(self,value,text=None):
        self.progress=max(0,min(100,int(value)))
        if text is not None:self.setText(text)
        self.setAccessibleDescription(f'Общий прогресс теста скорости: {self.progress}%');self.update()
    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds=QRectF(self.rect()).adjusted(.5,.5,-.5,-.5);shape=QPainterPath();shape.addRoundedRect(bounds,6,6)
        painter.fillPath(shape,QColor('#172437'));painter.save();painter.setClipPath(shape)
        if self.progress:painter.fillRect(QRectF(0,0,self.width()*self.progress/100,self.height()),QColor('#2563eb' if self.isEnabled() else '#425470'))
        painter.restore();painter.setPen(QPen(QColor('#93c5fd' if self.hasFocus() or self.underMouse() else '#3b82f6'),1));painter.drawPath(shape)
        painter.setPen(QColor('#f8fafc'));painter.drawText(self.rect().adjusted(7,0,-7,0),Qt.AlignmentFlag.AlignCenter,self.fontMetrics().elidedText(self.text(),Qt.TextElideMode.ElideRight,self.width()-14))

class CompactValue(QLabel):
    def paintEvent(self,event):
        painter=QPainter(self);painter.setPen(QColor('#dce6f4'))
        painter.drawText(self.rect(),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,self.fontMetrics().elidedText(self.text(),Qt.TextElideMode.ElideRight,self.width()))

class ServerSelector(QComboBox):
    def __init__(self):
        super().__init__();self.setEditable(True);self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addItems(['src-ugc.strm.yandex.net','ya.ru','google.com']);self.lineEdit().clear()
        self.returnPressed=self.lineEdit().returnPressed
        self.activated.connect(self.choose_preset)
    def choose_preset(self,index):
        if self.itemData(index):self.setEditText(self.itemData(index))
    def set_gateway(self,value):
        value=value or None
        existing=self.itemData(3) if self.count()>3 else None
        if value==existing:return
        typed=self.currentText();editor=self.lineEdit();cursor=editor.cursorPosition()
        selection=editor.selectionStart();length=len(editor.selectedText());blocked=self.blockSignals(True)
        try:
            if value:
                if self.count()>3:
                    self.setItemText(3,'Шлюз · '+value);self.setItemData(3,value)
                else:self.addItem('Шлюз · '+value,value)
            elif self.count()>3:self.removeItem(3)
            self.setEditText(typed)
            if selection>=0:editor.setSelection(selection,length)
            else:editor.setCursorPosition(cursor)
        finally:self.blockSignals(blocked)
    def text(self):return self.currentText()
    def setText(self,value):self.setEditText(value);self.lineEdit().setCursorPosition(0)
    def setPlaceholderText(self,value):self.lineEdit().setPlaceholderText(value)

class Window(QMainWindow):
    def __init__(self,folder,require_service=False,service_folder=None):
        super().__init__();self.setWindowTitle(NAME);self.setMinimumSize(900,640)
        self.folder=Path(folder);self.about_dialog=None;self.update_release=None
        self.updater=update_checker.UpdateChecker(server.VERSION,self)
        self.updater.available.connect(self.offer_update);self.updater.checked.connect(self.update_checked);self.updater.failed.connect(self.update_failed)
        self.update_action=QAction('Проверить обновления…',self);self.update_action.triggered.connect(lambda:self.updater.check(True))
        self.updater.busyChanged.connect(lambda busy:self.update_action.setEnabled(not busy))
        self.about_action=QAction('О '+NAME+'…',self)
        self.about_action.setMenuRole(QAction.MenuRole.AboutRole)
        self.about_action.triggered.connect(self.show_about)
        # A parentless menu bar is the macOS application-wide default; it adds
        # no row to the diagnostic window, including when no window is active.
        self.app_menu_bar=None
        if sys.platform=='darwin':
            self.app_menu_bar=QMenuBar();self.destroyed.connect(self.app_menu_bar.deleteLater)
            help_menu=self.app_menu_bar.addMenu('Справка');help_menu.addAction(self.about_action);help_menu.addAction(self.update_action)
        screen=QApplication.primaryScreen();self.resize(max(900,min(1100,int(screen.availableGeometry().width()*.9))) if screen else 1100,max(640,min(720,int(screen.availableGeometry().height()*.9))) if screen else 720)
        self.setWindowIcon(app_icon())
        # service_folder False: the engine location is unknown, run without it.
        self.service=None if service_folder is False else background_engine.connect(folder if service_folder is None else service_folder)
        if require_service and self.service is None:raise RuntimeError('Фоновый движок недоступен. Перезапустите приложение; второй движок не запущен.')
        if self.service and not background_engine.current(self.service.status):
            raise RuntimeError(background_engine.version_error(self.service.status))
        # A current engine owns its database; the GUI adds session data only through IPC.
        self.remote=bool(self.service) and 'session_data' in self.service.status.get('capabilities',[])
        self.store=None if self.remote else server.Store(folder/'network.sqlite3',recover=self.service is None,retention_seconds=server.RETENTION_SECONDS);self.monitor=self.service or server.Monitor(self.store)
        self.pending_restart=False;self.pending_config=None;self.session_config=None;self.paused=False;self.sid=None;self.worker=None;self.selected_node=None;self.snapshot={};self.closing=False
        self.export_worker=None;self.discovery=None;self.speed_worker=None;self.speed_active=False;self.load_windows=[];self.log_lines=[];self.network_snapshot=None;self.port_seen={};self.port_pending={};self.selected_interface=None;self.interface_names={}
        self.state_revision=0;self.command_busy=False;self.data_error=None;self.cleanup_worker=None;self.closed_clean=False;self.prune_pending=False;self.port_prune_at=0
        self.io=ui_tasks.IOQueue();self.io.completed.connect(self.io_completed);self.io.start()
        self.tabs=QTabWidget();self.tabs.setDocumentMode(True);self.setCentralWidget(self.tabs)
        container=QWidget();self.tabs.addTab(container,'Сеть');layout=QVBoxLayout(container);layout.setContentsMargins(6,6,6,6);layout.setSpacing(5)
        self.update_banner=QWidget();update_row=QHBoxLayout(self.update_banner);update_row.setContentsMargins(6,3,6,3)
        self.update_label=QLabel();update_row.addWidget(self.update_label,1)
        self.update_download=QPushButton('Скачать');self.update_download.clicked.connect(self.open_update);update_row.addWidget(self.update_download)
        later=QToolButton();later.setText('Позже');later.clicked.connect(self.update_banner.hide);update_row.addWidget(later)
        self.update_banner.hide();layout.addWidget(self.update_banner)
        self.identification_title=QLabel('Сетевая идентификация');self.identification_title.setFixedHeight(16);self.identification_title.setStyleSheet('font-size:11px;font-weight:600;')
        self.identification_title.setToolTip('Определяет сетевое подключение компьютера через LLDP и показывает его IP- и MAC-адреса.');layout.addWidget(self.identification_title)
        network_header=QHBoxLayout();network_header.addWidget(QLabel('Адаптер'))
        self.nic=QComboBox();self.nic.setStyleSheet('QComboBox{padding:3px 6px;}');self.nic.setMinimumWidth(140);self.nic.setMaximumWidth(260);self.nic.addItem('Автоматически',None);self.nic.currentIndexChanged.connect(self.select_interface);network_header.addWidget(self.nic)
        self.nic.setToolTip('Выбирает адаптер для ping, маршрута, локального захвата пакетов, сетевых сведений и LLDP. Автоматически — маршрут ОС. Тест скорости использует выбранный адаптер; DNS — резолвер ОС.')
        self.lldp_status=QLabel('Собираем сетевые данные…');self.lldp_status.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);network_header.addWidget(self.lldp_status,1)
        self.copy_button=QToolButton();self.copy_button.setText('Copy');self.copy_button.setToolTip('Скопировать Switch, Port, VLAN, Graphene, IP, MASK, GATEWAY и MAC');self.copy_button.clicked.connect(self.copy_network);network_header.addWidget(self.copy_button)
        self.tools_button=QToolButton();self.tools_button.setText('⚙');self.tools_button.setAccessibleName('Журналы и экспорт');self.tools_button.setToolTip('Журналы, экспорт TXT и справка');self.tools_button.setFixedSize(28,26)
        self.tools_button.setStyleSheet('QToolButton{font-size:18px;color:#dce6f4;padding:0;} QToolButton::menu-indicator{image:none;}')
        self.tools_menu=QMenu(self.tools_button);self.tools_button.setMenu(self.tools_menu);self.tools_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup);network_header.addWidget(self.tools_button);layout.addLayout(network_header)
        self.setup_notice=QLabel();self.setup_notice.setWordWrap(True);self.setup_notice.setStyleSheet('color:#d6bb71;font-size:11px;');self.setup_notice.hide();layout.addWidget(self.setup_notice)
        self.network_panel=QWidget();self.network_panel.setFixedWidth(SIDEBAR_WIDTH)
        self.network_layout=QVBoxLayout(self.network_panel);self.network_layout.setContentsMargins(0,0,0,0);self.network_layout.setSpacing(7)
        network_grid=QGridLayout();network_grid.setContentsMargins(0,0,0,0);network_grid.setVerticalSpacing(2);network_grid.setHorizontalSpacing(6);self.network_fields={}
        for i,key in enumerate(['switch','port','vlan','ip','mask','gateway','dns','mac']):
            title=QLabel({'switch':'Switch:','port':'Port:','vlan':'VLAN:','ip':'IP:','mask':'MASK:','gateway':'GATEWAY:','dns':'DNS:','mac':'MAC:'}[key]);title.setStyleSheet('color:#91a2bb;font-size:10px;')
            value=CompactValue('—');value.setMinimumHeight(14);value.setStyleSheet('font-size:11px;');value.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);value.setTextFormat(Qt.TextFormat.PlainText);value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse);value.setWordWrap(False);self.network_fields[key]=value
            network_grid.addWidget(title,i,0);network_grid.addWidget(value,i,1)
        network_grid.setColumnStretch(1,1)
        self.network_layout.addLayout(network_grid)
        self.address=ServerSelector();self.address.setPlaceholderText('IP или имя сервера');self.address.setAccessibleName('Адрес сервера');self.address.currentTextChanged.connect(self.change_server)
        self.address.setStyleSheet('QComboBox{padding:5px 6px;}');self.address.setMinimumWidth(0);self.address.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
        self.interval=QComboBox()
        for value,label in [(.5,'0,5 сек'),(1,'1 сек'),(2,'2 сек')]:self.interval.addItem(label,value)
        self.family=QComboBox()
        for label,value in [('IP авто','auto'),('IPv4','ipv4'),('IPv6','ipv6')]:self.family.addItem(label,value)
        self.family.setToolTip('Для сравнения с PingPlotter выберите такое же семейство IP и конечный адрес')
        self.button=QPushButton();self.button.setFixedSize(38,28);self.button.setStyleSheet('QPushButton{padding:5px;}');self.button.clicked.connect(self.toggle);self.address.returnPressed.connect(self.toggle)
        self.live=QLabel('ОСТАНОВЛЕНО');self.live.setStyleSheet('color:#91a2bb;font-size:10px;')
        self.port_panel=PortPanel();self.port_panel.received.connect(self.record_port)
        self.port_splitter=QSplitter(Qt.Orientation.Vertical);self.port_splitter.setChildrenCollapsible(False);self.port_splitter.setHandleWidth(6)
        network_row=QWidget();network_columns=QHBoxLayout(network_row);network_columns.setContentsMargins(0,0,0,0);network_columns.setSpacing(8)
        network_columns.addWidget(self.network_panel,0,Qt.AlignmentFlag.AlignTop);network_columns.addWidget(self.port_panel,1)
        self.port_splitter.addWidget(network_row);lower=QWidget();self.port_splitter.addWidget(lower);layout.addWidget(self.port_splitter,1)
        layout=QVBoxLayout(lower);layout.setContentsMargins(0,0,0,0);layout.setSpacing(6)
        self.traffic_panel=TrafficPanel();self.capture_height=150
        self.traffic_splitter=QSplitter(Qt.Orientation.Vertical);self.traffic_splitter.setChildrenCollapsible(False);self.traffic_splitter.setHandleWidth(6)
        self.traffic_splitter.addWidget(self.traffic_panel);monitor_panel=QWidget();self.traffic_splitter.addWidget(monitor_panel);layout.addWidget(self.traffic_splitter,1)
        self.traffic_splitter.setStretchFactor(0,0);self.traffic_splitter.setStretchFactor(1,1);self.traffic_splitter.setSizes([self.traffic_panel.compact_height(),700])
        self.traffic_splitter.setToolTip('Перетащите разделитель под захватом пакетов вниз, чтобы увеличить высоту таблицы')
        self.traffic_panel.expansionChanged.connect(self.size_capture);self.traffic_splitter.splitterMoved.connect(self.remember_capture_height)
        layout=QVBoxLayout(monitor_panel);layout.setContentsMargins(0,0,0,0);layout.setSpacing(4)
        self.diagnostics_title=QLabel('Диагностики сети');self.diagnostics_title.setFixedHeight(16);self.diagnostics_title.setStyleSheet('font-size:11px;font-weight:600;')
        self.diagnostics_title.setToolTip('Регулярно проверяет маршрут до сервера и показывает задержки и потери пакетов на графике.');layout.addWidget(self.diagnostics_title)
        diagnostics=QWidget();columns=QHBoxLayout(diagnostics);columns.setContentsMargins(0,0,0,0);columns.setSpacing(8)
        sidebar=QWidget();sidebar.setFixedWidth(SIDEBAR_WIDTH);controls=QVBoxLayout(sidebar);controls.setContentsMargins(0,0,0,0);controls.setSpacing(3);columns.addWidget(sidebar)
        self.port_splitter.setStretchFactor(0,0);self.port_splitter.setStretchFactor(1,1);self.port_splitter.setSizes([144,746]);self.port_splitter.setToolTip('Перетащите разделитель под Graphene для изменения высоты графиков')
        self.speed_button=ProgressButton('Тест');self.speed_button.clicked.connect(self.measure_speed);self.speed_button.setAccessibleName('Измерить скорость')
        self.speed_button.setMinimumWidth(0);self.speed_button.setFixedSize(40,26);self.speed_button.setStyleSheet('font-size:10px;')
        self.speed_button.setToolTip('Измерить скорость: 15 секунд измерения + 3 секунды прогрева на каждое направление. Нажмите повторно для отмены. Тест нагружает канал и влияет на ping.')
        self.speed_readout=CompactValue('↓ — ↑ — Мбит/с');self.speed_readout.setFixedSize(SIDEBAR_WIDTH-44,26);self.speed_readout.setStyleSheet('font-size:10px;')
        server_title=QLabel('Сервер');server_title.setStyleSheet('color:#91a2bb;font-size:11px;');controls.addWidget(server_title);controls.addWidget(self.address)
        options=QHBoxLayout();options.setSpacing(4)
        for control in (self.family,self.interval):
            control.setStyleSheet('QComboBox{padding:4px 6px;}');options.addWidget(control,1)
        controls.addLayout(options);play_row=QHBoxLayout();play_row.addWidget(self.button);play_row.addWidget(self.live,1);controls.addLayout(play_row)
        self.cards=[];self.card_titles=['Jitter','Ping без ответа','Средняя задержка','Максимум']
        self.metrics=QWidget();self.metrics.setObjectName('metrics');self.metrics.setStyleSheet('QWidget#metrics{background:#141d2b;border:1px solid #263449;border-radius:6px;}')
        metrics=QGridLayout(self.metrics);metrics.setContentsMargins(7,4,7,4);metrics.setHorizontalSpacing(6);metrics.setVerticalSpacing(0);metrics.setColumnStretch(0,1)
        for row,title in enumerate(self.card_titles):
            caption=QLabel('Средняя' if row==2 else title);caption.setToolTip(title);caption.setStyleSheet('font-size:10px;color:#91a2bb;');metrics.addWidget(caption,row,0)
            label=QLabel('—');label.setAccessibleName(title);label.setAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter);label.setFixedHeight(18);label.setStyleSheet('font-size:11px;font-weight:600;');metrics.addWidget(label,row,1);self.cards.append(label)
        controls.addWidget(self.metrics)
        controls.addStretch(1)
        speed_row=QHBoxLayout();speed_row.setSpacing(4);speed_row.addWidget(self.speed_button);speed_row.addWidget(self.speed_readout);controls.addLayout(speed_row)
        sidebar.setMinimumHeight(controls.minimumSize().height())
        self.panes=QSplitter(Qt.Orientation.Horizontal);self.panes.setChildrenCollapsible(False);self.panes.setHandleWidth(6)
        self.panes.setToolTip('Перетащите разделитель между маршрутом и графиками, чтобы изменить ширину колонок')
        route_panel=QWidget();route_panel.setMinimumWidth(440);route_layout=QVBoxLayout(route_panel);route_layout.setContentsMargins(0,0,0,0);route_layout.setSpacing(4)
        route_title=QLabel('Маршрут');route_title.setToolTip('● средняя RTT · голубая метка — текущая задержка · серая полоса — Min–Max');route_layout.addWidget(route_title)
        self.table=RouteTable(0,9);self.table.setHorizontalHeader(RouteHeader(self.table));self.table.setHorizontalHeaderLabels(['Hop','Count','IP','Name','Avg','Min','Cur','PL%','Latency, мс'])
        self.table.verticalHeader().setMinimumSectionSize(18);self.table.verticalHeader().setDefaultSectionSize(18)
        self.table.setMinimumHeight(70);self.table.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Expanding);self.table.setStyleSheet('QTableWidget{font-size:11px;} QHeaderView::section{padding:3px;}')
        self.table.verticalHeader().hide();self.table.setAlternatingRowColors(True);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows);self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for column,width in [(2,100),(3,100)]:
            self.table.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeMode.Interactive);self.table.setColumnWidth(column,width)
        self.table.horizontalHeader().setSectionResizeMode(8,QHeaderView.ResizeMode.Stretch)
        self.table.setItemDelegateForColumn(8,RangeDelegate(self.table));self.table.cellClicked.connect(self.select_hop);self.table.focused.connect(self.focus_hop);route_layout.addWidget(self.table,1)
        self.route_summary=RouteSummary(self.table);route_layout.addWidget(self.route_summary)
        charts_panel=QWidget();charts_panel.setMinimumWidth(230);charts_layout=QVBoxLayout(charts_panel);charts_layout.setContentsMargins(0,0,0,0);charts_layout.setSpacing(4)
        chart_header=QHBoxLayout();self.chart_title=CompactValue('Задержка');self.chart_title.setMinimumWidth(0);self.chart_title.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);chart_header.addWidget(self.chart_title,1)
        chart_header.addWidget(QLabel('Период'));self.period=QComboBox()
        for value,label in PERIODS:self.period.addItem(label,value)
        self.period.setCurrentIndex(1);self.period.currentIndexChanged.connect(self.refresh);chart_header.addWidget(self.period);charts_layout.addLayout(chart_header)
        self.jitter=Graph(True);self.graph=Graph();charts_layout.addWidget(self.jitter,2);charts_layout.addWidget(self.graph,5)
        self.panes.addWidget(route_panel);self.panes.addWidget(charts_panel);self.panes.setStretchFactor(0,1);self.panes.setStretchFactor(1,1);self.panes.setSizes([620,460]);columns.addWidget(self.panes,1);layout.addWidget(diagnostics,1)
        for graph in [self.jitter,self.graph]:graph.zoom.connect(self.zoom)
        self.logs=QPlainTextEdit();self.logs.setReadOnly(True);self.logs.setMaximumHeight(120);self.logs.hide()
        self.log_action=self.tools_menu.addAction('Журнал событий');self.log_action.setCheckable(True);self.log_action.toggled.connect(self.logs.setVisible)
        self.errors_action=self.tools_menu.addAction('Журнал ошибок');self.errors_action.triggered.connect(self.open_errors)
        self.errors_menu=QMenu(self);self.errors_action.setMenu(self.errors_menu)
        self.errors_menu.addAction('Интерфейс',lambda:self.open_errors(False));self.errors_menu.addAction('Фоновый движок',lambda:self.open_errors(True))
        self.errors_action.setToolTip('Технические ошибки приложения за 12 часов, без измерений и ключей. Сохраняется после закрытия.')
        self.tools_menu.addAction(self.about_action);self.tools_menu.addAction(self.update_action);self.tools_menu.addSeparator();self.export_action=self.tools_menu.addAction('Экспорт TXT');self.export_action.triggered.connect(self.export_session);layout.addWidget(self.logs)
        self.stream_panel=StreamPanel(self.network_report,network_report_factory=self.network_report_job);self.tabs.addTab(self.stream_panel,'Stream')
        self.tabs.currentChanged.connect(self.queue_balance)
        self.tabs.currentChanged.connect(self.refresh_visible)
        self.tabs.setTabToolTip(1,'Состояние трансляций OBS/vMix: битрейт, потери кадров, congestion, SRT/RTMP/NDI, отчёт для провайдера')
        self.timer=QTimer(self);self.timer.timeout.connect(self.refresh_visible);self.timer.start(500)
        self.interval.currentIndexChanged.connect(self.change_interval)
        self.family.currentIndexChanged.connect(self.change_server)
        if self.service and self.service.sid:
            config=self.service.status['config'];self.session_config=config;self.paused=self.service.status.get('paused',False);self.sid=self.service.sid;self.address.setText(config['targets'][0]['host'])
            self.interval.blockSignals(True);self.interval.setCurrentIndex(max(0,self.interval.findData(config['interval'])));self.interval.blockSignals(False)
            self.family.blockSignals(True);self.family.setCurrentIndex(max(0,self.family.findData(config['targets'][0]['family'])));self.family.blockSignals(False)
            self.selected_interface=config.get('interface')
            if self.selected_interface:
                self.nic.blockSignals(True);self.nic.addItem(self.selected_interface,self.selected_interface);self.nic.setCurrentIndex(1);self.nic.blockSignals(False)
            self.refresh()
        self.update_control()
        self.live.setToolTip('История ограничена 12 часами. Закрытие приложения останавливает сессию и очищает накопленные данные.'+(' Работает системная служба.' if self.service else ' Мониторинг удерживает компьютер от автоматического сна.'))
        if '--no-discovery' not in sys.argv:
            self.discovery=Discovery();self.discovery.selected=self.selected_interface;self.discovery.ready.connect(self.show_network);self.discovery.start()
        self.restore_preferences()
        self.layout_timer=QTimer(self);self.layout_timer.setSingleShot(True);self.layout_timer.timeout.connect(self.balance_blocks)
        self.log_action.toggled.connect(self.queue_balance)
        QTimer.singleShot(0,self.initial_layout)
    def restore_preferences(self):
        if self.sid:return
        try:
            data=json.loads((self.folder/'ui-preferences.json').read_text())
            for name in ('interval','period'):
                field=getattr(self,name);index=field.findData(data.get(name))
                if index>=0:field.blockSignals(True);field.setCurrentIndex(index);field.blockSignals(False)
            identity=server.interface_probes.validate_interface(data.get('interface'))
            if identity:
                self.selected_interface=identity;self.nic.blockSignals(True);self.nic.addItem(identity+' · ожидаем подключение',identity);self.nic.setCurrentIndex(self.nic.count()-1);self.nic.blockSignals(False)
            size=data.get('size')
            if isinstance(size,list) and len(size)==2 and all(type(v)==int for v in size):
                screen=QApplication.primaryScreen().availableGeometry();self.resize(max(900,min(size[0],screen.width())),max(640,min(size[1],screen.height())))
            self.timer.setInterval(int(self.interval.currentData()*1000))
            if self.discovery:self.discovery.selected=self.selected_interface
        except (OSError,ValueError,TypeError):pass
    def save_preferences(self):
        data=dict(interface=self.selected_interface,interval=self.interval.currentData(),period=self.period.currentData(),size=[self.width(),self.height()])
        try:export_transfer.atomic_write(self.folder/'ui-preferences.json',lambda output:output.write(json.dumps(data).encode()))
        except OSError as exc:error_log.error('Сохранение настроек окна',exc,key='preferences')
    def offer_update(self,release):
        if self.closing:return
        self.update_release=release;self.update_label.setText('Доступна LinkRadar '+release.version+' · установлена '+server.VERSION)
        self.update_label.setWordWrap(True);self.update_banner.show()
    def open_update(self):
        if self.update_release:
            url=update_checker.download_link(self.update_release.url)
            if not QDesktopServices.openUrl(QUrl(url)):QMessageBox.warning(self,'Обновление','Не удалось открыть браузер. Ссылка: '+url)
    def update_checked(self,manual):
        if manual and not self.closing:QMessageBox.information(self,'Обновления','Более новых версий LinkRadar нет.')
    def update_failed(self,message,manual):
        if manual and not self.closing:QMessageBox.information(self,'Обновления',message)
    def show_about(self):
        if self.about_dialog is None:
            self.about_dialog=AboutDialog(self);self.about_dialog.update_requested.connect(lambda:self.updater.check(True))
        self.about_dialog.show();self.about_dialog.raise_();self.about_dialog.activateWindow()
    def initial_layout(self):
        if not self.closing:
            width=sum(self.panes.sizes());route=int(width*.58);self.panes.setSizes([route,width-route])
            self.traffic_panel.refresh()
            self.size_capture(self.traffic_panel.expanded)
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'layout_timer'):self.layout_timer.start(0)
    def queue_balance(self,*args):self.layout_timer.start(0)
    def balance_blocks(self):
        # Hidden tab pages report empty splitter sizes; balancing then would raise the
        # window minimum without bound. The Network tab is balanced when shown again.
        if self.closing or not self.port_splitter.isVisible():return
        expanded=self.traffic_panel.expanded
        weights=list(BLOCK_WEIGHTS)+([1] if expanded else [])
        monitor=self.traffic_splitter.widget(1)
        available=self.port_splitter.sizes()[0]+monitor.height()+(self.traffic_splitter.sizes()[0] if expanded else 0)
        mins=[self.port_splitter.widget(0).minimumSizeHint().height(),monitor.minimumSizeHint().height()]+([self.traffic_panel.minimumSizeHint().height()] if expanded else [])
        self.setMinimumHeight(max(640,self.height()-available+sum(mins)))
        remaining=max(available,sum(mins));active=list(range(len(weights)));sizes=[0]*len(weights)
        while active:
            weight=sum(weights[i] for i in active)
            fixed=[i for i in active if remaining*weights[i]/weight<mins[i]]
            if not fixed:
                for i in active:sizes[i]=int(remaining*weights[i]/weight)
                sizes[active[-1]]+=remaining-sum(sizes[i] for i in active)
                break
            for i in fixed:sizes[i]=mins[i];remaining-=mins[i];active.remove(i)
        port,diagnostics,*capture=sizes
        total=sum(self.port_splitter.sizes());self.port_splitter.setSizes([port,max(1,total-port)])
        total=sum(self.traffic_splitter.sizes());capture=capture[0] if expanded else self.traffic_panel.compact_height()
        self.traffic_splitter.setSizes([capture,max(1,total-capture)])
    def size_capture(self,expanded):
        total=sum(self.traffic_splitter.sizes());height=max(self.capture_height,self.traffic_panel.compact_height()) if expanded else self.traffic_panel.compact_height()
        self.traffic_splitter.setSizes([height,max(1,total-height)])
        if hasattr(self,'layout_timer'):self.layout_timer.start(0)
    def remember_capture_height(self,position,index):
        height=self.traffic_splitter.sizes()[0]
        if height>=95:self.capture_height=height
    def io_completed(self,result):
        callback,value,error=result
        if self.closing:return
        if callback:callback(value,error)
        elif error:
            self.data_error=str(error);error_log.error('Запись данных сессии',error,key='record')
            self.live.setText('ОШИБКА ЗАПИСИ');self.live.setToolTip(self.data_error)
    def command(self,function,callback,on_error=None):
        self.state_revision+=1;self.command_busy=True
        for control in (self.button,self.nic,self.interval,self.family):control.setEnabled(False)
        def complete(value,error):
            self.state_revision+=1;self.command_busy=False
            for control in (self.button,self.nic,self.interval,self.family):control.setEnabled(True)
            if error:
                if on_error:on_error()
                error_log.error('Команда измерений',error);QMessageBox.warning(self,'Измерения',str(error))
            else:callback(value)
            self.update_control();self.refresh()
        try:self.io.submit(function,complete)
        except Exception as exc:complete(None,exc)
    def select_interface(self):
        previous=self.selected_interface
        identity=self.nic.currentData()
        if self.service and self.monitor.sid and identity!=previous:
            def selected(value):
                self.selected_interface=identity
                if self.session_config:self.session_config['interface']=identity
                self.apply_interface()
            def revert():
                self.nic.blockSignals(True);self.nic.setCurrentIndex(max(0,self.nic.findData(previous)));self.nic.blockSignals(False)
            self.command(lambda:self.monitor.set_interface(identity),selected,revert);return
        if self.monitor.sid and identity!=previous:
            try:
                self.monitor.set_interface(identity)
                if self.session_config:self.session_config['interface']=identity
            except Exception as exc:
                self.nic.blockSignals(True);self.nic.setCurrentIndex(max(0,self.nic.findData(previous)));self.nic.blockSignals(False)
                QMessageBox.warning(self,'Адаптер измерений',str(exc));return
        self.selected_interface=identity
        self.apply_interface()
    def apply_interface(self):
        self.state_revision+=1
        if self.speed_worker and self.speed_worker.isRunning():
            self.speed_worker.stop.set();self.speed_button.set_progress(self.speed_button.progress,'Отмена…');self.speed_button.setEnabled(False)
        if self.discovery:self.discovery.selected=self.selected_interface
        if hasattr(self,'traffic_panel'):self.traffic_panel.set_source({},{})
        for value in self.network_fields.values():value.setText('—')
        self.lldp_status.setText('Собираем сетевые данные…')
        self.selected_node=None
        self.port_panel.lldp({})
        if self.monitor.sid:self.table.setRowCount(0);self.refresh()
    def copy_network(self):
        values={key:field.text() for key,field in self.network_fields.items()}
        query='—';host,port=values['switch'],values['port']
        try:
            resolved=self.port_panel.resolved
            matches=(self.port_panel.auto and self.port_panel.lldp_key==(host,port)) or (
                resolved and resolved[0].lower()==host.lower() and graphene_port.port_identity(resolved[1])==graphene_port.port_identity(port))
            query=graphene_port.resource_query(*(resolved if resolved and matches else (host,port)),canonical=bool(resolved and matches))
        except graphene_port.SourceError:pass
        text='\n'.join([f'Switch: {host}',f'Port: {port}',f"VLAN: {values['vlan']}",f'Graphene: {query}',
                        f"IP: {values['ip']}",f"MASK: {values['mask']}",f"GATEWAY: {values['gateway']}",f"MAC: {values['mac']}"])
        QApplication.clipboard().setText(text)
        self.copy_button.setText('Copied');QTimer.singleShot(1200,lambda:self.copy_button.setText('Copy'))
    def show_network(self,data):
        if self.closing:return
        rows=data['interfaces'];selected=self.selected_interface
        self.interface_names.update({row['id']:row['name'] for row in rows})
        items=[('Автоматически',None)]+[(row['name']+' · '+(row.get('ip') or 'без IP'),row['id']) for row in rows]
        if selected is not None and not any(row['id']==selected for row in rows):
            items.append((self.interface_names.get(selected,str(selected))+' · нет подключения',selected))
        if [(self.nic.itemText(i),self.nic.itemData(i)) for i in range(self.nic.count())]!=items:
            self.nic.blockSignals(True);self.nic.clear()
            for label,identity in items:self.nic.addItem(label,identity)
            self.nic.setCurrentIndex(max(0,self.nic.findData(selected)));self.nic.blockSignals(False)
        if self.discovery:self.discovery.selected=selected
        # Discovery signals already queued before a manual selection may belong
        # to the previous adapter. Never offer that adapter's LLDP for capture.
        local=data['local'];neighbor=data.get('neighbor') or {}
        if selected is not None and local.get('id')!=selected:
            local={};neighbor={};data={**data,'local':local,'neighbor':None,'lldp_status':'Ожидаем выбранный адаптер и LLDP…'}
        if data!=self.network_snapshot:
            if not self.sid or self.record('network',str(time.time()),data):self.network_snapshot=data
        gateway=local.get('gateway')
        if isinstance(gateway,list):gateway=gateway[0] if gateway else None
        self.address.set_gateway(str(gateway) if gateway else None)
        self.port_panel.lldp(neighbor)
        self.traffic_panel.set_source(local,neighbor)
        for key,value in self.network_fields.items():
            raw=neighbor.get(key) if key in ('switch','port','vlan') else local.get(key)
            text=', '.join(raw) if isinstance(raw,list) else str(raw) if raw is not None else '—'
            value.setText(text or '—');value.setToolTip(text if key!='vlan' else 'PVID объявлен коммутатором через LLDP; не является списком VLAN транка')
            if key=='mac':value.setToolTip(value.text()+'\n'+local.get('mac_source','MAC выбранного интерфейса'))
        self.lldp_status.setText((local.get('name','')+' · ' if local else '')+data['lldp_status'])
        self.lldp_status.setToolTip('LLDP слушается автоматически; обновление конфигурации каждые 10 секунд. Switch/port/VLAN появятся только из реального объявления.')
        self.lldp_status.setTextFormat(Qt.TextFormat.PlainText)
    def measure_speed(self):
        if self.speed_worker and self.speed_worker.isRunning():
            self.speed_worker.stop.set();self.speed_button.set_progress(self.speed_button.progress,'Отмена…');self.speed_button.setDisabled(True);return
        if self.speed_worker:self.speed_worker.deleteLater()
        self.speed_worker=SpeedWorker(self.selected_interface);self.speed_worker.progress.connect(self.speed_progress);self.speed_worker.ready.connect(self.speed_result);self.speed_worker.failed.connect(self.speed_failure)
        self.speed_button.set_progress(0,'0%');self.speed_active=True;self.load_windows.append([time.time(),None]);self.speed_readout.setText('↓ — ↑ — Мбит/с');self.speed_readout.setToolTip('HTTP-тест: выбранный адаптер, а в Auto — маршрут ОС. DNS — резолвер ОС. Создаёт нагрузку на канал.')
        for graph in [self.graph,self.jitter]:graph.load_windows=self.load_windows;graph.invalidate_chart();graph.update()
        self.note('Начат тест скорости: канал под нагрузкой (синяя область графика)')
        self.speed_worker.start()
    def speed_progress(self,data):
        if self.closing:return
        phase=data['phase']
        percent=max(self.speed_button.progress,min(99,int(data.get('total_progress',0)*100)))
        caption='Подготовка' if phase=='config' else ('↓ Загрузка' if phase=='download' else '↑ Отдача')+(' · прогрев' if data.get('warmup') else '')
        self.speed_button.set_progress(percent,f'{percent}%');self.speed_button.setAccessibleDescription(f'{caption}. Общий прогресс теста скорости: {percent}%')
        if phase!='config':self.speed_readout.setText(('↓ ' if phase=='download' else '↑ ')+fmt(data['mbps'])+' Мбит/с')
    def finish_speed(self):
        self.speed_active=False
        if self.load_windows and self.load_windows[-1][1] is None:self.load_windows[-1][1]=time.time()
        for graph in [self.graph,self.jitter]:graph.invalidate_chart();graph.update()
        self.speed_button.setEnabled(True)
        self.note('Тест скорости завершён; нагрузка снята')
    def speed_result(self,result):
        if self.closing:return
        self.record('speed',str(time.time()),result)
        self.finish_speed();self.speed_button.set_progress(100,'Тест')
        values=[]
        for key,arrow in [('download','↓'),('upload','↑')]:
            values.append(arrow+' '+(fmt(result[key]['mbps']) if result[key] else 'нет данных'))
        directions=[result['download'],result['upload']]
        if not any(directions):note=' · измерение не удалось'
        elif any(not r or r['limited'] or r['partial'] for r in directions):note=' · короткая/частичная выборка'
        elif any(not r.get('stable') for r in directions):note=' · скорость колеблется'
        else:note=' · устойчивый замер'
        self.speed_readout.setText(' '.join(values)+' Мбит/с')
        self.speed_readout.setToolTip('Яндекс CDN'+note+'\n'+'\n'.join(result['errors'].values())+'\n'+'\n'.join(key+': '+str(r['seconds'])+' с, P10–P90 '+str(r.get('p10'))+'–'+str(r.get('p90'))+' Мбит/с' for key in ['download','upload'] if (r:=result[key]))+'\nАдаптер: '+str(self.speed_worker.interface or 'маршрут ОС')+'. DNS — резолвер ОС. HTTP-скорость до CDN; не измеряет качество RTMP.')
    def speed_failure(self,message):
        if self.closing:return
        self.finish_speed();caption='Отменено' if 'отменено' in message.lower() else 'Ошибка'
        self.speed_button.set_progress(self.speed_button.progress,caption);self.speed_readout.setText(message);self.speed_readout.setToolTip(message)
    def toggle(self):
        if self.command_busy:return
        self.state_revision+=1
        try:
            if self.service and 'pause' not in self.service.status.get('capabilities',[]):
                raise RuntimeError('Фоновый движок предыдущей версии ещё работает. После замены приложения перезагрузите компьютер, чтобы загрузить новый движок с паузой.')
            host,port=endpoint(self.address.text());seconds=self.interval.currentData()
            config=server.validate_config({'interface':self.selected_interface,'name':host,'interval':seconds,'timeout':min(1,seconds),'duration_minutes':1440,'continuous':True,'route_interval':seconds,
                'targets':[{'host':host,'mode':'icmp','family':self.family.currentData(),'required':True}]})
            if self.monitor.sid:
                if self.same_target(config):
                    if self.service:
                        def paused(value):self.paused=value
                        sid=self.monitor.sid;self.command(lambda:self.monitor.pause(sid),paused)
                    else:self.paused=self.monitor.pause(self.monitor.sid);self.update_control();self.refresh()
                    return
                self.pending_config=config;self.pending_restart=True
                if self.service:self.command(self.monitor.stop,lambda value:None)
                else:self.monitor.stop()
                self.button.setDisabled(True);self.live.setText('СМЕНА СЕРВЕРА…');return
            self.start_session(config)
        except Exception as exc:
            if not isinstance(exc,ValueError):error_log.error('Запуск проверки',exc)
            QMessageBox.warning(self,'Не удалось запустить',str(exc))
    def same_target(self,config):
        if not self.session_config:return False
        previous=self.session_config['targets'][0];current=config['targets'][0]
        return previous['host'].lower()==current['host'].lower() and previous['family']==current['family']
    def start_session(self,config):
        if self.service:
            self.command(lambda:self.monitor.start(config),lambda sid:self.started_session(config,sid));return
        self.started_session(config,self.monitor.start(config))
    def started_session(self,config,sid):
        self.sid=sid;self.session_config=config;self.paused=False;self.selected_node=None;self.snapshot={}
        self.log_lines=[];self.event_labels={};self.logs.clear();self.port_seen={};self.port_pending={};self.port_prune_at=0;self.data_error=None;
        if self.io:self.io.data_error=None
        self.timer.setInterval(int(config['interval']*1000))
        self.table.setRowCount(0)
        for graph in (self.graph,self.jitter):graph.set_data([],time.time(),self.period.currentData())
        for card in self.cards:card.setText('—')
        self.update_control();self.refresh()
        self.record('network','initial',self.network_snapshot)
        if self.port_panel.snapshot:self.record_port(self.port_panel.snapshot)
    def update_control(self):
        same=False
        try:
            host,_=endpoint(self.address.text());same=self.same_target({'targets':[{'host':host,'family':self.family.currentData()}]})
        except ValueError:pass
        running=bool(self.monitor.sid) and not self.paused and same
        self.button.setIcon(capture_icon('pause' if running else 'play'))
        label='Пауза' if running else 'Пуск'
        self.button.setAccessibleName(label);self.button.setToolTip(label+(' · новая история для другого сервера' if self.monitor.sid and not same else ' · история сохранится'))
        self.live.setText('СМЕНА СЕРВЕРА…' if self.pending_restart else 'ПАУЗА' if self.monitor.sid and self.paused else ('● ФОН' if self.service else '● LIVE') if self.monitor.sid else 'ГОТОВО')
    def change_interval(self):
        previous=(self.session_config or {}).get('interval',self.interval.currentData())
        def restore():
            self.interval.blockSignals(True);self.interval.setCurrentIndex(max(0,self.interval.findData(previous)));self.interval.blockSignals(False);self.timer.setInterval(int(previous*1000))
        self.state_revision+=1
        self.timer.setInterval(int(self.interval.currentData()*1000))
        if self.monitor.sid:
            try:
                if self.service:
                    seconds=self.interval.currentData()
                    def changed(value):
                        if self.session_config:self.session_config['interval']=seconds
                        self.timer.setInterval(int(seconds*1000))
                    self.command(lambda:self.monitor.set_interval(seconds),changed,restore);return
                self.monitor.set_interval(self.interval.currentData())
                if self.session_config:self.session_config['interval']=self.interval.currentData()
            except Exception as exc:
                restore();QMessageBox.warning(self,'Интервал опроса',str(exc))
    def change_server(self,*args):
        if hasattr(self,'button'):self.update_control()
    def record_failed(self,exc):
        self.data_error=str(exc);error_log.error('Передача данных сессии',exc,key='record')
        self.live.setText('ОШИБКА ЗАПИСИ');self.live.setToolTip(self.data_error)
    def record(self,kind,key,data,rows=None,done=None):
        """Acknowledge saved data before advancing the Graphene deduplication cache."""
        if self.closing or not self.sid:return False
        rows=rows if rows is not None else [[self.sid,kind,str(key),time.time(),data]]
        def completed(value,error):
            if error:self.record_failed(error)
            if done:done(error is None)
        try:
            if self.store:self.io.submit(lambda:session_export.record_many(self.store,rows),completed,data=True)
            else:self.io.submit(lambda:self.service.record(rows),completed,data=True)
            return True
        except Exception as exc:completed(None,exc);return False
    def note(self,message):
        if not self.sid:return
        try:
            sid=self.sid
            self.io.submit((lambda:self.store.event(sid,'','info',message)) if self.store else (lambda:self.service.event(sid,message)),data=True)
        except Exception as exc:self.record_failed(exc)
    def record_port(self,data):
        if self.closing or not self.sid:return
        now=self.store.clock() if self.store else time.time();cutoff=now-server.RETENTION_SECONDS
        if now>=self.port_prune_at:
            # Expire only points outside session retention; clearing the entire
            # cache at 50k caused the next poll to rewrite acknowledged history.
            self.port_seen={key:value for key,value in self.port_seen.items() if float(key.rsplit('/',1)[1])>=cutoff}
            self.port_prune_at=now+60
        self.record('graphene_source',data['host']+'/'+data['port'],{k:v for k,v in data.items() if k!='series'})
        rows=[];pending={};sid=self.sid
        for sensor,points in data['series'].items():
            for ts,value in points:
                if ts<cutoff:continue
                key=f"{data['host']}/{data['port']}/{sensor}/{ts}"
                if (key in self.port_seen and self.port_seen[key]==value) or (key in self.port_pending and self.port_pending[key]==value):continue
                pending[key]=value
                rows.append([sid,'graphene',key,ts,{'host':data['host'],'port':data['port'],'sensor':sensor,'value':value}])
        def saved(success):
            if self.sid!=sid:return
            for key,value in pending.items():
                if self.port_pending.get(key)==value:self.port_pending.pop(key,None)
            if success:
                self.port_seen.update(pending)
        if rows:
            self.port_pending.update(pending);self.record('graphene',None,None,rows,done=saved)
    def export_session(self):
        if not self.sid:QMessageBox.information(self,'Экспорт','Сначала запустите проверку');return
        path,_=QFileDialog.getSaveFileName(self,'Экспорт полной сессии',f'LinkRadar-{time.strftime("%Y%m%d-%H%M%S")}.txt','Текст (*.txt)')
        if not path:return
        if self.export_worker and self.export_worker.isRunning():return
        names={h['ip']:h.get('name') for t in self.snapshot.get('targets',[]) for h in t.get('hops',[]) if h.get('ip')}
        if not self.service:names.update(self.monitor.ptr_names)
        try:barrier=self.io.barrier() if self.io else None
        except queue.Full:
            QMessageBox.warning(self,'Экспорт','Очередь записи занята. Дождитесь завершения записи и повторите экспорт.');return
        except RuntimeError as exc:
            QMessageBox.warning(self,'Экспорт',str(exc));return
        if self.export_worker:self.export_worker.deleteLater()
        self.export_worker=ExportWorker(self.store,self.service,self.sid,path,names,barrier,lambda:self.data_error or (self.io.data_error if self.io else None))
        self.export_action.setEnabled(False);self.export_action.setText('Сохраняем TXT…')
        self.export_worker.failed.connect(self.export_failed)
        self.export_worker.finished.connect(self.export_finished);self.export_worker.start()
    def export_failed(self,message):
        if not self.closing:QMessageBox.warning(self,'Ошибка экспорта',message)
    def export_finished(self):
        self.export_action.setEnabled(True);self.export_action.setText('Экспорт TXT')
    def open_errors(self,service=False):
        path=Path(self.service.folder)/'errors-service.log' if service and self.service else error_log.path()
        if not path or not path.is_file():QMessageBox.information(self,'Журнал ошибок','За последние 12 часов ошибок не записано.');return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
    def update_logs(self,events):
        # A new event or TTL rollover must not reformat every surviving row.
        # Cache only this snapshot's immutable display keys; corrected content
        # gets a new label and expired entries are released immediately.
        lines,self.event_labels=format_events(reversed(events),getattr(self,'event_labels',{}),format_timestamp)
        self.log_lines=update_lines(self.logs,self.log_lines,lines)
    def prune_history(self):
        if self.closing:return
        if self.store and self.store.clock()>self.store.next_expiry and not self.prune_pending:
            self.prune_pending=True
            def pruned(value,error):
                self.prune_pending=False
                if error:self.record_failed(error)
            try:self.io.submit(self.store.prune,pruned,data=True)
            except Exception as exc:pruned(None,exc)
    def refresh(self,*args):
        self.prune_history()
        if not self.sid or self.closing or self.command_busy or (self.worker and self.worker.isRunning()):return
        if self.worker:self.worker.deleteLater()
        self.worker=Snapshot(self.monitor,self.sid,self.period.currentData(),self.selected_node,self.snapshot.get('events_cursor',0),self.state_revision);self.worker.ready.connect(self.render);self.worker.failed.connect(self.failure);self.worker.start()
    def refresh_visible(self,*args):
        # Probes and retention run independently. Hidden views need no full
        # 12-hour read/repaint; a requested server change must still finish.
        if self.tabs.currentIndex()==0 or self.pending_restart:self.refresh()
        else:self.prune_history()
    def failure(self,message):
        self.live.setText('Ошибка чтения данных');self.live.setToolTip(message)
    def render(self,snapshot):
        if self.closing or snapshot.get('_ui_revision',self.state_revision)!=self.state_revision or snapshot.get('_ui_period',self.period.currentData())!=self.period.currentData():return
        if snapshot.get('session') and snapshot['session']['id']!=self.sid:return
        if snapshot.get('session') and snapshot['session'].get('config',{}).get('interface')!=self.selected_interface:return
        update_events=True
        if snapshot.get('compact'):
            same_session=(self.snapshot.get('session') or {}).get('id')==self.sid
            old=self.snapshot.get('events',[]) if same_session else []
            cutoff=snapshot.get('events_cutoff',0)
            if same_session and not snapshot['events'] and (not old or old[-1]['ts']>=cutoff):
                # Quiet probes do not change the journal. Keep its contents,
                # scroll/selection and avoid formatting 12h of events again.
                snapshot['events']=old;update_events=False
            else:
                events={e.get('cursor'):e for e in old+snapshot['events'] if e['ts']>=cutoff}
                snapshot['events']=sorted(events.values(),key=lambda e:(e['ts'],e.get('cursor',0)),reverse=True)
        self.snapshot=snapshot;active=bool(snapshot.get('active_id'));
        if self.service:self.monitor.sid=snapshot.get('active_id')
        if not active and self.pending_restart and not self.command_busy:
            config=self.pending_config;self.pending_restart=False;self.pending_config=None;self.start_session(config);return
        self.paused=snapshot.get('paused',False);self.button.setEnabled(not self.command_busy and (not active or not self.monitor.stop_event.is_set()));self.update_control()
        targets=snapshot.get('targets',[])
        if not targets:return
        ping=next(t for t in targets if t['mode']=='icmp');s=ping['window_stats']
        last=ping.get('last') or {}
        self.address.setToolTip('Сервер: '+ping['host']+'\nИзмеряемый IP: '+str(last.get('ip') or 'ожидаем ответ')+'\nАдаптер: '+str((self.session_config or {}).get('interface') or 'маршрут ОС')+'\nICMP RTT; маршрут — отдельные зонды с TTL/Hop Limit'+('\n'+last['detail'] if last.get('detail') else ''))
        self.live.setToolTip(last.get('detail',''))
        if active and not self.paused and last.get('status')=='error':self.live.setText('ОШИБКА ПРОВЕРКИ')
        if active and not self.paused and last.get('status')=='unreachable':self.live.setText('ЦЕЛЬ НЕДОСТУПНА')
        age=max(0,time.time()-last['ts']) if last.get('ts') else None
        if active and not self.paused and (age is None or age>max(3,(self.session_config or {}).get('interval',1)*3+2)):
            self.live.setText('НЕТ СВЕЖИХ ПРОБ' if age is not None else 'ОЖИДАЕМ ПРОБУ')
            self.live.setToolTip('Последняя проба '+str(int(age))+' с назад; это разрыв сбора, а не доказанная потеря пакетов.' if age is not None else 'Сбор запущен; измерения ещё не получены.')
        jit=(ping.get('window_last') or {}).get('jitter')
        self.cards[0].setText(fmt(jit)+' мс');self.cards[1].setText(fmt(s['loss'],2)+' %')
        self.route_summary.stats=s;self.route_summary.current=(ping.get('window_last') or {}).get('latency') if (ping.get('window_last') or {}).get('status')=='ok' else None;self.route_summary.update()
        hops=ping.get('hops',[]);scale=route_scale(hops)
        route=ping.get('route');route_age=max(0,time.time()-route['ts']) if route else None
        cycle=route.get('data',{}).get('cycle_seconds') if route else None
        self.table.setToolTip(('Последний цикл: '+fmt(route_age,0)+' с назад · длительность '+fmt(cycle,1)+' с\n' if route else 'Маршрут ещё собирается\n')+'TTL-зонды выполняются последовательно. Интервал ping не гарантирует такую же частоту полного маршрута. RTT — до узла; промежуточный ICMP timeout не доказывает потерю транзитного трафика.')
        self.table.horizontalHeader().scale=scale;self.table.horizontalHeader().viewport().update()
        self.table.setRowCount(len(hops))
        for row,h in enumerate(hops):
            stats=h['stats'];last=h['last'];values=[str(h['hop']),str(stats['count']),h['ip'] or '—',h.get('name') or '—',fmt(stats['avg']),fmt(stats['min']),fmt(last.get('latency')),fmt(stats['loss']), '']
            for col,value in enumerate(values):
                item=QTableWidgetItem(value)
                if col==2:item.setToolTip((h.get('name') or '')+'\n'+str(h['ip'] or '—')+'\nПроб: '+str(stats['count']))
                if col==8:
                    item.setData(Qt.ItemDataRole.UserRole,(stats['min'],stats['avg'],stats['max'],last.get('latency'),scale))
                    item.setToolTip('Hop '+str(h['hop'])+' · средняя '+fmt(stats['avg'])+' мс · текущая '+fmt(last.get('latency'))+' мс\nMin '+fmt(stats['min'])+' мс · Max '+fmt(stats['max'])+' мс'+('\nMax выходит за шкалу (стрелка справа)' if (stats['max'] or 0)>scale else '')+'\nСредний интервал проб узла: '+fmt(h.get('interval'),2)+' с\nRTT до узла, не задержка отдельного участка. Ответы ICMP могут иметь низкий приоритет.')
                self.table.setItem(row,col,item)
            if self.selected_node==h['node']:self.table.selectRow(row)
        hop=next((h for h in hops if h['node']==self.selected_node),None)
        points=hop['graph'] if hop else ping['graph'];stats=hop['stats'] if hop else s
        if hop and snapshot.get('compact') and snapshot.get('selected_node')!=self.selected_node:points=[]
        self.chart_title.setText((('Узел '+str(hop['hop'])) if hop else ping['host'])+' · задержка')
        self.cards[2].setText(fmt(s['avg'])+' мс');self.cards[3].setText(fmt(s['max'])+' мс')
        for card in self.cards:card.setToolTip('Конечный сервер '+ping['host']+' · выбранный период; ICMP не измеряет jitter видеопотока')
        end=snapshot['session']['analysis_end']
        for graph in [self.jitter,self.graph]:graph.set_data(points,end,self.period.currentData())
        if update_events:self.update_logs(snapshot['events'])
        if self.data_error:self.live.setText('ОШИБКА ЗАПИСИ');self.live.setToolTip(self.data_error)
    def network_report(self):
        """Network context for the Stream report, read at the time of the request."""
        return self.network_report_job()()
    def network_report_job(self):
        """Capture GUI labels now; the returned reader only accesses the engine."""
        values={key:field.text() for key,field in self.network_fields.items()}
        base=[f"Адаптер: {self.nic.currentText()} · IP {values['ip']} · шлюз {values['gateway']} · DNS {values['dns']}",
               f"Коммутатор: {values['switch']} · порт {values['port']} · VLAN {values['vlan']}"]
        monitor,sid,period,label=self.monitor,self.sid,self.period.currentData(),self.period.currentText()
        data_error=self.data_error;speed=self.speed_readout.text().strip()
        def collect():
            # The tab may be hidden. Read current data, without touching widgets.
            snapshot=monitor.snapshot(sid,period,compact=True) if sid else {}
            lines=list(base);ping=next((t for t in snapshot.get('targets',[]) if t.get('mode')=='icmp'),None)
            if ping:
                s=ping['window_stats'];jitter=(ping.get('window_last') or {}).get('jitter')
                lines.append(f"ICMP до {ping['host']} за {label}: средн. {fmt(s['avg'])} мс · макс. {fmt(s['max'])} мс · без ответа {fmt(s['loss'],2)}% · jitter {fmt(jitter)} мс")
                hops=[h for h in ping.get('hops',[]) if h.get('ip')]
                if hops:lines.append('Маршрут: '+' → '.join(f"{h['ip']} ({fmt(h['stats']['avg'])} мс)" for h in hops[:20]))
            else:lines.append('ICMP-мониторинг не запущен')
            if data_error:lines.append('ВНИМАНИЕ: часть данных сессии не удалось записать.')
            if any(ch.isdigit() for ch in speed):lines.append('Тест скорости (HTTP до CDN Яндекса): '+speed)
            return lines
        return collect
    def select_hop(self,row,column):
        hops=self.snapshot.get('targets',[{}])[0].get('hops',[])
        if row<len(hops):self.selected_node=None if self.selected_node==hops[row]['node'] else hops[row]['node'];self.render(self.snapshot);self.refresh()
    def focus_hop(self,row):
        hops=self.snapshot.get('targets',[{}])[0].get('hops',[])
        if 0<=row<len(hops) and self.selected_node!=hops[row]['node']:
            self.selected_node=hops[row]['node'];self.render(self.snapshot);self.refresh()
    def zoom(self,direction):self.period.setCurrentIndex(max(0,min(self.period.count()-1,self.period.currentIndex()+direction)))
    def closeEvent(self,event):
        if self.closed_clean:event.accept();return
        if not self.closing:
            self.save_preferences();self.closing=True;self.timer.stop();self.layout_timer.stop()
            if not self.service:self.monitor.stop()
            self.updater.shutdown()
            self.port_panel.closing=True;self.port_panel.timer.stop();self.port_panel.clock.stop()
            self.traffic_panel.closing=True;self.traffic_panel.timer.stop();self.traffic_panel.engine.stop()
            self.stream_panel.closing=True;self.stream_panel.timer.stop();self.stream_panel.monitor.pause()
            if self.discovery:self.discovery.stop.set()
            if self.speed_worker:self.speed_worker.stop.set()
            if self.export_worker:self.export_worker.stop.set()
            if self.worker:self.worker.stop.set()
            if self.io:self.io.shutdown()
            self.close_started=time.monotonic()
        # Tiny isolated windows can finish immediately. Long jobs/cleanup use a
        # timer, so macOS keeps painting the window and never destroys live QThreads.
        busy=self.close_jobs()
        small=self.store and self.store.path.stat().st_size<2*1024*1024
        if not busy and small:
            try:self.monitor.clear()
            except Exception as exc:self.close_failure(exc);event.ignore();return
            self.complete_close();event.accept();return
        event.ignore();self.setEnabled(False);self.live.setText('ЗАВЕРШАЕМ…')
        QTimer.singleShot(50,self.finish_close)
    def close_jobs(self):
        threads=[self.worker,self.discovery,self.speed_worker,self.export_worker,self.io,self.port_panel.worker,self.stream_panel.report_worker]
        return (self.updater.busy() or any(t and t.isRunning() for t in threads)
            or (not self.service and any(t.is_alive() for t in self.monitor.workers))
            or bool(self.stream_panel.monitor.thread and self.stream_panel.monitor.thread.is_alive())
            or bool(self.traffic_panel.engine.worker and self.traffic_panel.engine.worker.is_alive()))
    def finish_close(self):
        if self.closed_clean or not self.closing:return
        if self.close_jobs():
            if time.monotonic()-self.close_started>20:self.live.setText('ЖДЁМ ЗАВЕРШЕНИЯ…')
            QTimer.singleShot(100,self.finish_close);return
        if self.cleanup_worker is None:
            self.cleanup_worker=ui_tasks.Call(self.monitor.clear);self.cleanup_worker.start()
        if self.cleanup_worker.isRunning():QTimer.singleShot(50,self.finish_close);return
        if self.cleanup_worker.error:self.close_failure(self.cleanup_worker.error);return
        self.complete_close();self.close()
    def close_failure(self,exc):
        error_log.error('Очистка данных при закрытии',exc)
        self.cleanup_worker=None;self.closing=False;self.setEnabled(True)
        # A refused cleanup preserves data. Reopening is safer than continuing
        # with discovery/capture half stopped; closing can be retried.
        self.live.setText('НЕ УДАЛОСЬ ЗАВЕРШИТЬ')
        QMessageBox.warning(self,'Не удалось очистить данные',str(exc)+'\nДанные сохранены. Повторите закрытие.')
    def complete_close(self):
        self.port_panel.shutdown();self.traffic_panel.shutdown();self.stream_panel.shutdown()
        self.sid=None;self.snapshot={};self.log_lines=[];self.port_seen={};self.port_pending={};self.network_snapshot=None
        self.logs.clear();self.event_labels={};self.port_panel.clear();self.load_windows.clear()
        for graph in (self.graph,self.jitter):graph.set_data([],time.time(),self.period.currentData())
        self.table.setRowCount(0)
        if self.about_dialog is not None:self.about_dialog.close()
        self.closed_clean=True

def qt_message(kind,context,message):
    # Only Qt's own critical/fatal messages; warnings are frequent and not defects.
    if kind in (QtMsgType.QtCriticalMsg,QtMsgType.QtFatalMsg):error_log.logger.error('Qt: %s',message)

LEGACY_SERVICE=('Работает фоновая служба прежней версии: она хранит данные в папке пользователя, что небезопасно. '
    'Запустите Install background.cmd из нового комплекта (служба будет перенесена) или Remove background.cmd.')

def engine_service(argv):
    """Headless engine: --data-dir (macOS launchd, development) or --user-sid (Windows SYSTEM task)."""
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--engine-service',action='store_true')
    where=parser.add_mutually_exclusive_group(required=True);where.add_argument('--data-dir');where.add_argument('--user-sid')
    args=parser.parse_args(argv)
    # The Windows task names only the user; the engine derives its own protected folder.
    if args.user_sid:background_engine.run(service_paths.service_folder(args.user_sid),user_sid=args.user_sid)
    else:background_engine.run(args.data_dir)

def service_location(folder,argv,platform=sys.platform):
    """Folder with the engine's service.key. Windows: per-user folder in ProgramData."""
    if platform!='win32' or '--data-dir' in argv or '--smoke-test' in argv:return folder
    return service_paths.service_folder(service_paths.current_sid())

def legacy_service(folder,service_folder):
    """An engine of 2.8.1 or older still serving from the user's own folder (Windows)."""
    if (service_folder is not False and Path(service_folder)==Path(folder)) or not (Path(folder)/'service.key').exists():return False
    return background_engine.connect(folder) is not None

APP_ID='VCS.NetworkWatch.Desktop'

def app_icon():
    """Multi-size .ico on Windows (sharp taskbar/Alt+Tab), PNG elsewhere."""
    return QIcon(str(ROOT/('branding/NetworkWatch.ico' if sys.platform=='win32' else 'branding/network-watch-logo.png')))

def set_windows_app_id():
    """Own taskbar group and icon instead of python.exe/pythonw.exe when run from source."""
    if sys.platform!='win32':return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError,OSError):pass

def option_value(argv,flag):
    if flag not in argv:return None
    index=argv.index(flag)+1
    if index>=len(argv) or argv[index].startswith('--'):raise ValueError('Не указано значение '+flag)
    return argv[index]


def main():
    if '--engine-service' in sys.argv:
        engine_service(sys.argv[1:]);return
    # Must precede any window: Windows binds the taskbar group when the first window appears.
    set_windows_app_id()
    app=QApplication(sys.argv);app.setStyle('Fusion');app.setStyleSheet(STYLE);app.setApplicationName(NAME);app.setApplicationVersion(server.VERSION);app.setOrganizationName('VCS Tech');app.setWindowIcon(app_icon())
    folder=Path(os.environ.get('LOCALAPPDATA',str(Path.home()))) / 'VCS Network Watch' if sys.platform=='win32' else Path.home()/'Library/Application Support/VCS Network Watch'
    if '--data-dir' in sys.argv:
        try:folder=Path(option_value(sys.argv,'--data-dir'))
        except ValueError as exc:QMessageBox.warning(None,NAME,str(exc));return
    elif '--smoke-test' in sys.argv:
        import tempfile
        folder=Path(tempfile.mkdtemp(prefix='network-watch-desktop-'))
    service_paths.private_profile(folder)
    error_log.setup(folder);qInstallMessageHandler(qt_message)
    lock=os.fdopen(os.open(folder/'engine.lock',os.O_RDWR|os.O_CREAT,0o600),'r+b')
    try:
        if sys.platform=='win32':
            import msvcrt
            lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        QMessageBox.information(None,NAME,'Приложение уже запущено.');return
    capture_status,capture_message=mac_capture.prepare()
    status,message=mac_background.prepare(folder)
    message='\n\n'.join(part for part in (capture_message,message) if part)
    if status=='unavailable':
        QMessageBox.warning(None,'Фоновый движок',message);lock.close();return
    try:service_folder=service_location(folder,sys.argv)
    except (OSError,ValueError) as exc:
        error_log.error('Папка фоновой службы',exc);service_folder=False
    if legacy_service(folder,service_folder):
        QMessageBox.warning(None,'Фоновый движок',LEGACY_SERVICE);lock.close();return
    try:window=Window(folder,require_service=status=='ready',service_folder=service_folder)
    except RuntimeError as exc:
        QMessageBox.warning(None,'Фоновый движок',str(exc));lock.close();return
    if '--smoke-test' in sys.argv:
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating,True);window.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus,True)
    window.show()
    if '--smoke-test' not in sys.argv:window.updater.start()
    window.setup_notice.setText(message);window.setup_notice.setVisible(bool(message))
    if '--smoke-test' in sys.argv:
        if not window.monitor.sid:
            if '--smoke-interface' in sys.argv:
                identity=server.interface_probes.validate_interface(sys.argv[sys.argv.index('--smoke-interface')+1])
                index=window.nic.findData(identity)
                if index<0:window.nic.addItem(identity,identity);index=window.nic.count()-1
                window.nic.setCurrentIndex(index)
            window.address.setText(sys.argv[sys.argv.index('--smoke-target')+1] if '--smoke-target' in sys.argv else '127.0.0.1')
            window.toggle()
        if '--smoke-port' in sys.argv:
            window.port_panel.manual();window.port_panel.period.setCurrentIndex(1);window.port_panel.host.setText('switch-a.example.test');window.port_panel.port.setText('Gi1/0/17');window.port_panel.connect_source()
        def capture():
            preview=Path(os.environ.get('NETWORK_WATCH_PREVIEW','/private/tmp/network-watch-native-preview.png'))
            window.grab().save(str(preview))
            if '--smoke-report' in sys.argv and window.monitor.sid:
                snapshot=window.monitor.snapshot(window.sid,window.period.currentData(),compact=True)
                report={'version':server.VERSION,'profile':str(folder),'compact':snapshot.get('compact'),'ui_status':window.live.text(),'cards':[card.text() for card in window.cards],'interface':window.selected_interface,'targets':[
                    {key:target.get(key) for key in ('host','window_stats','window_last','last','hops','route')}
                    for target in snapshot.get('targets',[])]}
                Path(sys.argv[sys.argv.index('--smoke-report')+1]).write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
            print('Native window and monitoring:',bool(window.monitor.sid),flush=True);window.close()
        if '--smoke-scenario' in sys.argv:
            import smoke_scenarios
            smoke_scenarios.start(window,folder,sys.argv[sys.argv.index('--smoke-report')+1],stream_report='--smoke-stream-report' in sys.argv)
        elif '--smoke-speed' in sys.argv:
            window.measure_speed();window.speed_worker.ready.connect(lambda result:QTimer.singleShot(150,capture));window.speed_worker.failed.connect(lambda message:QTimer.singleShot(150,capture));QTimer.singleShot(150000,capture)
        else:
            seconds=max(4,min(120,float(sys.argv[sys.argv.index('--smoke-seconds')+1]))) if '--smoke-seconds' in sys.argv else (10 if '--smoke-port' in sys.argv else 4)
            QTimer.singleShot(int(seconds*1000),capture)
    app.exec();lock.close()

if __name__=='__main__':main()

"""Compact native Graphene port charts; source history stays in memory only."""
import math
import time
from PySide6.QtCore import Qt,QRectF,QTimer,QThread,Signal
from PySide6.QtGui import QColor,QPainter,QPen,QPainterPath,QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLineEdit,QComboBox,QToolButton,QLabel,QSizePolicy
from time_labels import format_timestamp
import error_log
from plot_widgets import CachedPlot
from plot_sampling import line_samples,nearest_point,axis_label,scale_headroom
import graphene_port as source

COLORS={'rx':'#7acb73','tx':'#f4cd35','rx_packets':'#7acb73','tx_packets':'#f4cd35','rx_errs':'#ef5263','tx_errs':'#ef8ba6','rx_drop':'#f58239','tx_drop':'#f4b753','flap':'#dce6f4'}
LABELS={'rx':'RX','tx':'TX','rx_packets':'RX','tx_packets':'TX','rx_errs':'RX err','tx_errs':'TX err','rx_drop':'RX drop','tx_drop':'TX drop','flap':'flap'}

def number(value):
    if value is None:return '—'
    return f'{value:.2f}'.rstrip('0').rstrip('.').replace('.',',')

def unit(peak,traffic):
    if not traffic:return 1,'п/с'
    return (1e9,'Гбит/с') if peak>=1e9 else (1e6,'Мбит/с') if peak>=1e6 else (1e3,'кбит/с') if peak>=1e3 else (1,'бит/с')

class PortFetch(QThread):
    ready=Signal(object);failed=Signal(object)
    def __init__(self,key,resolved=None,client=None,previous=None):super().__init__();self.key=key;self.resolved=resolved;self.client=client;self.previous=previous
    def run(self):
        try:
            canonical=self.resolved or source.resolve(*self.key[:2],session=self.client)
            data=source.fetch(*canonical,self.key[2],session=self.client,previous=self.previous);data['requested']=self.key;self.ready.emit(data)
        except source.SourceError as e:self.failed.emit((self.key,str(e)))
        except Exception as e:error_log.error('Чтение Graphene',e);self.failed.emit((self.key,'Не удалось прочитать данные Graphene'))

class PortPlot(CachedPlot):
    hovered=Signal(object)
    def __init__(self,title,keys):
        super().__init__();self.title=title;self.keys=keys;self.data=None;self.cursor=None;self.message="Укажите коммутатор и порт"
        self.setMinimumHeight(100);self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Expanding);self.setMouseTracking(True)
        self.setToolTip('RX — вход в коммутатор, TX — выход из коммутатора. Пропуски не равны нулю. Шаг определяется источником Graphene.')
    @property
    def data(self):return self._data
    @data.setter
    def data(self,value):
        self._data=value;self._info=None;self.invalidate_chart()
    def chart_info(self):
        key=(self._generation,self.height(),self.font().key())
        if self._info and self._info[0]==key:return self._info[1]
        series=self.data['series'] if self.data else {}
        peak=max((v for name in self.keys if name!='flap' for _,v in series.get(name,[]) if v is not None),default=0)
        factor,label=unit(peak,self.keys[0]=='rx');maximum=max(1,scale_headroom(peak))
        lines=5 if self.height()>=160 else 3;measure=self.fontMetrics().horizontalAdvance
        labels=[axis_label(maximum*(i/(lines-1))/factor,65,measure) for i in range(lines)]
        flapmax=max((v for _,v in series.get('flap',[]) if v is not None),default=1);flapmax=max(1,flapmax)
        flaplabel=axis_label(flapmax,55,measure)
        info=dict(factor=factor,label=label,maximum=maximum,labels=labels,
                  left=max(42,max(map(measure,labels))+6),
                  right=max(34,measure(flaplabel)+8) if 'flap' in self.keys else 13,
                  flapmax=flapmax,flaplabel=flaplabel)
        self._info=(key,info);return info
    def axis_labels(self):return self.chart_info()['labels']
    def plot_rect(self):
        info=self.chart_info();left=info['left'];right=info['right']
        return QRectF(left,26,max(1,self.width()-left-right),max(1,self.height()-54))
    def draw_chart(self,p):
        p.setRenderHint(QPainter.RenderHint.Antialiasing);p.fillRect(self.rect(),QColor('#141d2b'));p.setPen(QColor('#263449'));p.drawRect(self.rect().adjusted(0,0,-1,-1))
        p.setPen(QColor('#dce6f4'));p.drawText(8,15,self.title);r=self.plot_rect()
        if not self.data:
            p.setPen(QColor('#91a2bb'));p.drawText(r,Qt.AlignmentFlag.AlignCenter|Qt.TextFlag.TextWordWrap,self.message);return
        begin=self.data['begin'];end=self.data['end'];series=self.data['series'];step=self.data.get('step')
        info=self.chart_info();maximum=info['maximum']
        x=lambda ts:r.left()+(ts-begin)/max(1,end-begin)*r.width()
        y=lambda v:r.bottom()-v/maximum*r.height()
        p.setPen(QColor('#91a2bb'));p.drawText(QRectF(8,16,self.width()-16,12),Qt.AlignmentFlag.AlignRight,info['label']+(' · flap' if 'flap' in self.keys else ''))
        labels=info['labels']
        for i,label in enumerate(labels):
            yy=r.bottom()-r.height()*i/(len(labels)-1)
            p.setPen(QPen(QColor('#2b3a50'),1,Qt.PenStyle.DotLine));p.drawLine(int(r.left()),int(yy),int(r.right()),int(yy))
            p.setPen(QColor('#91a2bb'));p.drawText(QRectF(0,yy-7,r.left()-5,14),Qt.AlignmentFlag.AlignRight,label)
        for name in self.keys:
            if name=='flap':
                p.setPen(QPen(QColor(COLORS[name]),2))
                for ts,value in series[name]:
                    if value is not None and value>0:p.drawLine(int(x(ts)),int(r.bottom()),int(x(ts)),int(r.bottom()-value/info['flapmax']*r.height()))
                p.setPen(QColor('#91a2bb'));p.drawText(QRectF(r.right()+4,r.top()-6,info['right']-5,14),Qt.AlignmentFlag.AlignLeft,info['flaplabel'])
                continue
            sampled=line_samples(series[name],begin,end,r.width(),max(1,(step or 30)*1.6))
            segments=[];segment=[]
            for ts,value,break_before in sampled:
                if value is None or break_before:
                    if segment:segments.append(segment)
                    segment=[]
                if value is not None:segment.append((ts,value))
            if segment:segments.append(segment)
            p.save();p.setClipRect(r.adjusted(-2,-2,2,2))
            fill=QColor(COLORS[name]);fill.setAlpha(65)
            for segment in segments:
                area=QPainterPath();area.moveTo(x(segment[0][0]),r.bottom())
                for ts,value in segment:area.lineTo(x(ts),y(value))
                area.lineTo(x(segment[-1][0]),r.bottom());area.closeSubpath();p.fillPath(area,fill)
            p.setPen(QPen(QColor(COLORS[name]),1.3));previous=None
            for ts,value,break_before in sampled:
                if value is None:previous=None;continue
                xx=x(ts);yy=y(value)
                if previous and not break_before:p.drawLine(int(previous[0]),int(previous[1]),int(xx),int(yy))
                else:p.drawEllipse(QRectF(xx-1.5,yy-1.5,3,3))
                previous=(xx,yy)
            p.restore()
        ticks=2 if r.width()<200 else 3
        for i in range(ticks):
            ts=begin+(end-begin)*i/(ticks-1);xx=x(ts)
            alignment=Qt.AlignmentFlag.AlignLeft if i==0 else Qt.AlignmentFlag.AlignRight if i==ticks-1 else Qt.AlignmentFlag.AlignCenter
            left=r.left() if i==0 else r.right()-60 if i==ticks-1 else xx-30
            p.setPen(QColor('#91a2bb'));p.drawText(QRectF(left,r.bottom()+2,60,14),alignment,format_timestamp(ts,'%H:%M:%S' if end-begin<=300 else '%H:%M'))
        if len(self.keys)>2 and self.width()<310:
            p.setPen(QColor('#91a2bb'));p.drawText(8,self.height()-5,'err / drop / flap')
        else:
            xx=8
            for name in self.keys:
                p.setPen(QColor(COLORS[name]));text=LABELS[name];p.drawText(xx,self.height()-5,text);xx+=p.fontMetrics().horizontalAdvance(text)+12
        if not any(value is not None for name in self.keys for _,value in series[name]):
            p.setPen(QColor('#91a2bb'));p.drawText(r,Qt.AlignmentFlag.AlignCenter,'Нет точек в этом периоде')
    def draw_overlay(self,p):
        if self.cursor is None or not self.data:return
        r=self.plot_rect();ts=self.cursor
        if not self.data['begin']<=ts<=self.data['end']:return
        xx=r.left()+(ts-self.data['begin'])/max(1,self.data['end']-self.data['begin'])*r.width()
        p.setPen(QPen(QColor('#60a5fa'),1,Qt.PenStyle.DashLine));p.drawLine(int(xx),int(r.top()),int(xx),int(r.bottom()))
    def mouseMoveEvent(self,event):
        if not self.data:return
        rect=self.plot_rect();fraction=min(1,max(0,(event.position().x()-rect.left())/rect.width()))
        ts=self.data['begin']+fraction*(self.data['end']-self.data['begin']);self.hovered.emit(ts)
        rows=[]
        for name in self.keys:
            points=self.data['series'][name]
            if not points:rows.append(LABELS[name]+': нет данных');continue
            at,value=nearest_point(points,ts)
            factor,label=unit(value or 0,name in ('rx','tx'))
            rows.append(LABELS[name]+' '+number(None if value is None else value/factor)+' '+('флап' if name=='flap' else label)+' · '+format_timestamp(at))
        self.setToolTip('\n'.join(rows))
    def leaveEvent(self,event):self.hovered.emit(None)

class PortPanel(QWidget):
    received=Signal(object)
    def __init__(self):
        super().__init__();self.worker=None;self.connected=None;self.closing=False;self.snapshot=None;self.failed=False;self.auto=True;self.resolved=None;self.lldp_key=None;self.lldp_missing=False
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(3)
        header=QHBoxLayout();header.setSpacing(6);header.addWidget(QLabel('Порт · Graphene'))
        self.host=QLineEdit();self.host.setPlaceholderText('Коммутатор');self.host.setMaximumWidth(170)
        self.port=QLineEdit();self.port.setPlaceholderText('Порт');self.port.setMaximumWidth(180)
        for field in (self.host,self.port):
            field.setStyleSheet('QLineEdit{padding:3px 6px;}');field.returnPressed.connect(self.connect_source);field.textEdited.connect(self.manual);field.editingFinished.connect(self.source_edited)
        header.addWidget(self.host);header.addWidget(self.port)
        self.read=QToolButton();self.read.setText('↻');self.read.setToolTip('Прочитать порт из Graphene');self.read.clicked.connect(self.connect_source);header.addWidget(self.read)
        self.auto_button=QToolButton();self.auto_button.setText('LLDP');self.auto_button.setCheckable(True);self.auto_button.setChecked(True);self.auto_button.setToolTip('Автоматически выбирать порт по LLDP; нажмите, чтобы вернуться из ручного режима');self.auto_button.clicked.connect(self.use_lldp);header.addWidget(self.auto_button)
        self.status=QLabel('Укажите источник или дождитесь LLDP');self.status.setStyleSheet('color:#91a2bb;font-size:10px;');self.status.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);header.addWidget(self.status,1)
        self.period=QComboBox();self.period.setStyleSheet('QComboBox{padding:3px 6px;}')
        for value,label in source.PERIODS:self.period.addItem(label,value)
        self.period.setCurrentIndex(2);self.period.currentIndexChanged.connect(self.change_period);header.addWidget(self.period)
        self.link=QToolButton();self.link.setText('↗');self.link.setToolTip('Открыть этот порт в Graphene');self.link.clicked.connect(self.open_source);header.addWidget(self.link)
        layout.addLayout(header);row=QHBoxLayout();row.setSpacing(5)
        self.plots=[PortPlot('Трафик',('rx','tx')),PortPlot('Пакеты',('rx_packets','tx_packets')),PortPlot('Ошибки / флапы',('rx_errs','tx_errs','rx_drop','tx_drop','flap'))]
        for plot in self.plots:row.addWidget(plot,1);plot.hovered.connect(self.hover)
        layout.addLayout(row)
        self.timer=QTimer(self);self.timer.setInterval(5000);self.timer.timeout.connect(self.refresh)
        self.clock=QTimer(self);self.clock.setInterval(1000);self.clock.timeout.connect(self.age);self.clock.start()
    def manual(self,*args):
        self.auto=False
        self.auto_button.setChecked(False)
        self.lldp_missing=False
        if self.connected and (self.host.text().strip(),self.port.text().strip())!=(self.resolved or self.connected[:2]):
            self.connected=None;self.resolved=None;self.timer.stop();self.clear();self.message('Завершите ввод коммутатора и порта')
    def source_edited(self):
        if self.auto or not self.host.text().strip() or not self.port.text().strip():return
        key=(self.host.text().strip(),self.port.text().strip(),self.period.currentData())
        if not self.connected or key[:2]!=(self.resolved or self.connected[:2]) or key[2]!=self.connected[2]:self.connect_source()
    def set_status(self,text,error=False):
        self.status.setText(text);self.status.setToolTip(text)
        self.status.setStyleSheet('color:'+('#ef6275' if error else '#91a2bb')+';font-size:10px;')
    def message(self,text):
        self.set_status(text,self.failed)
        for plot in self.plots:plot.message=text;plot.update()
    def lldp(self,neighbor):
        self.last_neighbor=dict(neighbor)
        if not self.auto:return
        host=neighbor.get('switch');port=neighbor.get('port')
        if not host or not port:
            if self.connected:
                self.lldp_missing=True;self.timer.stop();self.age()
                if not self.snapshot:self.set_status('Нет LLDP · ждём подключения; источник сохранён')
            return
        try:source.validate_target(host,port)
        except source.SourceError:return
        reconnecting=self.lldp_missing;self.lldp_missing=False
        if self.lldp_key!=(host,port):
            self.lldp_key=(host,port);self.host.setText(host);self.port.setText(port);self.connect_source()
        elif reconnecting:
            self.failed=False;self.timer.start();self.age();self.refresh()
    def use_lldp(self,checked=True):
        self.auto=checked
        if not checked:
            self.lldp_missing=False
            if self.connected:self.timer.start();self.age();self.refresh()
            return
        self.lldp_key=None;self.lldp_missing=False
        neighbor=getattr(self,'last_neighbor',{})
        if neighbor.get('switch') and neighbor.get('port'):self.lldp(neighbor)
        else:
            self.timer.stop();self.lldp_missing=True;self.age()
            if not self.snapshot:self.message('Нет LLDP · ждём подключения')
    def change_period(self,*args):
        if self.connected:
            self.connected=(self.connected[0],self.connected[1],self.period.currentData())
            if self.lldp_missing:
                # Keep the last available history, including its actual timestamps.
                if self.snapshot:
                    data=dict(self.snapshot);data['period']=self.period.currentData();data['begin']=data['end']-data['period']
                    data['series']={key:[point for point in points if data['begin']<=point[0]<=data['end']] for key,points in data['series'].items()}
                    for plot in self.plots:plot.data=data;plot.update()
                return
            self.clear();self.refresh()
    def connect_source(self,*args):
        try:host,port=source.validate_target(self.host.text(),self.port.text())
        except source.SourceError as e:self.message(str(e));return
        self.host.setToolTip(host);self.port.setToolTip(port)
        self.host.setCursorPosition(0);self.port.setCursorPosition(0)
        key=(host,port,self.period.currentData())
        if key!=self.connected:self.connected=key;self.resolved=None;self.clear()
        if not self.lldp_missing:self.timer.start();self.refresh()
    def clear(self):
        self.snapshot=None;self.failed=False
        for plot in self.plots:plot.data=None;plot.update()
    def refresh(self):
        if self.closing or self.lldp_missing or not self.connected or (self.worker and self.worker.isRunning()):return
        if self.worker:self.worker.deleteLater()
        if not hasattr(self,'client'):self.client=source.client_session()
        self.message('Читаем Graphene…');self.worker=PortFetch(self.connected,self.resolved,self.client,self.snapshot);self.worker.ready.connect(self.receive);self.worker.failed.connect(self.failure);self.worker.finished.connect(self.finished);self.worker.start()
    def finished(self):
        if self.worker and self.worker.key!=self.connected:QTimer.singleShot(0,self.refresh)
    def receive(self,data):
        if self.closing or self.lldp_missing or data.get('requested',(data['host'],data['port'],data['period']))!=self.connected:return
        self.resolved=(data['host'],data['port']);self.snapshot=data;self.failed=False
        for field,value in ((self.host,data['host']),(self.port,data['port'])):
            field.setToolTip(value)
            if field.text()!=value:field.setText(value);field.setCursorPosition(0)
        for plot in self.plots:plot.data=data;plot.update()
        self.received.emit(data)
        self.age()
    def failure(self,error):
        key,message=error
        if self.closing or self.lldp_missing or key!=self.connected:return
        self.failed=True;self.message('Нет свежих данных · '+message)
        # Retain old history with its original timestamps; no false zeros or current samples.
    def age(self):
        if not self.snapshot or (self.failed and not self.lldp_missing):return
        latest=self.snapshot['latest'];step=self.snapshot['step']
        if latest is None:text='Graphene не вернул данных · проверьте источник'
        else:
            age=max(0,int(time.time()-latest));text=('Устарели · ' if age>max(90,(step or 30)*3) else '')+'до '+format_timestamp(latest)+' · задержка '+str(age)+' с'+(' · шаг '+number(step)+' с' if step else ' · одна точка')+' · опрос 5 с'
        if self.lldp_missing:text='Нет LLDP · последние данные · '+text
        self.set_status(text);self.status.setToolTip(text+'\nПериод заканчивается последней доступной точкой Graphene. Время на шкале — настоящее время измерений.\nRX/TX относительно порта коммутатора. Нули — только реальные нули источника; пропуски — отсутствие измерения.')
    def hover(self,ts):
        for plot in self.plots:plot.cursor=ts;plot.update()
    def open_source(self):
        if self.connected:QDesktopServices.openUrl(QUrl(source.url(*(self.resolved or self.connected[:2]),self.connected[2])))
    def shutdown(self):
        self.closing=True;self.timer.stop();self.clock.stop()
        if self.worker and self.worker.isRunning():self.worker.wait()
        if hasattr(self,'client'):self.client.close()

"""Stream tab: OBS/vMix/SRT/RTMP/NDI stream health, charts, events and provider report."""
import math
import time
from PySide6.QtCore import Qt,QEvent,QObject,QRectF,QTimer,Signal,QSignalBlocker
from PySide6.QtGui import QColor,QPainter,QPen
from PySide6.QtWidgets import (QApplication,QComboBox,QFileDialog,QHBoxLayout,QHeaderView,QLabel,QLineEdit,QMessageBox,
    QPlainTextEdit,QPushButton,QSizePolicy,QSplitter,QTableWidget,QTableWidgetItem,QVBoxLayout,QWidget)
from time_labels import format_timestamp
import error_log
import export_transfer
from log_widgets import update_lines,format_events
from plot_sampling import chart_samples,nearest_point,axis_label,nice_scale
from plot_widgets import CachedPlot
import stream_monitor as engine
from traffic_widgets import capture_icon
from ui_tasks import Call

PERIODS=[(60,'1 мин'),(300,'5 мин'),(900,'15 мин'),(1800,'30 мин'),(3600,'1 час'),(10800,'3 часа'),(43200,'12 часов')]
STATES={'live':('В ЭФИРЕ','#22c55e'),'reconnecting':('ПЕРЕПОДКЛЮЧЕНИЕ','#ef6275'),'connected':('подключено','#60a5fa'),'found':('обнаружен','#91a2bb'),
        'off':('остановлен','#91a2bb'),'lost':('пропал','#f59e0b')}
LEVEL_COLORS=('#dce6f4','#f4cd35','#ef6275')
COLUMNS=['Источник','Протокол','Назначение','Состояние','В эфире','Битрейт','FPS','Потеряно','Потери/с','Congest.','RTT','Ретр., %','Примечание']
COLUMN_TIPS=['','','Сервер приёма без ключа или адрес соединения','','Длительность эфира','Битрейт, кбит/с','Кадров в секунду',
             'Потеряно кадров всего (доля, %)','Среднее число потерянных кадров в секунду между двумя свежими опросами; после паузы нужен новый замер','Congestion OBS 0–1: заполненность буфера отправки',
             'RTT TCP до сервера приёма, мс','Повторно отправленные TCP-сегменты, %','']
COLUMN_WIDTHS=[170,80,220,110,70,80,50,110,70,70,55,70,220]
VMIX_COLUMNS=['vMix Statistics','Тип','Source','Renderer','Resync','Queue','Видео / аудио','Δ за опрос']
VMIX_WIDTHS=[160,80,60,70,60,60,260,170]
BUTTON_STYLE='QPushButton{background:#172437;border:1px solid #3b82f6;border-radius:6px;padding:4px 12px;color:#dce6f4;} QPushButton:hover{background:#203f67;border-color:#93c5fd;} QPushButton:pressed{background:#2563eb;} QPushButton:disabled{background:#172130;border-color:#3c4c62;color:#9cabbd;}'

def fmt(value,digits=0):return engine.fmt(value,digits)


def network_context(reader):
    if reader is None:return None
    try:return reader()
    except Exception as exc:
        error_log.error('Сетевой контекст для отчёта Stream',exc)
        return ['Не удалось получить свежие сетевые измерения. Повторите создание отчёта.']


class SeriesPlot(CachedPlot):
    """Time series for one stream; gaps are drawn as gaps, not zeros."""
    def __init__(self,title,series):
        super().__init__();self.title=title;self.series=series;self.points=[];self.start=0;self.end=1;self.message='Выберите поток';self._samples=None;self._sample_key=None
        self.setMinimumHeight(120);self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Expanding);self.setMouseTracking(True)
    def set_data(self,points,end,period):
        self.end=end;self.start=end-period
        self.points=sorted((point for point in points if self.start<=point[0]<=end),key=lambda point:point[0])
        self._samples=None;self.invalidate_chart();self.update()
    def sampled(self):
        rect=self.plot_rect();key=(self._generation,int(rect.width()))
        if self._samples is None or self._sample_key!=key:
            self._samples=chart_samples(self.points,self.start,self.end,rect.width(),[s['key'] for s in self.series]);self._sample_key=key
        return self._samples
    def scale(self,item):
        if item.get('max'):return item['max']
        peak=max((p[1].get(item['key']) or 0 for p in self.sampled()),default=0)
        peak=max(peak,item.get('min_scale',1))
        return nice_scale(peak)
    def plot_rect(self):
        right=any(series.get('axis')=='right' for series in self.series)
        top=43 if self.legend_wraps() else 28
        return QRectF(48,top,max(1,self.width()-(92 if right else 60)),max(1,self.height()-top-20))
    def legend_wraps(self):
        measure=self.fontMetrics().horizontalAdvance
        return 24+measure(self.title)+sum(measure('● '+s['label'])+10 for s in self.series)>self.width()
    def draw_chart(self,p):
        p.setRenderHint(QPainter.RenderHint.Antialiasing);p.fillRect(self.rect(),QColor('#141d2b'));p.setPen(QColor('#263449'));p.drawRect(self.rect().adjusted(0,0,-1,-1))
        p.setPen(QColor('#dce6f4'));p.drawText(8,15,self.title)
        # Legend follows the title so it never covers the time axis.
        wrapped=self.legend_wraps();xx=8 if wrapped else 16+p.fontMetrics().horizontalAdvance(self.title)
        for item in self.series:
            p.setPen(QColor(item['color']));p.drawText(xx,30 if wrapped else 15,'● '+item['label']);xx+=p.fontMetrics().horizontalAdvance('● '+item['label'])+10
        r=self.plot_rect()
        x=lambda ts:r.left()+(ts-self.start)/max(1,self.end-self.start)*r.width()
        samples=self.sampled()
        if not self.points or not any(p_[1].get(s['key']) is not None for p_ in samples for s in self.series):
            p.setPen(QColor('#91a2bb'));p.drawText(r,Qt.AlignmentFlag.AlignCenter|Qt.TextFlag.TextWordWrap,self.message if not self.points else 'Нет данных этого показателя');return
        left=next(s for s in self.series if s.get('axis','left')=='left');maximum=self.scale(left);lines=5 if self.height()>=170 else 3
        for i in range(lines):
            yy=r.bottom()-r.height()*i/(lines-1)
            p.setPen(QPen(QColor('#2b3a50'),1,Qt.PenStyle.DotLine));p.drawLine(int(r.left()),int(yy),int(r.right()),int(yy))
            p.setPen(QColor('#91a2bb'));p.drawText(QRectF(0,yy-7,43,14),Qt.AlignmentFlag.AlignRight,axis_label(maximum*(i/(lines-1)),43,p.fontMetrics().horizontalAdvance,0 if maximum>=10 else 1))
        for item in self.series:
            if item.get('axis')=='right':
                top=self.scale(item);p.setPen(QColor(item['color']));p.drawText(QRectF(r.right()+4,r.top()-7,40,14),Qt.AlignmentFlag.AlignLeft,axis_label(top,40,p.fontMetrics().horizontalAdvance))
        p.save();p.setClipRect(r.adjusted(-1,-1,1,1))
        # Live/reconnect state strip under the chart.
        for ts,_,state,_ in samples:
            if state=='reconnecting':p.fillRect(QRectF(x(ts)-1,r.top(),2,r.height()),QColor(239,98,117,60))
        for item in self.series:
            top=self.scale(item);y=lambda v:r.bottom()-min(1,v/top)*r.height();color=QColor(item['color'])
            if item.get('style')=='bars':
                for ts,values,_,_ in samples:
                    value=values.get(item['key'])
                    if value:p.fillRect(QRectF(x(ts)-1.5,y(value),3,r.bottom()-y(value)),color)
                continue
            p.setPen(QPen(color,1.4));last=None
            for ts,values,_,break_before in samples:
                value=values.get(item['key'])
                if value is None:last=None;continue
                point=(x(ts),y(value))
                if last and not break_before:p.drawLine(int(last[0]),int(last[1]),int(point[0]),int(point[1]))
                else:p.drawEllipse(QRectF(point[0]-1,point[1]-1,2,2))
                last=(*point,ts)
        p.restore();p.setPen(QColor('#91a2bb'));ticks=2 if r.width()<200 else 3
        for i in range(ticks):
            ts=self.start+(self.end-self.start)*i/(ticks-1);alignment=Qt.AlignmentFlag.AlignLeft if i==0 else Qt.AlignmentFlag.AlignRight if i==ticks-1 else Qt.AlignmentFlag.AlignCenter
            p.drawText(QRectF(r.left() if i==0 else r.right()-60 if i==ticks-1 else x(ts)-30,r.bottom()+3,60,14),alignment,format_timestamp(ts))
    def mouseMoveEvent(self,event):
        if not self.points:return
        rect=self.plot_rect();fraction=min(1,max(0,(event.position().x()-rect.left())/rect.width()))
        ts=self.start+fraction*(self.end-self.start)
        at,values,state=nearest_point(self.points,ts)
        self.setToolTip(format_timestamp(at)+' · '+STATES.get(state,(state,''))[0]+'\n'+'\n'.join(s['label']+': '+fmt(values.get(s['key']),2) for s in self.series))

class ColumnFitter(QObject):
    """Columns resizable by the user; window width is shared in the user's proportions."""
    def __init__(self,table,widths):
        super().__init__(table);self.table=table;self.weights=list(widths);self.fitting=False
        header=table.horizontalHeader();header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive);header.setStretchLastSection(False)
        header.setMinimumSectionSize(36);header.setSectionsMovable(False)
        # Automatic fitting never cuts a header; a narrow window gets a horizontal scroll bar.
        metrics=header.fontMetrics()
        self.minimums=[max(36,metrics.horizontalAdvance(table.horizontalHeaderItem(i).text() if table.horizontalHeaderItem(i) else '')+18) for i in range(table.columnCount())]
        for column,width in enumerate(widths):table.setColumnWidth(column,width)
        header.sectionResized.connect(self.resized);table.viewport().installEventFilter(self)
    def eventFilter(self,watched,event):
        if event.type()==QEvent.Type.Resize:self.fit()
        return False
    def resized(self,*args):
        # A drag or a double-click autofit on the divider becomes the new proportion.
        if not self.fitting:self.weights=[self.table.columnWidth(i) for i in range(self.table.columnCount())]
    def fit(self):
        available=self.table.viewport().width();total=sum(self.weights)
        if available<=0 or total<=0:return
        # Reserve headers which cannot fit their proportional share, then divide
        # the remaining width. Clamping just the last column caused overflow.
        remaining=max(available,sum(self.minimums));active=list(range(len(self.weights)));widths=[0]*len(active)
        while active:
            weight=sum(self.weights[i] for i in active)
            fixed=[i for i in active if remaining*self.weights[i]/weight<self.minimums[i]]
            if not fixed:
                for i in active:widths[i]=int(remaining*self.weights[i]/weight)
                widths[active[-1]]+=remaining-sum(widths[i] for i in active)
                break
            for i in fixed:
                widths[i]=self.minimums[i];remaining-=widths[i];active.remove(i)
        self.fitting=True
        try:
            for column,width in enumerate(widths):self.table.setColumnWidth(column,width)
        finally:self.fitting=False

class StreamPanel(QWidget):
    changed=Signal()
    def __init__(self,network_report=None,monitor=None,network_report_factory=None):
        super().__init__();self.monitor=monitor or engine.StreamMonitor();self.network_report=network_report;self.network_report_factory=network_report_factory
        self.selected=None;self.closing=False;self.data={};self.report_worker=None;self.report_pending=False
        layout=QVBoxLayout(self);layout.setContentsMargins(12,10,12,12);layout.setSpacing(6)
        header=QHBoxLayout();header.setSpacing(6);settings=QHBoxLayout();settings.setSpacing(6)
        self.play=QPushButton();self.play.setFixedSize(38,30);self.play.setStyleSheet('QPushButton{padding:5px;}');self.play.clicked.connect(self.toggle);header.addWidget(self.play)
        self.live=QLabel('ОСТАНОВЛЕНО');self.live.setStyleSheet('color:#91a2bb;font-size:10px;');self.live.setMinimumWidth(90);header.addWidget(self.live)
        settings.addWidget(self.caption('OBS'))
        self.obs_host=QLineEdit('127.0.0.1');self.obs_host.setFixedWidth(130);self.obs_port=QLineEdit();self.obs_port.setPlaceholderText('4455');self.obs_port.setFixedWidth(60)
        self.obs_password=QLineEdit();self.obs_password.setEchoMode(QLineEdit.EchoMode.Password);self.obs_password.setPlaceholderText('пароль · авто');self.obs_password.setFixedWidth(130)
        self.obs_password.setToolTip('Пароль obs-websocket. Пусто и адрес этого компьютера — порт и пароль берутся из настроек OBS текущего пользователя. Пароль не сохраняется.')
        settings.addWidget(self.obs_host);settings.addWidget(self.obs_port);settings.addWidget(self.obs_password);settings.addSpacing(12)
        settings.addWidget(self.caption('vMix API'));self.vmix_url=QLineEdit(engine.sources.VMIX_API);self.vmix_url.setFixedWidth(240);settings.addWidget(self.vmix_url);settings.addStretch(1)
        self.vmix_url.setToolTip('Веб-API vMix (Настройки → Web Controller). Может указывать на другой компьютер. Окно Statistics читается только на этом компьютере.')
        for field in (self.obs_host,self.obs_port,self.obs_password,self.vmix_url):
            field.setStyleSheet('QLineEdit{padding:3px 6px;}');field.editingFinished.connect(self.apply_settings)
        header.addStretch(1);header.addWidget(self.caption('Период'))
        self.period=QComboBox();self.period.setStyleSheet('QComboBox{padding:3px 6px;}')
        for value,label in PERIODS:self.period.addItem(label,value)
        self.period.setCurrentIndex(1);self.period.currentIndexChanged.connect(self.refresh);header.addWidget(self.period)
        header.addSpacing(10);header.addWidget(self.caption('Отчёт для провайдера'))
        tip='Сводка за выбранный период: битрейт, потери кадров, congestion, RTT, события в местном времени и UTC, сеть. Ключи потоков не включаются.'
        self.copy_button=QPushButton('Копировать');self.copy_button.clicked.connect(self.copy_report)
        self.save_button=QPushButton('Сохранить TXT…');self.save_button.clicked.connect(self.save_report)
        for button in (self.copy_button,self.save_button):
            button.setStyleSheet(BUTTON_STYLE);button.setFixedHeight(30);button.setCursor(Qt.CursorShape.PointingHandCursor);button.setToolTip(tip);header.addWidget(button)
        self.copy_button.setMinimumWidth(self.copy_button.fontMetrics().horizontalAdvance('Скопировано')+30)
        self.save_button.setMinimumWidth(self.save_button.sizeHint().width())
        layout.addLayout(header);layout.addLayout(settings)
        self.sources=QLabel();self.sources.setTextFormat(Qt.TextFormat.RichText);self.sources.setWordWrap(True);self.sources.setStyleSheet('font-size:11px;');layout.addWidget(self.sources)
        self.table=QTableWidget(0,len(COLUMNS));self.table.setHorizontalHeaderLabels(COLUMNS);self.table.verticalHeader().hide()
        for column,tip in enumerate(COLUMN_TIPS):
            if tip:self.table.horizontalHeaderItem(column).setToolTip(tip)
        self.table.verticalHeader().setDefaultSectionSize(20);self.table.setAlternatingRowColors(True);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows);self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setStyleSheet('QTableWidget{font-size:11px;} QHeaderView::section{padding:3px;}');self.table.setMinimumHeight(90)
        self.table.horizontalHeader().setToolTip('Ширину столбца меняйте перетаскиванием границы; двойной щелчок — по содержимому. При изменении окна пропорции сохраняются.')
        self.columns=ColumnFitter(self.table,COLUMN_WIDTHS)
        self.table.cellClicked.connect(self.select_row);self.table.currentCellChanged.connect(self.select_row)
        self.chart_title=QLabel('Поток не выбран');self.plots=[SeriesPlot('Битрейт, кбит/с',[{'key':'bitrate','label':'битрейт','color':'#7acb73','min_scale':100}]),
            SeriesPlot('Потери кадров / с',[{'key':'dropped_rate','label':'потеряно','color':'#ef6275','style':'bars','min_scale':5}]),
            SeriesPlot('RTT, мс · congestion',[{'key':'rtt','label':'RTT','color':'#42b4ff','min_scale':50},{'key':'congestion','label':'congestion 0–1','color':'#f4cd35','axis':'right','max':1}])]
        charts=QWidget();charts_layout=QVBoxLayout(charts);charts_layout.setContentsMargins(0,0,0,0);charts_layout.setSpacing(4);charts_layout.addWidget(self.chart_title)
        row=QHBoxLayout();row.setSpacing(5)
        for plot in self.plots:row.addWidget(plot,1)
        charts_layout.addLayout(row,1)
        self.vmix_table=QTableWidget(0,len(VMIX_COLUMNS));self.vmix_table.setHorizontalHeaderLabels(VMIX_COLUMNS)
        self.vmix_table.verticalHeader().hide();self.vmix_table.verticalHeader().setDefaultSectionSize(20);self.vmix_table.setAlternatingRowColors(True);self.vmix_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.vmix_table.setStyleSheet('QTableWidget{font-size:11px;} QHeaderView::section{padding:3px;}');self.vmix_columns=ColumnFitter(self.vmix_table,VMIX_WIDTHS)
        self.vmix_table.setToolTip('Откройте в vMix окно Statistics. Δ — рост счётчиков между двумя свежими опросами; 0 — изменений нет. После паузы нужна новая пара замеров.')
        self.events=QPlainTextEdit();self.events.setReadOnly(True);self.events.setPlaceholderText('События трансляций');self.event_lines=[];self.event_labels={}
        bottom=QSplitter(Qt.Orientation.Horizontal);bottom.setChildrenCollapsible(False);bottom.addWidget(self.vmix_table);bottom.addWidget(self.events);bottom.setSizes([620,420])
        self.splitter=QSplitter(Qt.Orientation.Vertical);self.splitter.setChildrenCollapsible(False);self.splitter.setHandleWidth(6)
        for widget in (self.table,charts,bottom):self.splitter.addWidget(widget)
        self.splitter.setStretchFactor(0,2);self.splitter.setStretchFactor(1,3);self.splitter.setStretchFactor(2,2);self.splitter.setSizes([200,300,200])
        layout.addWidget(self.splitter,1)
        hint=QLabel('Данные только читаются из OBS (obs-websocket), vMix (API и окно Statistics), таблиц соединений Windows и mDNS. История — в памяти, до 12 часов; очищается при закрытии.')
        hint.setStyleSheet('color:#91a2bb;font-size:10px;');hint.setWordWrap(True);layout.addWidget(hint)
        self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.refresh_visible)
        self.update_control();self.refresh()
    def caption(self,text):
        label=QLabel(text);label.setStyleSheet('color:#91a2bb;font-size:11px;');return label
    def apply_settings(self):
        port=self.obs_port.text().strip()
        self.monitor.configure(obs_host=self.obs_host.text().strip() or '127.0.0.1',obs_port=int(port) if port.isdigit() else None,
                               obs_password=self.obs_password.text(),vmix_url=self.vmix_url.text().strip())
    def toggle(self):
        if self.monitor.running:self.monitor.pause();self.timer.start()
        else:self.apply_settings();self.monitor.start();self.timer.start()
        self.update_control();self.refresh()
    def update_control(self):
        running=self.monitor.running
        stopping=bool(self.monitor.thread and self.monitor.thread.is_alive() and not running)
        self.play.setEnabled(not stopping)
        self.play.setIcon(capture_icon('pause' if running else 'play'));self.play.setAccessibleName('Пауза' if running else 'Пуск')
        self.play.setToolTip('Приостановить опрос; история сохранится' if running else 'Начать опрос OBS, vMix, TCP и NDI')
        self.live.setText('ЗАВЕРШАЕМ…' if stopping else '● LIVE' if running else 'ПАУЗА' if self.monitor.started else 'ОСТАНОВЛЕНО')
    def refresh(self,*args):
        if self.closing:return
        self.data=self.monitor.snapshot(self.period.currentData(),key=self.selected);self.render(self.data)
    def refresh_visible(self):
        if self.isVisible():self.refresh()
        elif not self.closing:
            # Retention still applies to a paused monitor on a hidden tab.
            with self.monitor.lock:self.monitor.prune(self.monitor.clock())
    def showEvent(self,event):
        super().showEvent(event)
        self.refresh()
    def hideEvent(self,event):
        super().hideEvent(event)
        # Release cached chart history, preserving event scroll/selection.
        # Showing the tab rebuilds the view at the current retention boundary.
        self.data={}
        for plot in self.plots:plot.set_data([],self.monitor.clock(),self.period.currentData())
    def visible_streams(self,data):
        now=data['now']
        # Short-lived TCP connections leave the table a minute after closing.
        return [s for s in data['streams'] if not (s['key'].startswith('tcp:') and s['state']=='lost' and now-s.get('last_seen',now)>60)]
    def render(self,data):
        names={'obs':'OBS','vmix_api':'vMix API','vmix_stats':'vMix Statistics','tcp':'TCP','ndi':'NDI'};parts=[]
        for key,status in data['status'].items():
            color={'ok':'#22c55e','error':'#ef6275'}.get(status['state'],'#91a2bb')
            parts.append(f"<span style='color:{color}'>●</span> <b>{names[key]}</b>: {escape(status['text'])}")
        obs=data.get('obs') or {}
        if obs:parts.append(f"<b>OBS</b>: {fmt(obs.get('active_fps'),1)} fps · CPU {fmt(obs.get('cpu'),1)}% · рендер {fmt(obs.get('render_ms'),1)} мс · пропущено рендером {obs.get('render_skipped')} · кодировщиком {obs.get('encoder_skipped')}")
        self.sources.setText(' &nbsp; '.join(parts))
        order={'live':0,'reconnecting':0,'connected':1,'lost':2,'off':3,'found':4}
        streams=sorted(self.visible_streams(data),key=lambda s:(order.get(s['state'],5),s['key']))
        if self.selected is None or not any(s['key']==self.selected for s in streams):
            self.selected=next((s['key'] for s in streams if s['state'] in ('live','reconnecting')),None)
            if self.selected and self.selected not in data['history']:QTimer.singleShot(0,self.refresh)
        with QSignalBlocker(self.table):
            self.table.setRowCount(len(streams))
            for row,s in enumerate(streams):
                state,color=STATES.get(s['state'],(s['state'],'#dce6f4'))
                values=[(s['label'],None),(s.get('protocol') or '',None),(s.get('target') or s.get('tcp_target') or '',None),(state,color),(engine.duration(s.get('duration')) if s.get('duration') else '',None),
                        (fmt(s.get('bitrate')),None),(fmt(s.get('fps'),1),None),
                        ('' if s.get('frames_dropped') is None else f"{s['frames_dropped']}"+(f" ({fmt(s['drop_pct'],2)}%)" if s.get('drop_pct') is not None else ''),LEVEL_COLORS[engine.level('drop_pct',s.get('drop_pct'))]),
                        (fmt(s.get('dropped_rate'),2),LEVEL_COLORS[2] if s.get('dropped_rate') else None),
                        (fmt(s.get('congestion'),2),LEVEL_COLORS[engine.level('congestion',s.get('congestion'))]),(fmt(s.get('rtt')),LEVEL_COLORS[engine.level('rtt',s.get('rtt'))]),
                        (fmt(s.get('retrans_pct'),2),LEVEL_COLORS[engine.level('retrans_pct',s.get('retrans_pct'))]),(s.get('note') or '',None)]
                for col,(text,foreground) in enumerate(values):
                    item=QTableWidgetItem(text if text!='—' else '');item.setData(Qt.ItemDataRole.UserRole,s['key'])
                    if foreground:item.setForeground(QColor(foreground))
                    if col==0:item.setToolTip(s['key']+(' · процесс '+s['process'] if s.get('process') else ''))
                    elif text and text!='—':item.setToolTip(text)
                    self.table.setItem(row,col,item)
                if s['key']==self.selected:self.table.selectRow(row)
        stream=next((s for s in streams if s['key']==self.selected),None)
        self.chart_title.setText((stream['label']+' · '+STATES.get(stream['state'],(stream['state'],''))[0]) if stream else 'Выберите поток в таблице')
        points=data['history'].get(self.selected,[]) if self.selected else []
        for plot in self.plots:plot.set_data(points,data['now'],self.period.currentData())
        rows=data.get('vmix_rows') or []
        self.vmix_table.setRowCount(len(rows))
        for row,r in enumerate(rows):
            delta=r.get('delta') or {};changes=', '.join(f'{k} +{v}' for k,v in delta.items() if v)
            media=r['video']+(' · '+r['audio'] if r['audio'] else '')
            for col,text in enumerate([r['name'],r['kind'],str(r.get('source_dropped') if r.get('source_dropped') is not None else ''),str(r.get('renderer_dropped') if r.get('renderer_dropped') is not None else ''),
                                       str(r.get('resync') if r.get('resync') is not None else ''),str(r.get('queue') if r.get('queue') is not None else ''),media,changes or ('без изменений' if delta else 'нужен второй опрос')]):
                item=QTableWidgetItem(text)
                if col==7:
                    item.setForeground(QColor(LEVEL_COLORS[2] if changes else '#22c55e' if delta else '#91a2bb'))
                    if r.get('interval_seconds') is not None:item.setToolTip('Изменение за '+fmt(r['interval_seconds'],2)+' с между опросами')
                if col==6:item.setToolTip(media)
                self.vmix_table.setItem(row,col,item)
        lines,self.event_labels=format_events(data['events'],self.event_labels,format_timestamp)
        self.event_lines=update_lines(self.events,self.event_lines,lines)
        self.update_control()
    def select_row(self,row,column,*unused):
        item=self.table.item(row,0)
        if item and item.data(Qt.ItemDataRole.UserRole)!=self.selected:
            self.selected=item.data(Qt.ItemDataRole.UserRole);self.refresh()
    def report_text(self):
        return self.monitor.report(self.period.currentData(),network_context(self.network_report))
    def copy_report(self):
        self.start_report()
    def save_report(self):
        path,_=QFileDialog.getSaveFileName(self,'Отчёт о трансляции',f'Stream-report-{time.strftime("%Y%m%d-%H%M%S")}.txt','Текст (*.txt)')
        if not path:return
        self.start_report(path)
    def start_report(self,path=None):
        if self.closing or self.report_pending:return
        # GUI values are captured here; the factory returns an engine-only
        # reader. The worker must never access QWidgets.
        reader=self.network_report_factory() if self.network_report_factory else None
        if reader is None:
            network=network_context(self.network_report)
            reader=lambda:network
        monitor=self.monitor;period=self.period.currentData()
        def prepare():
            text=monitor.report(period,network_context(reader))
            if path:export_transfer.atomic_write(path,lambda output:output.write(text.encode('utf-8')))
            return text
        if self.report_worker:self.report_worker.deleteLater()
        self.report_pending=True;self.report_path=path
        self.copy_button.setEnabled(False);self.save_button.setEnabled(False)
        if path:self.save_button.setText('Сохраняем…')
        else:self.copy_button.setText('Готовим…')
        self.report_worker=Call(prepare);self.report_worker.finished.connect(self.report_finished);self.report_worker.start()
    def report_finished(self):
        worker=self.sender()
        if worker is None or worker is not self.report_worker:return
        self.report_pending=False
        result,error=worker.result,worker.error;worker.result=None;worker.error=None;worker.function=None
        if self.closing:return
        self.copy_button.setEnabled(True);self.save_button.setEnabled(True)
        self.copy_button.setText('Копировать');self.save_button.setText('Сохранить TXT…')
        if error:
            error_log.error('Подготовка отчёта Stream',error)
            QMessageBox.warning(self,'Ошибка отчёта','Не удалось подготовить или сохранить отчёт. Проверьте путь и повторите.');return
        if not self.report_path:
            QApplication.clipboard().setText(result);self.copy_button.setText('Скопировано')
            QTimer.singleShot(1200,self.reset_copy_caption)
    def reset_copy_caption(self):
        if not self.closing and not self.report_pending:self.copy_button.setText('Копировать')
    def shutdown(self):
        self.closing=True;self.timer.stop()
        if self.report_worker and self.report_worker.isRunning():
            if not self.report_worker.wait(5000):raise RuntimeError('Подготовка отчёта Stream ещё завершается')
        if self.report_worker:
            self.report_worker.result=None;self.report_worker.error=None;self.report_worker.function=None
            self.report_worker.deleteLater();self.report_worker=None;self.report_pending=False
        self.monitor.clear()
        self.table.setRowCount(0);self.vmix_table.setRowCount(0);self.events.clear();self.event_lines=[];self.event_labels={}
        for plot in self.plots:plot.set_data([],time.time(),self.period.currentData())

def escape(text):
    return str(text).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

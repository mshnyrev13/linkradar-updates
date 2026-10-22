"""Compact explicit Play/Pause/Stop capture panel, independent of ping and Graphene."""
import time
from time_labels import format_timestamp
from bisect import bisect_left
from PySide6.QtCore import Qt,QAbstractTableModel,QModelIndex,QTimer,QSize,QPointF,Signal,QSignalBlocker
from PySide6.QtGui import QColor,QFont,QIcon,QPixmap,QPainter,QPolygonF
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QToolButton,QTableView,QHeaderView,QPlainTextEdit,QSizePolicy
from traffic_capture import TrafficCapture,HISTORY_SECONDS,SNAPLEN


def capture_icon(kind):
    # Native standard media icons can be black even with a dark Qt stylesheet.
    icon=QIcon()
    for mode,color in ((QIcon.Mode.Normal,'#f8fafc'),(QIcon.Mode.Disabled,'#6b7c95')):
        pixmap=QPixmap(32,32);pixmap.setDevicePixelRatio(2);pixmap.fill(Qt.GlobalColor.transparent)
        painter=QPainter(pixmap);painter.setRenderHint(QPainter.RenderHint.Antialiasing);painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor(color))
        if kind=='play':painter.drawPolygon(QPolygonF([QPointF(4,2),QPointF(14,8),QPointF(4,14)]))
        elif kind=='pause':painter.drawRect(3,2,4,12);painter.drawRect(9,2,4,12)
        else:painter.drawRect(3,3,10,10)
        painter.end();icon.addPixmap(pixmap,mode)
    return icon


class FramesModel(QAbstractTableModel):
    headers=('Время','Источник','Назначение','Протокол','Байт','Информация')
    def __init__(self,parent=None):super().__init__(parent);self.rows=[]
    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.rows)
    def columnCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.headers)
    def headerData(self,section,orientation,role=Qt.ItemDataRole.DisplayRole):
        if role==Qt.ItemDataRole.DisplayRole and orientation==Qt.Orientation.Horizontal:return self.headers[section]
    def row_for(self,number):
        if number is None:return None
        index=bisect_left(self.rows,number,key=lambda frame:frame.number)
        return index if index<len(self.rows) and self.rows[index].number==number else None
    def replace_rows(self,rows):
        """Rolling capture numbers are immutable and consecutive within a buffer.

        Preserve Qt row identities through append/expiry. A different capture or
        a discontinuous buffer falls back to a reset rather than guessing.
        """
        old=self.rows
        if old and rows and len(old)==old[-1].number-old[0].number+1 and len(rows)==rows[-1].number-rows[0].number+1:
            removed=rows[0].number-old[0].number
            overlap=len(old)-removed
            if 0<=removed<len(old) and 0<overlap<=len(rows) and old[-1].number==rows[overlap-1].number:
                if removed:
                    self.beginRemoveRows(QModelIndex(),0,removed-1);del old[:removed];self.endRemoveRows()
                added=rows[overlap:]
                if added:
                    self.beginInsertRows(QModelIndex(),len(old),len(old)+len(added)-1);old.extend(added);self.endInsertRows()
                return
        self.beginResetModel();self.rows=rows;self.endResetModel()
    def data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row()>=len(self.rows):return None
        frame=self.rows[index.row()]
        if role==Qt.ItemDataRole.DisplayRole:
            column=index.column()
            if column==0:return format_timestamp(frame.ts,milliseconds=True)
            if column==1:return frame.src
            if column==2:return frame.dst
            if column==3:return frame.protocol
            if column==4:return str(frame.length)
            return frame.info
        if role==Qt.ItemDataRole.ToolTipRole:return frame.src+' → '+frame.dst+'\n'+frame.info+f'\nПакет {frame.length} Б; сохранено {len(frame.raw)} Б'
        if role==Qt.ItemDataRole.ForegroundRole:
            return QColor('#f87171' if frame.protocol=='TCP' and 'RST' in frame.info else {'TCP':'#93c5fd','UDP':'#5eead4','ICMP':'#fcd34d','ICMPv6':'#fcd34d','LLDP':'#c4b5fd'}.get(frame.protocol,'#dce6f4'))
        if role==Qt.ItemDataRole.SizeHintRole:return QSize(80,18)


class TrafficPanel(QWidget):
    expansionChanged=Signal(bool)
    def __init__(self):
        super().__init__();self.engine=TrafficCapture();self.available=None;self.interface_name='';self.reason='Дождитесь сетевого адаптера';self.closing=False;self.selected=None;self.expanded=False
        self.setMinimumHeight(44)
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(3)
        header=QHBoxLayout();header.setSpacing(5);self.title=QLabel('Анализа сетевого трафика');self.title.setTextFormat(Qt.TextFormat.PlainText);self.title.setStyleSheet('font-size:11px;font-weight:600;')
        self.title.setToolTip('Перехватывает пакеты и показывает, кто с кем обменивается данными, по каким протоколам и с какими ошибками.');header.addWidget(self.title,1)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);self.title.setMinimumWidth(160)
        self.status=QLabel('Дождитесь сетевого адаптера · запуск вручную');self.status.setStyleSheet('color:#91a2bb;font-size:10px;');self.status.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);header.addWidget(self.status,1)
        self.play_icon=capture_icon('play');self.pause_icon=capture_icon('pause')
        self.play=QToolButton();self.play.setIcon(self.play_icon);self.play.setAccessibleName('Запустить захват пакетов');self.play.setToolTip('Запустить захват на адаптере, выбранном вверху');self.play.clicked.connect(self.start);header.addWidget(self.play)
        self.stop=QToolButton();self.stop.setIcon(capture_icon('stop'));self.stop.setAccessibleName('Остановить захват пакетов');self.stop.setToolTip('Остановить захват; последние пакеты удаляются по истечении 2 минут');self.stop.clicked.connect(self.stop_capture);header.addWidget(self.stop);layout.addLayout(header)
        for button,background in ((self.play,'#2563eb'),(self.stop,'#263449')):
            button.setFixedSize(32,26);button.setIconSize(QSize(16,16))
            button.setStyleSheet('QToolButton{background:'+background+';border:1px solid #344459;border-radius:5px;padding:0;} QToolButton:hover{border-color:#93c5fd;} QToolButton:disabled{background:#141d2b;border-color:#263449;}')
        self.summary=QLabel('История до 2 минут · без записи на диск');self.summary.setStyleSheet('color:#91a2bb;font-size:10px;');self.summary.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);layout.addWidget(self.summary)
        self.table=QTableView();self.model=FramesModel(self.table);self.table.setModel(self.model)
        self.table.verticalHeader().hide();self.table.verticalHeader().setMinimumSectionSize(16);self.table.verticalHeader().setDefaultSectionSize(16)
        self.table.setStyleSheet('QTableView{background:#141d2b;alternate-background-color:#1a2638;selection-background-color:#203f67;selection-color:#f8fafc;border:1px solid #263449;font-size:10px;gridline-color:#263449;} QHeaderView::section{padding:2px;}')
        self.table.setAlternatingRowColors(True);self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows);self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection);self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        for column,width in enumerate((88,160,160,64,44)):
            self.table.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeMode.Interactive);self.table.setColumnWidth(column,width)
        self.table.horizontalHeader().setSectionResizeMode(5,QHeaderView.ResizeMode.Stretch);self.table.clicked.connect(self.inspect);layout.addWidget(self.table,1)
        self.table.selectionModel().currentRowChanged.connect(self.current_packet)
        self.details=QPlainTextEdit();self.details.setReadOnly(True);self.details.setFont(QFont('Menlo',10));self.details.setMaximumHeight(62);self.details.hide();layout.addWidget(self.details)
        self.setToolTip('Это трафик локального адаптера, а не удалённый захват коммутатора. Для чужого трафика нужен SPAN/mirror.\nСохраняется до 256 байт каждого пакета (заголовки и начало данных); размер — полная длина кадра.\nБуфер ограничен 2 минутами, 50 000 пакетов и 32 МиБ. При нагрузке самые старые записи удаляются раньше. Потери захвата не равны сетевым потерям.')
        self.timer=QTimer(self);self.timer.setInterval(500);self.timer.timeout.connect(self.refresh);self.timer.start();self.refresh()
    def compact_height(self):return self.layout().minimumSize().height()
    def sizeHint(self):return QSize(800,self.compact_height())
    def set_source(self,local,neighbor):
        source=(local.get('capture'),neighbor.get('switch'),neighbor.get('port'))
        self.available=source if source[0] else None
        self.interface_name=local.get('name') or local.get('capture') or ''
        # LLDP describes the switch port; it is not a prerequisite for capturing
        # this local device. Losing/changing LLDP must not stop its pcap reader.
        current=self.engine.source[0] if self.engine.source else None
        if self.engine.state in ('starting','running','paused') and source[0]!=current:
            self.engine.stop();self.reason='Подключение изменилось · захват остановлен'
        elif not self.available:self.reason='Нет выбранного адаптера · запуск недоступен'
        else:self.reason='Готово · запуск вручную'
        self.refresh()
    def start(self):
        try:
            if self.engine.state in ('starting','running','paused'):self.engine.pause()
            elif self.available:self.engine.start(*self.available);self.selected=None;self.details.clear();self.details.hide()
        except ValueError:self.reason='Предыдущий захват завершается'
        self.refresh()
    def stop_capture(self):self.engine.stop();self.reason='Захват остановлен · история до 2 минут';self.refresh()
    def inspect(self,index):
        if not index.isValid() or index.row()>=len(self.model.rows):return
        frame=self.model.rows[index.row()]
        if frame.number==self.selected:return
        self.selected=frame.number
        lines=[f'{frame.src} → {frame.dst} · {frame.protocol} · {frame.length} Б · сохранено {len(frame.raw)} Б',frame.info]
        for offset in range(0,len(frame.raw),16):
            block=frame.raw[offset:offset+16];chars=''.join(chr(b) if 32<=b<127 else '.' for b in block)
            lines.append(f'{offset:04x}  '+block.hex(' ').ljust(47)+'  '+chars)
        self.details.setPlainText('\n'.join(lines));self.details.show()
    def current_packet(self,index,previous):
        if self.table.hasFocus():self.inspect(index)
    def refresh(self):
        if self.closing:return
        snapshot=self.engine.snapshot();rows=snapshot['frames'];state=snapshot['state'];busy=state in ('starting','running','paused','stopping')
        expanded=bool(rows or busy);self.table.show()
        self.table.setMinimumHeight(5*self.table.verticalHeader().defaultSectionSize()+self.table.horizontalHeader().height()+2*self.table.frameWidth()+self.table.horizontalScrollBar().sizeHint().height())
        self.setMinimumHeight(self.compact_height())
        if expanded!=self.expanded:self.expanded=expanded;self.expansionChanged.emit(expanded)
        running=state in ('starting','running')
        active=state in ('starting','running','paused')
        self.play.setEnabled(active or (bool(self.available) and not snapshot['worker_active'] and state!='stopping'))
        self.stop.setEnabled(snapshot['worker_active'] or active)
        self.play.setIcon(self.pause_icon if running else self.play_icon)
        self.play.setAccessibleName('Приостановить захват пакетов' if running else 'Продолжить захват пакетов' if state=='paused' else 'Запустить захват пакетов')
        source=snapshot['source'] if snapshot['source'] and (rows or busy) else self.available
        name=self.interface_name if source and self.available and source[0]==self.available[0] else source[0] if source else ''
        adapter='Адаптер: '+str(name)+' ('+str(source[0])+')' if source else 'Адаптер выбран вверху; LLDP для захвата не требуется'
        self.play.setToolTip(self.play.accessibleName()+' · история не старше 2 минут\n'+adapter)
        text={'starting':'Открываем адаптер…','running':'● Захват','paused':'ПАУЗА · история до 2 минут','stopping':'Завершаем…','stopped':'Остановлено · история до 2 минут' if self.available else self.reason,'error':snapshot['error']}.get(state,self.reason)
        if rows and state not in ('starting','running','paused','stopping'):
            text+=' · последние пакеты'
        self.status.setText(text);self.status.setToolTip(text+'\n'+adapter)
        drops=snapshot['capture_stats'];lost=str(drops['dropped']) if drops else '—'
        self.summary.setText(f"{snapshot['mbps']:.2f} Мбит/с · {snapshot['pps']} пак/с · за 2 мин: {snapshot['packets']} пак, {snapshot['bytes']/1e6:.2f} МБ · пропущено захватом: {lost}"+(' · буфер ограничен' if snapshot['evicted'] else ''))
        self.summary.setToolTip('Наблюдаемые кадры, включая входящие и исходящие. Скорость рассчитана по полным длинам кадров за последнюю завершённую секунду.\nСчётчики за последние 2 минуты; пропуски — счётчик libpcap с начала захвата, а не PL%.\nТаблица ограничена памятью и может содержать меньше пакетов, чем счётчик трафика.')
        changed=len(rows)!=len(self.model.rows) or (rows and self.model.rows and (rows[0].number!=self.model.rows[0].number or rows[-1].number!=self.model.rows[-1].number))
        if changed:
            bar=self.table.verticalScrollBar();follow=bar.value()>=bar.maximum()-1;position=bar.value()
            anchor=self.model.rows[self.table.indexAt(self.table.viewport().rect().topLeft()).row()].number if self.model.rows and self.table.indexAt(self.table.viewport().rect().topLeft()).isValid() else None
            with QSignalBlocker(self.table.selectionModel()):
                self.model.replace_rows(rows)
                selected=self.model.row_for(self.selected)
                if selected is not None:self.table.selectRow(selected)
                elif self.selected is not None:self.selected=None;self.details.clear();self.details.hide()
            anchor_row=self.model.row_for(anchor)
            if follow:self.table.scrollToBottom()
            elif anchor_row is not None:self.table.scrollTo(self.model.index(anchor_row,0),QTableView.ScrollHint.PositionAtTop)
            else:bar.setValue(position)
    def shutdown(self):
        self.closing=True;self.timer.stop();self.engine.close();self.model.beginResetModel();self.model.rows=[];self.model.endResetModel();self.details.clear();self.details.hide()

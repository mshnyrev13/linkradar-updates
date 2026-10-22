from qt_support import close_window,flush_io,production_application,settle_layout
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys,struct,tempfile,threading,time,unittest,gc
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import traffic_capture as capture,traffic_widgets as ui,desktop
from PySide6.QtCore import QCoreApplication,QEvent,QSize
from PySide6.QtGui import QIcon


def tcp_frame(vlan=False):
    ethernet=bytes.fromhex('001122334455aabbccddeeff')+(bytes.fromhex('810000640800') if vlan else bytes.fromhex('0800'))
    ip=bytearray(20);ip[0]=0x45;ip[9]=6;ip[12:16]=bytes((192,0,2,1));ip[16:20]=bytes((192,0,2,2));ip[2:4]=struct.pack('!H',40)
    tcp=struct.pack('!HHIIBBHHH',443,12345,123,99,0x50,0x14,2048,0,0)
    return ethernet+ip+tcp


def eventually(predicate):
    deadline=time.monotonic()+3
    while not predicate():
        if time.monotonic()>=deadline:raise AssertionError('Worker did not reach expected state')
        time.sleep(.005)


class PauseCapture(unittest.TestCase):
    def test_stop_does_not_restore_stopping_after_worker_has_finished_cleanup(self):
        engine=capture.TrafficCapture();engine.worker=Mock();engine.worker.is_alive.return_value=True;engine.state='stopped'
        engine.stop();self.assertEqual(engine.state,'stopped');self.assertTrue(engine.stop_event.is_set())
    def exercise_pause(self,rapid=False):
        in_read=threading.Event();release=threading.Event();closed=threading.Event();calls=[0]
        old=Mock();old.stats.return_value={'received':2,'dropped':1,'interface_dropped':0};old.close.side_effect=closed.set
        def read_old():
            calls[0]+=1
            if calls[0]==1:return {'ts':1,'length':54,'raw':tcp_frame()}
            in_read.set();release.wait(3)
            return {'ts':2,'length':999,'raw':tcp_frame()}
        old.read_packet.side_effect=read_old
        resumed=Mock();resumed.stats.return_value={'received':1,'dropped':0,'interface_dropped':0};remaining=[{'ts':3,'length':100,'raw':tcp_frame()}]
        resumed.read_packet.side_effect=lambda:remaining.pop() if remaining else None
        factory=Mock(side_effect=[old,resumed]);engine=capture.TrafficCapture(capture_factory=factory)
        try:
            engine.start('en4','switch','Gi1/0/7');self.assertTrue(in_read.wait(3));self.assertEqual(len(engine.snapshot()['frames']),1)
            self.assertTrue(engine.pause());self.assertEqual(engine.snapshot()['state'],'paused');self.assertEqual(engine.snapshot()['pps'],0)
            if rapid:self.assertFalse(engine.pause())
            release.set();self.assertTrue(closed.wait(3))
            if not rapid:
                self.assertEqual(factory.call_count,1);self.assertEqual(len(engine.snapshot()['frames']),1)
                self.assertFalse(engine.pause())
            eventually(lambda:len(engine.snapshot()['frames'])==2)
            self.assertEqual([frame.ts for frame in engine.snapshot()['frames']],[1,3]);self.assertEqual(factory.call_count,2)
            self.assertEqual(engine.source,('en4','switch','Gi1/0/7'))
            engine.stop();engine.worker.join(3);self.assertFalse(engine.worker.is_alive())
            self.assertEqual(engine.snapshot()['capture_stats']['received'],3);self.assertEqual(engine.snapshot()['capture_stats']['dropped'],1)
        finally:release.set();engine.close()
    def test_pause_closes_reader_resume_preserves_history_discards_inflight_packet(self):self.exercise_pause()
    def test_rapid_pause_resume_discards_packets_from_previous_handle(self):self.exercise_pause(rapid=True)
    def test_paused_history_expires_and_stop_terminates_without_reopening(self):
        now=[1000.];closed=threading.Event();backend=Mock();backend.read_packet.return_value=None;backend.stats.return_value=None;backend.close.side_effect=closed.set
        factory=Mock(return_value=backend);engine=capture.TrafficCapture(clock=lambda:now[0],capture_factory=factory)
        try:
            engine.start('en4','switch','Gi1/0/7');eventually(lambda:engine.state=='running')
            engine.add({'ts':1,'length':54,'raw':tcp_frame()});engine.pause();self.assertTrue(closed.wait(3))
            now[0]=1120.;self.assertEqual(engine.snapshot()['frames'],[]);self.assertEqual(engine.state,'paused')
            engine.stop();engine.worker.join(3);self.assertFalse(engine.worker.is_alive());self.assertEqual(engine.state,'stopped');factory.assert_called_once()
        finally:engine.close()
    def test_stats_failure_during_close_does_not_leave_worker_in_stopping_state(self):
        backend=Mock();backend.read_packet.return_value=None;backend.stats.side_effect=OSError('stats unavailable')
        engine=capture.TrafficCapture(capture_factory=lambda *args,**kwargs:backend)
        try:
            engine.start('en4','switch','Gi1/0/7');eventually(lambda:engine.state=='running');engine.stop();engine.worker.join(3)
            self.assertFalse(engine.worker.is_alive());self.assertEqual(engine.state,'stopped');backend.close.assert_called_once()
        finally:engine.close()


class Decoding(unittest.TestCase):
    def test_real_tcp_fields_vlan_and_safe_truncated_headers(self):
        raw=tcp_frame(True);src,dst,protocol,info=capture.decode(raw)
        self.assertEqual(src,'192.0.2.1:443');self.assertEqual(dst,'192.0.2.2:12345');self.assertEqual(protocol,'TCP')
        self.assertIn('VLAN 100',info);self.assertIn('RST, ACK',info);self.assertIn('Seq=123 Ack=99',info)
        for size in range(len(raw)):self.assertEqual(len(capture.decode(raw[:size])),4)
    def test_ipv6_extension_udp_and_noninitial_fragment(self):
        ethernet=bytes.fromhex('001122334455aabbccddeeff86dd');ip=bytearray(40);ip[0]=0x60;ip[6]=0;ip[8:24]=bytes.fromhex('20010db8000000000000000000000001');ip[24:40]=bytes.fromhex('20010db8000000000000000000000002')
        raw=ethernet+ip+bytes((17,0,0,0,0,0,0,0))+struct.pack('!HHHH',5004,5005,8,0)
        self.assertEqual(capture.decode(raw)[:3],('[2001:db8::1]:5004','[2001:db8::2]:5005','UDP'))
        ip[6]=44;fragment=bytes((17,0,0,8,0,0,0,1));self.assertIn('Фрагмент',capture.decode(ethernet+ip+fragment)[3])


class RollingHistory(unittest.TestCase):
    def test_retention_expires_after_stop_with_monotonic_clock_and_full_wire_lengths(self):
        now=[0.];engine=capture.TrafficCapture(clock=lambda:now[0]);raw=tcp_frame()+bytes(1000)
        engine.add({'ts':100000,'length':1500,'raw':raw});now[0]=1.2;engine.state='running'
        result=engine.snapshot();self.assertEqual(result['mbps'],.012);self.assertEqual(result['pps'],1)
        self.assertEqual(len(result['frames'][0].raw),256);self.assertEqual(result['frames'][0].length,1500)
        engine.stop();engine.state='stopped';now[0]=119.9;self.assertEqual(len(engine.snapshot()['frames']),1)
        now[0]=120.;result=engine.snapshot();self.assertEqual(result['frames'],[]);self.assertEqual(result['memory'],0);self.assertEqual(result['packets'],0)
    def test_packet_and_memory_caps_without_disk_or_unbounded_history(self):
        now=[1000.];engine=capture.TrafficCapture(clock=lambda:now[0])
        with patch.object(capture,'MAX_PACKETS',3),patch.object(capture,'MAX_MEMORY',100000):
            for i in range(10):engine.add({'ts':i,'length':54,'raw':tcp_frame()})
            result=engine.snapshot();self.assertEqual(len(result['frames']),3);self.assertEqual(result['packets'],10);self.assertEqual(result['evicted'],7)
        with patch.object(capture,'MAX_MEMORY',500):
            engine.add({'ts':10,'length':54,'raw':tcp_frame()});self.assertLessEqual(engine.snapshot()['memory'],500)
        now[0]=1121;self.assertEqual(engine.snapshot()['frames'],[]);self.assertEqual(engine.snapshot()['packets'],0)
    def test_manual_start_stop_owns_and_closes_pcap_handle(self):
        closed=threading.Event();backend=Mock();backend.read_packet.return_value=None;backend.close.side_effect=closed.set
        factory=Mock(return_value=backend);engine=capture.TrafficCapture(capture_factory=factory)
        self.assertEqual(engine.state,'idle');factory.assert_not_called()
        engine.start('en4','switch','Gi1/0/7');deadline=time.monotonic()+2
        while engine.state=='starting' and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(engine.state,'running');factory.assert_called_once_with('en4',snaplen=256,promiscuous=True)
        engine.close();self.assertTrue(closed.is_set());self.assertFalse(engine.worker.is_alive());self.assertEqual(engine.snapshot()['frames'],[])


class CaptureControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=production_application(cls)
    def test_usb_selection_survives_disconnect_and_stale_other_interface_lldp_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'):
            window=desktop.Window(Path(folder));window.discovery.selected=None
            wifi={'id':'en0','name':'Wi-Fi','capture':'en0','ip':'192.0.2.1'};usb={'id':'en7','name':'USB Ethernet','capture':'en7','ip':'192.0.2.2'}
            neighbor={'switch':'switch','port':'Gi1/0/7'}
            try:
                with patch.object(window.port_panel,'lldp'):
                    window.show_network({'interfaces':[wifi,usb],'local':wifi,'neighbor':None,'lldp_status':'Нет LLDP'})
                    window.nic.setCurrentIndex(window.nic.findData('en7'))
                    window.show_network({'interfaces':[wifi,usb],'local':usb,'neighbor':neighbor,'lldp_status':'LLDP получен'})
                    self.assertTrue(window.traffic_panel.play.isEnabled())
                    window.show_network({'interfaces':[wifi],'local':{},'neighbor':None,'lldp_status':'Нет интерфейса'})
                    self.assertEqual(window.nic.currentData(),'en7');self.assertEqual(window.discovery.selected,'en7');self.assertIn('нет подключения',window.nic.currentText())
                    self.assertFalse(window.traffic_panel.play.isEnabled())
                    window.show_network({'interfaces':[wifi,usb],'local':wifi,'neighbor':{'switch':'wrong','port':'wrong'},'lldp_status':'LLDP получен'})
                    self.assertEqual(window.nic.currentData(),'en7');self.assertIsNone(window.traffic_panel.available);self.assertEqual(window.network_fields['switch'].text(),'—')
                    window.show_network({'interfaces':[wifi,usb],'local':usb,'neighbor':neighbor,'lldp_status':'LLDP получен'})
                    self.assertTrue(window.traffic_panel.play.isEnabled());self.assertFalse(window.traffic_panel.stop.isEnabled());self.assertEqual(window.traffic_panel.engine.state,'idle')
            finally:close_window(window);window.store.db.close();window.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()
    def test_disconnect_stops_collection_keeps_history_and_reconnect_requires_manual_start(self):
        panel=ui.TrafficPanel();closed=threading.Event();backend=Mock();backend.stats.return_value=None;backend.close.side_effect=closed.set
        backend.read_packet.side_effect=lambda:{'ts':time.time(),'length':54,'raw':tcp_frame()}
        factory=Mock(return_value=backend);panel.engine.capture_factory=factory
        try:
            local={'capture':'en7'};neighbor={'switch':'switch','port':'Gi1/0/7'};panel.set_source(local,neighbor);panel.play.click()
            eventually(lambda:len(panel.engine.snapshot()['frames'])>2)
            panel.set_source({},{});self.assertTrue(closed.wait(3));panel.engine.worker.join(3);panel.refresh()
            number=panel.engine.number;self.assertGreater(panel.model.rowCount(),0);self.assertFalse(panel.stop.isEnabled());self.assertIn('последние пакеты',panel.status.text())
            panel.set_source(local,{});time.sleep(.03);panel.refresh();self.assertEqual(panel.engine.number,number);self.assertTrue(panel.play.isEnabled())
            panel.set_source(local,neighbor);self.assertTrue(panel.play.isEnabled());self.assertFalse(panel.stop.isEnabled());factory.assert_called_once();self.assertEqual(panel.engine.number,number)
        finally:panel.shutdown();panel.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()
    def test_packet_panel_can_grow_beyond_old_height_limit(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'):
            window=desktop.Window(Path(folder));window.resize(1100,1250);window.show();settle_layout(self.app)
            try:
                panel=window.traffic_panel
                for i in range(40):panel.engine.add({'ts':i,'length':54,'raw':tcp_frame()})
                panel.refresh();self.app.processEvents();before=panel.table.viewport().height()
                total=sum(window.traffic_splitter.sizes());window.traffic_splitter.setSizes([400,max(1,total-400)]);self.app.processEvents()
                self.assertGreater(panel.height(),235);self.assertGreater(panel.table.viewport().height(),before);self.assertTrue(window.traffic_splitter.handle(1).isVisible())
                self.assertLess(panel.play.geometry().right(),panel.stop.geometry().left())
                self.assertEqual(panel.model.rowCount(),40);self.assertGreater(panel.table.verticalScrollBar().maximum(),0)
                previous=window.height();window.resize(1100,1400);self.app.processEvents();self.assertGreater(window.height(),previous)
            finally:close_window(window);window.store.db.close();window.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()
    def test_alternate_packet_rows_and_selection_remain_dark(self):
        old_style=self.app.styleSheet();self.app.setStyleSheet(desktop.STYLE);panel=ui.TrafficPanel()
        try:
            raw=bytearray(tcp_frame());raw[23]=17
            for i in range(6):panel.engine.add({'ts':i,'length':54,'raw':raw})
            panel.resize(900,300);panel.show();panel.refresh();self.app.processEvents()
            for row in (0,1):
                rect=panel.table.visualRect(panel.model.index(row,4));color=panel.table.viewport().grab().toImage().pixelColor(rect.left()+2,rect.top()+2)
                self.assertLess(max(color.red(),color.green(),color.blue()),100)
            panel.table.selectRow(1);self.app.processEvents();rect=panel.table.visualRect(panel.model.index(1,4))
            color=panel.table.viewport().grab().toImage().pixelColor(rect.left()+2,rect.top()+2);self.assertLess(max(color.red(),color.green(),color.blue()),120)
        finally:panel.shutdown();panel.close();panel.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect();self.app.setStyleSheet(old_style)
    def test_usb_selection_lldp_enables_visible_high_contrast_play_without_autostart(self):
        old_style=self.app.styleSheet();self.app.setStyleSheet(desktop.STYLE)
        try:
            with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),patch.object(capture.TrafficCapture,'start') as start:
                window=desktop.Window(Path(folder));window.resize(800,700);window.show();self.app.processEvents()
                try:
                    wifi={'id':'en0','name':'Wi-Fi','capture':'en0','ip':'192.0.2.1'}
                    usb={'id':'en7','name':'USB Ethernet','capture':'en7','ip':'192.0.2.2'}
                    with patch.object(window.port_panel,'lldp'):
                        window.show_network({'interfaces':[wifi,usb],'local':wifi,'neighbor':None,'lldp_status':'Нет LLDP'})
                        window.nic.setCurrentIndex(window.nic.findData('en7'));self.assertFalse(window.traffic_panel.play.isEnabled())
                        window.show_network({'interfaces':[wifi,usb],'local':usb,'neighbor':{'switch':'switch-b.example.test','port':'GigabitEthernet1/0/7'},'lldp_status':'LLDP получен'})
                    self.app.processEvents();panel=window.traffic_panel
                    start.assert_not_called();self.assertEqual(panel.engine.state,'idle');self.assertTrue(panel.play.isVisible());self.assertTrue(panel.play.isEnabled());self.assertFalse(panel.stop.isEnabled())
                    self.assertLess(panel.status.geometry().right(),panel.play.geometry().left());self.assertLess(panel.play.geometry().right(),panel.stop.geometry().left())
                    icon=panel.play.icon().pixmap(QSize(16,16),QIcon.Mode.Normal).toImage();scale=icon.devicePixelRatio()
                    color=icon.pixelColor(int(7*scale),int(8*scale));self.assertGreater(color.red(),220);self.assertGreater(color.green(),220);self.assertGreater(color.alpha(),220)
                    panel.play.click();start.assert_called_once_with('en7','switch-b.example.test','GigabitEthernet1/0/7')
                finally:close_window(window);window.store.db.close();window.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()
        finally:self.app.setStyleSheet(old_style)
    def test_play_pause_resume_preserves_selection_and_source_change_stops_pause(self):
        panel=ui.TrafficPanel()
        try:
            panel.set_source({'capture':'en4'},{'switch':'switch','port':'Gi1/0/7'})
            panel.engine.source=panel.available;panel.engine.state='running'
            panel.engine.add({'ts':1,'length':54,'raw':tcp_frame()});panel.refresh();panel.inspect(panel.model.index(0,0));selected=panel.selected;details=panel.details.toPlainText()
            panel.play.click();self.assertEqual(panel.engine.state,'paused');self.assertIn('Продолжить',panel.play.accessibleName());self.assertTrue(panel.stop.isEnabled())
            self.assertEqual(panel.selected,selected);self.assertEqual(panel.details.toPlainText(),details)
            panel.set_source({'capture':'en4'},{});self.assertEqual(panel.engine.state,'paused')
            panel.play.click();self.assertEqual(panel.engine.state,'starting');self.assertEqual(len(panel.model.rows),1);self.assertEqual(panel.selected,selected)
            panel.play.click();self.assertEqual(panel.engine.state,'paused')
            with patch.object(panel.engine,'stop') as stop:
                panel.set_source({'capture':'en5'},{'switch':'other','port':'Gi1/0/8'});stop.assert_called_once()
        finally:panel.shutdown();panel.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()
    def test_lldp_maps_exact_local_interface_no_automatic_start_and_source_change_stops(self):
        panel=ui.TrafficPanel()
        try:
            self.assertFalse(panel.play.isEnabled());self.assertFalse(panel.stop.isEnabled())
            with patch.object(panel.engine,'start') as start,patch.object(panel.engine,'stop') as stop:
                panel.set_source({'capture':'en4'},{'switch':'switch','port':'Gi1/0/7'});self.assertTrue(panel.play.isEnabled());start.assert_not_called()
                panel.play.click();start.assert_called_once_with('en4','switch','Gi1/0/7')
                panel.engine.source=panel.available;panel.engine.state='running';panel.refresh();self.assertTrue(panel.stop.isEnabled());self.assertTrue(panel.play.isEnabled());self.assertIn('Приостановить',panel.play.accessibleName())
                panel.set_source({'capture':'en4'},{'switch':'switch','port':'Gi1/0/7'});stop.assert_not_called()
                panel.set_source({'capture':'en5'},{'switch':'other','port':'Gi1/0/8'});stop.assert_called_once();self.assertEqual(start.call_count,1)
        finally:panel.shutdown();panel.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()
    def test_top_selector_binds_pcap_without_lldp_and_switch_requires_manual_restart(self):
        backend=Mock();backend.read_packet.return_value=None;backend.stats.return_value=None
        factory=Mock(return_value=backend)
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'):
            window=desktop.Window(Path(folder));window.timer.stop();panel=window.traffic_panel;panel.engine.capture_factory=factory
            wifi={'id':'en0','name':'Wi-Fi','capture':'en0'};usb={'id':'en7','name':'USB Ethernet','capture':'en7'}
            def publish(local,neighbor=None):
                window.show_network({'interfaces':[wifi,usb],'local':local,'neighbor':neighbor,'lldp_status':'Нет LLDP'})
            try:
                with patch.object(window.port_panel,'lldp'):
                    publish(wifi);window.nic.setCurrentIndex(window.nic.findData('en7'))
                    publish(usb);self.assertTrue(panel.play.isEnabled());factory.assert_not_called()
                    panel.play.click();eventually(lambda:panel.engine.state=='running')
                    factory.assert_called_once_with('en7',snaplen=256,promiscuous=True)
                    self.assertIn('USB Ethernet',panel.play.toolTip())
                    publish(usb,{'switch':'switch','port':'Gi1/0/7'});publish(usb)
                    self.assertEqual(panel.engine.state,'running');factory.assert_called_once()
                    window.nic.setCurrentIndex(window.nic.findData('en0'));panel.engine.worker.join(3)
                    self.assertEqual(panel.engine.state,'stopped');backend.close.assert_called_once()
                    publish(usb);self.assertIsNone(panel.available);self.assertFalse(panel.play.isEnabled())
                    publish(wifi);panel.refresh();self.assertTrue(panel.play.isEnabled());factory.assert_called_once()
                    panel.play.click();eventually(lambda:factory.call_count==2)
                    self.assertEqual(factory.call_args.args,('en0',))
            finally:close_window(window);window.store.db.close();window.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()
    def test_stopped_packet_inspection_disappears_when_two_minutes_expire(self):
        panel=ui.TrafficPanel();now=[1000.];panel.engine.clock=lambda:now[0]
        try:
            panel.engine.add({'ts':1000,'length':54,'raw':tcp_frame()});panel.refresh();panel.inspect(panel.model.index(0,0))
            self.assertIn('Seq=123',panel.details.toPlainText());self.assertIn('0000',panel.details.toPlainText())
            now[0]=1120;panel.refresh();self.assertEqual(panel.model.rowCount(),0);self.assertEqual(panel.details.toPlainText(),'')
        finally:panel.shutdown();panel.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()
    def test_panel_after_graphene_has_five_rows_and_speed_is_at_bottom_of_sidebar(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),patch.object(capture.TrafficCapture,'start') as start:
            window=desktop.Window(Path(folder));window.show();self.app.processEvents();start.assert_not_called()
            self.assertLess(window.traffic_panel.title.geometry().right(),window.traffic_panel.status.geometry().left())
            self.assertLess(window.port_panel.mapTo(window,window.port_panel.rect().topLeft()).y(),window.traffic_panel.mapTo(window,window.traffic_panel.rect().topLeft()).y())
            self.assertTrue(window.traffic_panel.table.isVisible())
            self.assertGreaterEqual(window.traffic_panel.table.viewport().height(),5*window.traffic_panel.table.verticalHeader().defaultSectionSize())
            self.assertEqual(window.speed_button.parent(),window.speed_readout.parent())
            self.assertEqual(window.speed_button.parent(),window.address.parent())
            self.assertGreater(window.speed_button.mapTo(window,window.speed_button.rect().topLeft()).y(),window.traffic_panel.mapTo(window,window.traffic_panel.rect().bottomLeft()).y())
            self.assertEqual(window.speed_button.geometry().center().y(),window.speed_readout.geometry().center().y())
            self.assertLess(window.speed_button.geometry().right(),window.speed_readout.geometry().left())
            self.assertLessEqual(window.speed_readout.geometry().right(),window.address.parent().width())
            self.assertLessEqual(window.speed_button.parent().height()-window.speed_button.geometry().bottom(),2)
            close_window(window);window.store.db.close();window.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()


if __name__=='__main__':unittest.main()

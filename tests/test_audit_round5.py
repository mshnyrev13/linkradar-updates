"""Round 5: dormant views, fresh reports and portable time labels; synthetic only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys,time,tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QPointF
from PySide6.QtGui import QImage,QPainter
from PySide6.QtWidgets import QApplication
from types import SimpleNamespace
import desktop,stream_widgets,port_widgets,server
from qt_support import close_window,flush_io


class RoundFive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def window(self,folder):
        with patch.object(desktop.Discovery,'start'):
            w=desktop.Window(Path(folder),service_folder=False)
        w.timer.stop();return w

    def dispose(self,w):
        close_window(w);w.store.db.close();w.deleteLater();self.app.processEvents()

    def test_hidden_network_timer_skips_snapshot_and_return_refreshes(self):
        with tempfile.TemporaryDirectory() as folder:
            w=self.window(folder)
            try:
                w.show();self.app.processEvents();w.tabs.setCurrentIndex(1)
                with patch.object(w,'refresh') as refresh:
                    w.timer.timeout.emit();refresh.assert_not_called()
                    w.tabs.setCurrentIndex(0);self.assertTrue(refresh.called)
                w.tabs.setCurrentIndex(1);w.pending_restart=True
                with patch.object(w,'refresh') as refresh:
                    w.timer.timeout.emit();refresh.assert_called_once()
                w.pending_restart=False
            finally:self.dispose(w)

    def test_hidden_stream_timer_skips_snapshot_but_manual_refresh_reads(self):
        panel=stream_widgets.StreamPanel()
        try:
            with patch.object(panel.monitor,'snapshot',wraps=panel.monitor.snapshot) as snapshot:
                panel.timer.timeout.emit();snapshot.assert_not_called()
                panel.refresh();snapshot.assert_called_once()
                panel.show();self.app.processEvents();self.assertGreater(snapshot.call_count,1)
        finally:panel.shutdown();panel.close();panel.deleteLater();self.app.processEvents()

    def test_hidden_network_still_expires_database_history(self):
        with tempfile.TemporaryDirectory() as folder:
            w=self.window(folder);now=[20000];w.store.clock=lambda:now[0]
            try:
                config=server.validate_config({'targets':[{'host':'192.0.2.1'}]})
                w.store.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',('s',now[0],None,'running',json.dumps(config)))
                w.store.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',('s',config['targets'][0]['id'],now[0],'ok',10,'192.0.2.1',''))
                now[0]+=server.RETENTION_SECONDS+1;w.tabs.setCurrentIndex(1)
                w.timer.timeout.emit();flush_io(w)
                self.assertEqual(w.store.db.execute('SELECT count(*) FROM samples').fetchone()[0],0)
            finally:self.dispose(w)

    def test_hidden_stream_retention_and_view_cache_are_bounded(self):
        panel=stream_widgets.StreamPanel();panel.monitor.clock=lambda:20000
        try:
            panel.monitor.update_obs(20000,dict(stream=dict(outputActive=True,outputBytes=1),stats={},video={}))
            panel.monitor.sample('obs:stream',20000)
            panel.selected='obs:stream';panel.show();self.app.processEvents()
            self.assertTrue(panel.plots[0].points)
            panel.hide();self.assertFalse(panel.plots[0].points);self.assertEqual(panel.data,{})
            panel.monitor.clock=lambda:20000+panel.monitor.history_seconds+1
            panel.timer.timeout.emit();self.assertEqual(len(panel.monitor.history.get('obs:stream',())),0)
        finally:panel.shutdown();panel.close();panel.deleteLater();self.app.processEvents()

    def test_switching_stream_tab_keeps_event_reading_position_and_selection(self):
        panel=stream_widgets.StreamPanel();panel.monitor.clock=lambda:20000
        try:
            panel.monitor.events.extend(dict(ts=19900+i*.1,key='',severity='info',message='synthetic event '+str(i)) for i in range(300))
            panel.resize(900,640);panel.show();self.app.processEvents()
            cursor=panel.events.textCursor();cursor.setPosition(20);cursor.setPosition(30,cursor.MoveMode.KeepAnchor);panel.events.setTextCursor(cursor)
            bar=panel.events.verticalScrollBar();bar.setValue(20);position=bar.value();selection=panel.events.textCursor().selectedText()
            panel.hide();panel.show();self.app.processEvents()
            self.assertEqual(panel.events.textCursor().selectedText(),selection);self.assertEqual(bar.value(),position)
        finally:panel.shutdown();panel.close();panel.deleteLater();self.app.processEvents()

    def test_report_reads_latest_snapshot_instead_of_hidden_stale_view(self):
        with tempfile.TemporaryDirectory() as folder:
            w=self.window(folder)
            try:
                w.sid='synthetic'
                latest={'targets':[dict(mode='icmp',host='192.0.2.1',window_stats=dict(avg=77,max=88,loss=0),window_last={'jitter':3},hops=[])]}
                with patch.object(w.monitor,'snapshot',return_value=latest) as snapshot:
                    text='\n'.join(w.network_report())
                snapshot.assert_called_once();self.assertIn('77,0',text);self.assertIn('3,0',text)
            finally:self.dispose(w)

    def test_unavailable_local_time_does_not_abort_native_graph_paint_or_hover(self):
        graphs=[desktop.Graph(),stream_widgets.SeriesPlot('RTT',[dict(key='rtt',label='RTT',color='#60a5fa')])]
        graphs[0].set_data([dict(ts=1000,status='ok',latency=10,jitter=1)],1000,300)
        graphs[1].set_data([(1000,dict(rtt=10),'live')],1000,300)
        for chart in graphs:
            with self.subTest(chart=type(chart).__name__):
                chart.resize(320,180);image=QImage(320,180,QImage.Format.Format_ARGB32);p=QPainter(image)
                try:
                    with patch('time.localtime',side_effect=OSError('timestamp outside platform range')):
                        chart.draw_chart(p)
                        chart.mouseMoveEvent(SimpleNamespace(position=lambda:QPointF(100,60)))
                    self.assertIn('—',chart.toolTip())
                finally:p.end();chart.deleteLater()

    def test_timestamp_labels_keep_valid_values_and_refuse_invalid_input(self):
        from time_labels import format_timestamp
        stamp=1800000000.125
        self.assertEqual(format_timestamp(stamp),time.strftime('%H:%M:%S',time.localtime(stamp)))
        self.assertTrue(format_timestamp(stamp,milliseconds=True).endswith('.125'))
        self.assertEqual(format_timestamp(stamp,utc=True),time.strftime('%H:%M:%S',time.gmtime(stamp)))
        for value in (None,True,float('nan'),float('inf'),10**1000,'broken'):
            with self.subTest(value=repr(value)[:30]):self.assertEqual(format_timestamp(value),'—')

    def test_packet_model_time_keeps_milliseconds_and_handles_os_range_error(self):
        import traffic_widgets
        model=traffic_widgets.FramesModel();model.replace_rows([SimpleNamespace(number=1,ts=1800000000.125)])
        try:
            self.assertTrue(model.data(model.index(0,0)).endswith('.125'))
            with patch('time.localtime',side_effect=OSError('unsupported date')):
                self.assertEqual(model.data(model.index(0,0)),'—')
        finally:model.deleteLater()

    def test_graphene_axes_and_hover_handle_unavailable_local_time(self):
        chart=port_widgets.PortPlot('Трафик',('rx','tx'));chart.resize(320,180)
        chart.data=dict(begin=700,end=1000,step=5,series={'rx':[(700,1),(1000,2)],'tx':[(700,2),(1000,3)]})
        image=QImage(320,180,QImage.Format.Format_ARGB32);p=QPainter(image)
        try:
            with patch('time.localtime',side_effect=OSError('unsupported date')):
                chart.draw_chart(p);chart.mouseMoveEvent(SimpleNamespace(position=lambda:QPointF(100,60)))
            self.assertIn('—',chart.toolTip())
        finally:p.end();chart.deleteLater()

    def test_stream_report_marks_unavailable_network_without_exception_text(self):
        def fail():raise OSError('synthetic internal failure')
        panel=stream_widgets.StreamPanel(network_report=fail)
        try:
            report=panel.report_text();self.assertIn('Не удалось получить свежие сетевые измерения',report)
            self.assertNotIn('synthetic internal failure',report)
        finally:panel.shutdown();panel.deleteLater()

    def test_stream_export_never_overwrites_a_neighboring_tmp_or_its_hardlink(self):
        panel=stream_widgets.StreamPanel()
        try:
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);target=root/'report.txt';victim=root/'keep.txt';temporary=root/'report.txt.tmp'
                victim.write_text('keep this file',encoding='utf-8');os.link(victim,temporary)
                from qt_support import flush_report
                with patch.object(stream_widgets.QFileDialog,'getSaveFileName',return_value=(str(target),'')),patch.object(panel.monitor,'report',return_value='Учебный отчёт'):
                    panel.save_report();flush_report(panel)
                self.assertEqual(victim.read_text(encoding='utf-8'),'keep this file')
                self.assertEqual(temporary.read_text(encoding='utf-8'),'keep this file')
                self.assertEqual(target.read_text(encoding='utf-8'),'Учебный отчёт')
        finally:panel.shutdown();panel.deleteLater()

    def test_failed_stream_export_keeps_target_and_cleans_only_its_temporary(self):
        import export_transfer
        panel=stream_widgets.StreamPanel()
        try:
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);target=root/'report.txt';target.write_text('old report',encoding='utf-8')
                from qt_support import flush_report
                with patch.object(stream_widgets.QFileDialog,'getSaveFileName',return_value=(str(target),'')),patch.object(panel.monitor,'report',return_value='new report'),patch.object(export_transfer.os,'replace',side_effect=OSError('synthetic disk error')),patch.object(stream_widgets.QMessageBox,'warning') as warning:
                    panel.save_report();flush_report(panel);warning.assert_called_once()
                self.assertEqual(target.read_text(encoding='utf-8'),'old report')
                self.assertEqual([p.name for p in root.iterdir()],['report.txt'])
        finally:panel.shutdown();panel.deleteLater()


if __name__=='__main__':unittest.main()

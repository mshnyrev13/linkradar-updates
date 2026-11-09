"""Technician feedback: writable export paths and persistent Graphene outcomes."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys
from pathlib import Path
import tempfile
import errno
import json
import time
import threading
import socketserver
import unittest
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qt_support import production_application,managed_window
import desktop
import graphene_port as source
import port_widgets
import text_export
import export_transfer
import stream_widgets
from qt_support import flush_report
from PySide6.QtCore import Qt,QPoint
from PySide6.QtGui import QContextMenuEvent,QKeySequence
from PySide6.QtTest import QTest


class FeedbackRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=production_application(cls)

    def test_network_export_starts_in_absolute_user_directory(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),managed_window(desktop.Window(Path(folder),service_folder=False)) as window:
            window.sid='fixture'
            with patch.object(desktop.QFileDialog,'getSaveFileName',return_value=('','')) as choose:
                window.export_session()
            default=Path(choose.call_args.args[2])
            self.assertTrue(default.is_absolute(),str(default))
            self.assertNotEqual(default.parent,Path('/'))

    def test_graphene_failure_remains_visible_during_automatic_retry(self):
        panel=port_widgets.PortPanel();panel.timer.stop()
        try:
            panel.connected=('switch','Gi1/0/1',300)
            panel.failure((panel.connected,'Нет доступа к Graphene'))
            with patch.object(port_widgets.PortFetch,'start'),patch.object(source,'client_session',return_value=Mock()):
                panel.refresh()
            self.assertIn('Нет доступа',panel.status.text())
            self.assertNotIn('Читаем',panel.status.text())
            self.assertTrue(all('Нет доступа' in plot.message for plot in panel.plots))
        finally:panel.shutdown();panel.deleteLater();self.app.processEvents()

    def test_network_field_has_full_value_copy_action(self):
        value=desktop.CompactValue('2001:db8:1234:5678:90ab:cdef:1234:5678');value.resize(48,20)
        try:
            self.assertTrue(hasattr(value,'copy_value'))
            value.copy_value()
            self.assertEqual(self.app.clipboard().text(),value.text())
        finally:value.deleteLater();self.app.processEvents()

    def test_relative_native_dialog_result_uses_displayed_folder(self):
        parent=desktop.QWidget()
        try:
            with tempfile.TemporaryDirectory() as folder:
                parent.setProperty(text_export.DIRECTORY_PROPERTY,folder)
                with patch.object(text_export.QFileDialog,'getSaveFileName',return_value=('Отчёт сети.txt','')):
                    self.assertEqual(text_export.choose_path(parent,'TXT','default.txt'),str(Path(folder)/'Отчёт сети.txt'))
                self.assertEqual(text_export.initial_path(parent,'next.txt'),Path(folder)/'next.txt')
        finally:parent.deleteLater()

    def test_missing_remembered_folder_falls_back_to_standard_user_folder(self):
        parent=desktop.QWidget()
        try:
            with tempfile.TemporaryDirectory() as folder:
                parent.setProperty(text_export.DIRECTORY_PROPERTY,str(Path(folder)/'removed'))
                with patch.object(text_export.QStandardPaths,'writableLocation',return_value=folder):
                    self.assertEqual(text_export.initial_path(parent,'a.txt'),Path(folder)/'a.txt')
        finally:parent.deleteLater()

    def seed(self,window):
        config=desktop.server.validate_config({'continuous':True,'targets':[{'host':'192.0.2.1'}]})
        window.sid='fixture';window.snapshot={}
        window.store.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',('fixture',time.time(),None,'running',json.dumps(config)))
        window.store.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',('fixture',config['targets'][0]['id'],time.time(),'ok',12,'192.0.2.1',''))

    def wait_export(self,window):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            self.app.processEvents()
            if window.export_action.isEnabled():return
            time.sleep(.005)
        self.fail('Export did not finish')

    def test_real_async_export_remembers_only_successful_directory(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),managed_window(desktop.Window(Path(folder),service_folder=False)) as window:
            self.seed(window);target=Path(folder)/'Отчёт сети.txt'
            with patch.object(desktop.QFileDialog,'getSaveFileName',return_value=(str(target),'')):
                window.export_session();self.wait_export(window)
            self.assertTrue(target.exists());self.assertIn('192.0.2.1',target.read_text())
            self.assertEqual(window.property(text_export.DIRECTORY_PROPERTY),folder)

    def test_read_only_export_keeps_session_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),managed_window(desktop.Window(Path(folder),service_folder=False)) as window:
            self.seed(window);target=Path(folder)/'keep.txt';target.write_text('previous report')
            with patch.object(desktop.QFileDialog,'getSaveFileName',return_value=(str(target),'')),patch.object(export_transfer.tempfile,'NamedTemporaryFile',side_effect=OSError(errno.EROFS,'Read-only filesystem')),patch.object(desktop.QMessageBox,'warning') as warning:
                window.export_session();self.wait_export(window)
            self.assertEqual(target.read_text(),'previous report');self.assertEqual(window.sid,'fixture')
            self.assertIsNone(window.property(text_export.DIRECTORY_PROPERTY))
            self.assertIn('другую папку',warning.call_args.args[2])
            with patch.object(desktop.QFileDialog,'getSaveFileName',return_value=(str(target),'')):
                window.export_session();self.wait_export(window)
            self.assertIn('192.0.2.1',target.read_text())

    def test_stream_save_uses_same_absolute_location_and_remembers_success(self):
        panel=stream_widgets.StreamPanel();panel.timer.stop()
        try:
            with tempfile.TemporaryDirectory() as folder,patch.object(panel.monitor,'report',return_value='Synthetic stream report'):
                panel.setProperty(text_export.DIRECTORY_PROPERTY,folder)
                with patch.object(stream_widgets.QFileDialog,'getSaveFileName',return_value=('Stream.txt','')):
                    panel.save_report();flush_report(panel)
                self.assertEqual((Path(folder)/'Stream.txt').read_text(),'Synthetic stream report')
                self.assertEqual(panel.property(text_export.DIRECTORY_PROPERTY),folder)
        finally:panel.shutdown();panel.deleteLater();self.app.processEvents()

    def test_context_menu_copies_clicked_full_value_even_after_refresh(self):
        value=desktop.CompactValue('2001:db8:1234:5678::abcd');value.resize(35,18)
        class Menu:
            def __init__(self,parent):self.action=desktop.QAction(parent)
            def addAction(self,text):self.action.setText(text);return self.action
            def exec(self,*args):
                value.setText('changed while menu is open');self.action.trigger()
            def deleteLater(self):self.action.deleteLater()
        try:
            with patch.object(desktop,'QMenu',Menu):
                value.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse,QPoint(2,2),QPoint(2,2)))
            self.assertEqual(self.app.clipboard().text(),'2001:db8:1234:5678::abcd')
        finally:value.deleteLater();self.app.processEvents()

    def test_selection_survives_unchanged_update_and_keyboard_copy_works(self):
        value=desktop.CompactValue('192.0.2.15');value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse|Qt.TextInteractionFlag.TextSelectableByKeyboard)
        try:
            value.setSelection(0,5);value.setText('192.0.2.15')
            self.assertEqual(value.selectedText(),'192.0')
            QTest.keySequence(value,QKeySequence(QKeySequence.StandardKey.Copy))
            self.assertEqual(self.app.clipboard().text(),'192.0')
        finally:value.deleteLater();self.app.processEvents()

    def test_retry_keeps_previous_graphene_points_and_success_clears_error(self):
        panel=port_widgets.PortPanel();panel.timer.stop()
        try:
            panel.connected=('switch','Gi1/0/1',300)
            data=dict(host='switch',port='Gi1/0/1',period=300,begin=700,end=1000,latest=1000,step=30,series={k:[(1000,0)] for k in source.SENSORS})
            panel.receive(data);panel.failure((panel.connected,'Нет доступа'))
            with patch.object(port_widgets.PortFetch,'start'),patch.object(source,'client_session',return_value=Mock()):panel.refresh()
            self.assertIs(panel.snapshot,data);self.assertTrue(all(p.data is data for p in panel.plots))
            self.assertIn('Нет доступа',panel.status.text());panel.receive(data)
            self.assertFalse(panel.failed);self.assertNotIn('Нет доступа',panel.status.text())
        finally:panel.shutdown();panel.deleteLater();self.app.processEvents()

    def test_typed_graphene_error_is_compact_with_full_details_in_tooltip(self):
        panel=port_widgets.PortPanel();panel.timer.stop()
        try:
            panel.connected=('switch','Gi1/0/1',300)
            details='Нет доступа к Graphene (HTTP 403). Проверьте корпоративную сеть и авторизацию.'
            panel.failure((panel.connected,details,'access'))
            self.assertEqual(panel.status.text(),'Нет доступа к Graphene')
            self.assertEqual(panel.status.toolTip(),details)
            for plot in panel.plots:
                self.assertLess(len(plot.message),80)
                self.assertEqual(plot.toolTip(),details)
        finally:panel.shutdown();panel.deleteLater();self.app.processEvents()

    def test_empty_series_and_real_zero_have_different_chart_states(self):
        plot=port_widgets.PortPlot('Traffic',('rx','tx'))
        try:
            plot.data=dict(begin=0,end=300,series={k:[] for k in source.SENSORS})
            self.assertFalse(plot.chart_info()['has_data'])
            plot.data=dict(begin=0,end=300,series={k:[(100,0)] for k in source.SENSORS})
            self.assertTrue(plot.chart_info()['has_data'])
        finally:plot.deleteLater()


class GrapheneAccessTests(unittest.TestCase):
    def test_dripping_http_headers_are_bounded_by_request_deadline(self):
        stop=threading.Event()
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(8192)
                try:
                    for byte in b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{}':
                        if stop.wait(.03):return
                        self.request.sendall(bytes([byte]))
                except OSError:pass
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads=True
        with Server(('127.0.0.1',0),Handler) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            client=source.requests.Session();client.trust_env=False;client.mount('http://',source.bound_http.adapter(None))
            real_deadline=source.bound_http.Deadline
            try:
                with patch.object(source,'BASE','http://127.0.0.1:'+str(server.server_address[1])),patch.object(source.bound_http,'Deadline',side_effect=lambda seconds,*args,**kwargs:real_deadline(.2,*args,**kwargs)):
                    started=time.monotonic()
                    with self.assertRaises(source.SourceError):source.fetch('switch','Gi1/0/1',300,session=client)
                    self.assertLess(time.monotonic()-started,2)
            finally:stop.set();client.close();server.shutdown();thread.join(3)

    def test_catalog_and_data_reject_auth_responses_without_redirects(self):
        for status in (301,302,401,403):
            for catalog in (False,True):
                with self.subTest(status=status,catalog=catalog):
                    client=Mock();response=Mock(status_code=status);client.get.return_value=response;client.post.return_value=response
                    with self.assertRaises(source.SourceError) as caught:
                        if catalog:source.resolve('switch','Gi1/0/1',session=client)
                        else:source.fetch('switch','Gi1/0/1',300,session=client)
                    self.assertEqual(caught.exception.code,'access');response.close.assert_called_once()
                    call=client.post.call_args if catalog else client.get.call_args
                    self.assertFalse(call.kwargs['allow_redirects']);self.assertTrue(call.kwargs['verify'])

    def test_html_login_and_timeout_are_actionable(self):
        response=Mock();response.iter_content.return_value=iter([b'<html>Login</html>'])
        with self.assertRaises(source.SourceError) as caught:source.read_json(response)
        self.assertEqual(caught.exception.code,'access');response.close.assert_called_once()
        client=Mock();client.get.side_effect=source.requests.Timeout('private transport detail')
        with self.assertRaises(source.SourceError) as caught:source.fetch('switch','Gi1/0/1',300,session=client)
        self.assertEqual(caught.exception.code,'timeout');self.assertNotIn('private transport',str(caught.exception))

    def test_failed_replace_leaves_old_report_and_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'report.txt';target.write_text('old')
            with patch.object(export_transfer.os,'replace',side_effect=PermissionError(errno.EACCES,'denied')):
                with self.assertRaises(PermissionError):export_transfer.atomic_write(target,lambda output:output.write(b'new'))
            self.assertEqual(target.read_text(),'old');self.assertEqual(list(Path(folder).iterdir()),[target])

if __name__=='__main__':unittest.main()

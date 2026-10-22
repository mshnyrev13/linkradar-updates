"""Native controls regressions, without real probes or an HTTP listener."""
from qt_support import close_window,flush_io,managed_window,production_application,settle_layout
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop
from PySide6.QtCore import QCoreApplication,QEvent,QPoint
from PySide6.QtWidgets import QProgressBar

class NativeControls(unittest.TestCase):
    def setUp(self):
        self.discovery_patch=patch.object(desktop.Discovery,'start');self.discovery_patch.start()
    def tearDown(self):self.discovery_patch.stop()
    @classmethod
    def setUpClass(cls):
        cls.app = production_application(cls)

    def test_play_pause_preserves_session_and_other_server_starts_clean(self):
        with tempfile.TemporaryDirectory() as folder:
            w = desktop.Window(Path(folder))
            w.timer.stop()
            w.address.setText('rtmp://example.com:1940/live')
            configs = []
            def start(config):
                configs.append(config)
                w.monitor.sid = str(len(configs))
                return w.monitor.sid
            def stop():
                w.monitor.sid = None
            with patch.object(w.monitor, 'start', side_effect=start), patch.object(w.monitor, 'stop', side_effect=stop), patch.object(w, 'refresh'), patch.object(w.monitor,'pause',side_effect=[True,False]), patch.object(w.monitor,'set_interval') as set_interval:
                w.button.click()
                self.assertEqual(len(configs), 1)
                w.button.click()
                self.assertEqual(w.monitor.sid,'1');self.assertTrue(w.paused)
                self.assertEqual(w.button.accessibleName(),'Пуск')
                w.button.click()
                self.assertEqual(w.monitor.sid,'1');self.assertFalse(w.paused)
                self.assertEqual(len(configs),1)
                w.interval.setCurrentIndex(2)
                set_interval.assert_called_once_with(2)
                self.assertFalse(w.pending_restart);self.assertEqual(len(configs),1)
                w.address.setText('ya.ru')
                self.assertEqual(len(configs),1)
                self.assertEqual(w.button.accessibleName(),'Пуск')
                w.button.click();self.assertTrue(w.pending_restart)
                w.render({'active_id':None,'targets':[]})
                self.assertEqual(len(configs),2)
                self.assertEqual(configs[-1]['targets'][0]['host'],'ya.ru')
                self.assertEqual(configs[-1]['interval'],2)
                w.period.setCurrentIndex(6)
                self.assertEqual(w.timer.interval(),2000);self.assertEqual(len(configs),2)
                close_window(w)
            w.store.db.close()
            w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

    def test_speed_overall_progress_and_cancellation(self):
        with tempfile.TemporaryDirectory() as folder:
            w=desktop.Window(Path(folder));w.timer.stop();w.show();self.app.processEvents()
            self.assertGreater(w.speed_button.mapTo(w,QPoint()).y(),w.address.mapTo(w,QPoint()).y())
            self.assertEqual(w.findChildren(QProgressBar),[])
            for phase,fraction in [('download',.25),('upload',.75)]:
                w.speed_progress({'phase':phase,'total_progress':fraction,'mbps':10,'progress':.5})
                self.assertEqual(w.speed_button.progress,int(fraction*100))
                self.assertIn(str(int(fraction*100))+'%',w.speed_button.text())
            image=w.speed_button.grab().toImage()
            self.assertEqual(image.pixelColor(10,5).name(),'#2563eb')
            self.assertEqual(image.pixelColor(image.width()-10,5).name(),'#172437')
            w.speed_worker=MagicMock();w.speed_worker.isRunning.return_value=True
            w.speed_button.click();w.speed_worker.stop.set.assert_called_once()
            self.assertFalse(w.speed_button.isEnabled())
            w.speed_worker=None
            w.speed_failure('Измерение отменено')
            self.assertEqual(w.speed_button.progress,75)
            self.assertIn('Отменено',w.speed_button.text())
            self.assertTrue(w.speed_button.isEnabled())
            close_window(w);w.store.db.close()
            w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

    def test_failed_or_one_sided_speed_is_never_labelled_stable(self):
        with tempfile.TemporaryDirectory() as folder:
            w=desktop.Window(Path(folder));w.timer.stop()
            w.speed_worker=MagicMock();w.speed_worker.interface='en7'
            good={'mbps':100,'seconds':15,'limited':False,'partial':False,'stable':True}
            with patch.object(w,'record'):
                for download,upload in ((None,None),(good,None),(None,good)):
                    w.speed_result({'download':download,'upload':upload,'errors':{'test':'Synthetic failure'}})
                    self.assertNotIn('устойчивый замер',w.speed_readout.toolTip())
                    self.assertIn('нет данных',w.speed_readout.text())
                w.speed_result({'download':good,'upload':good,'errors':{}})
                self.assertIn('устойчивый замер',w.speed_readout.toolTip())
            w.speed_worker=None
            close_window(w);w.store.db.close()
            w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

    def test_close_clears_local_session_and_ignores_queued_results(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
            w=desktop.Window(Path(folder));w.timer.stop()
            config=desktop.server.validate_config({'continuous':True,'targets':[{'host':'192.0.2.1'}]})
            w.start_session(config)
            w.store.event(w.sid,'','info','synthetic event')
            self.assertTrue(w.store.rows('SELECT * FROM sessions'))
            close_window(w)
            w.speed_result({});w.record_port({});w.render({'active_id':None})
            for table in ('sessions','samples','hop_samples','routes','events','session_data'):
                self.assertEqual(w.store.rows('SELECT * FROM '+table),[])
            self.assertIsNone(w.monitor.sid)
            w.store.db.close();w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

    def test_compact_controls_left_of_route_at_both_window_sizes(self):
        with tempfile.TemporaryDirectory() as folder,managed_window(desktop.Window(Path(folder))) as w:
            w.timer.stop();w.show();settle_layout(self.app)
            w.table.setRowCount(15)
            for width,height in ((1100,720),(900,640)):
                w.resize(width,height)
                for _ in range(4):self.app.processEvents()
                self.assertEqual((w.width(),w.height()),(width,height))
                self.assertEqual(w.address.parent().width(),180)
                self.assertEqual(w.network_panel.width(),180)
                widgets=[w.speed_button,w.address,w.family,w.button,*w.cards]
                route_left=w.table.mapTo(w,QPoint()).x()
                for widget in widgets:
                    self.assertLess(widget.mapTo(w,QPoint(widget.width(),0)).x(),route_left)
                    self.assertLessEqual(widget.mapTo(w,QPoint(0,widget.height())).y(),w.height())
                for upper,lower in zip((w.address,w.button,w.cards[0]),(w.button,w.cards[0],w.cards[1])):
                    self.assertLessEqual(upper.mapTo(w,QPoint(0,upper.height())).y(),lower.mapTo(w,QPoint()).y())
                self.assertGreater(w.table.columnWidth(8),140)
                self.assertTrue(w.table.isColumnHidden(1));self.assertTrue(w.table.isColumnHidden(3))

    def test_help_actions_remain_available_without_a_windows_menu_row(self):
        with tempfile.TemporaryDirectory() as folder,managed_window(desktop.Window(Path(folder))) as w:
            w.timer.stop();w.show();settle_layout(self.app)
            self.assertIn(w.about_action,w.tools_menu.actions())
            self.assertIn(w.update_action,w.tools_menu.actions())
            if sys.platform=='darwin':
                self.assertIsNotNone(w.app_menu_bar)
                self.assertIsNone(w.app_menu_bar.parent())
            else:
                self.assertIsNone(w.menuWidget())
                self.assertIsNone(w.app_menu_bar)
            w.about_action.trigger();settle_layout(self.app)
            self.assertTrue(w.about_dialog.isVisible())

class AppIdentity(unittest.TestCase):
    def test_windows_taskbar_uses_own_app_id_and_multisize_icon(self):
        app=desktop.QApplication.instance() or desktop.QApplication([])
        icon=desktop.app_icon();self.assertFalse(icon.isNull())
        if sys.platform=='win32':
            self.assertTrue({16,32,48,256}.issubset({size.width() for size in icon.availableSizes()}))
            import ctypes
            desktop.set_windows_app_id();value=ctypes.c_wchar_p()
            self.assertEqual(ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(value)),0)
            self.assertEqual(value.value,desktop.APP_ID)

if __name__ == '__main__':
    unittest.main()

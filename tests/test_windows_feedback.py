"""Windows feedback reproduced with synthetic Ethernet frames and source snapshots."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from linkradar.network import network_info as n
from linkradar.ui import measurement_display as display
from linkradar.core import session_export
from linkradar.ui import desktop
from linkradar.ui import stream_widgets as ui
from qt_support import production_application,managed_window,settle_layout
from test_network_info import frame
from test_discovery_feedback import cdp_frame,FakeStop


class SelfDiscovery(unittest.TestCase):
    def test_local_mac_formats_and_invalid_values(self):
        rows=[{'mac':m} for m in ('00:11:22:33:44:55','AA-BB-CC-DD-EE-FF','aabb.ccdd.eeff',None,'bad','00:00:00:00:00:00')]
        self.assertEqual(n.local_mac_bytes(rows),{bytes.fromhex('001122334455'),bytes.fromhex('aabbccddeeff')})

    def test_outbound_lldp_and_cdp_are_ignored_but_peer_is_kept(self):
        reader=object.__new__(n.LldpCapture);reader.local_macs={bytes.fromhex('001122334455')}
        for raw in (frame(),cdp_frame()):
            peer=raw[:6]+bytes.fromhex('aabbccddeeff')+raw[12:]
            reader.read_packet=Mock(side_effect=[{'raw':raw},{'raw':peer},None])
            self.assertIsNotNone(reader.poll());self.assertEqual(reader.read_packet.call_count,2)
        reader.read_packet=Mock(side_effect=[{'raw':frame(ttl=0)},None])
        self.assertIsNone(reader.poll())

    def test_discover_configures_local_macs_before_first_read(self):
        reader=object.__new__(n.LldpCapture)
        reader.close=Mock()
        reader.read_packet=Mock(side_effect=[{'raw':frame()},None,None,None])
        clock=[0.];states=[]
        rows=[{'id':'usb','capture':'usb','mac':'00:11:22:33:44:55'}, {'id':'wifi','capture':'wifi','mac':'aa:bb:cc:dd:ee:ff'}]
        with patch.object(n,'local_interfaces',return_value=rows),patch.object(n,'LldpCapture',return_value=reader),patch.object(n.time,'monotonic',side_effect=lambda:clock[0]):
            n.discover(FakeStop(clock,1),lambda:'usb',states.append)
        self.assertTrue(all(s['neighbor'] is None for s in states))
        self.assertEqual(len(reader.local_macs),2);reader.close.assert_called_once()


class WindowsPrecision(unittest.TestCase):
    def test_windows_zero_is_submillisecond_without_fabricating_jitter(self):
        with patch.object(display,'sys',SimpleNamespace(platform='win32')):
            self.assertEqual(display.rtt(0),'<1');self.assertEqual(display.rtt(None),'—')
            self.assertEqual(display.rtt(3),'3,0');self.assertEqual(display.rtt(.25),'0,2')
            self.assertIn('Нулевой jitter',display.precision_note())
            self.assertEqual(desktop.fmt(0),'0,0')
        with patch.object(display,'sys',SimpleNamespace(platform='darwin')):
            self.assertEqual(display.rtt(0),'0,0');self.assertEqual(display.precision_note(),'')
        self.assertIn('Числа ниже — исходные значения API',session_export.HEADER)


class StreamExplanations(unittest.TestCase):
    def test_vmix_metrics_explain_separate_sources_and_remote_limit(self):
        stream={'key':'vmix:stream','state':'live'}
        self.assertIn('Statistics',ui.missing_metric_message(stream,{}, {'dropped_rate'}))
        self.assertIn('администратора',ui.missing_metric_message(stream,{}, {'bitrate'}))
        self.assertIn('удалённый API',ui.missing_metric_message(stream,{}, {'rtt'},True))
        self.assertIn('не сообщает',ui.missing_metric_message(stream,{'vmix_stats':{'state':'ok'}}, {'dropped_rate'}))
        self.assertIn('WebSocket',ui.missing_metric_message({'key':'obs:stream','state':'live'},{},{'bitrate'}))
        self.assertIn('не сообщает потери',ui.missing_metric_message({'key':'tcp:vMix:1','state':'connected'},{},{'dropped_rate'}))


class WindowsFeedbackUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=production_application(cls)

    def test_windows_precision_hint_and_titles_fit_compact_window(self):
        with patch.object(display,'sys',SimpleNamespace(platform='win32')),tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),managed_window(desktop.Window(Path(folder),service_folder=False)) as window:
            window.resize(900,640);window.show();settle_layout(self.app)
            self.assertEqual((window.width(),window.height()),(900,640))
            self.assertTrue(window.precision_label.isVisible());self.assertIn('1 мс',window.precision_label.text())
            self.assertEqual(window.diagnostics_title.text(),'Диагностика сети')
            self.assertEqual(window.traffic_panel.title.text(),'Анализ сетевого трафика')

    def test_unused_obs_is_neutral_but_previous_obs_failure_is_preserved(self):
        panel=ui.StreamPanel()
        stream=dict(key='vmix:stream',source='vMix',label='vMix',state='live',active=True)
        data=dict(now=1,streams=[stream],history={'vmix:stream':[(1,{'bitrate':None},'live')]},status={'obs':{'state':'error','text':'OBS synthetic failure'},'vmix_api':{'state':'ok','text':'vMix 29'},'vmix_stats':{'state':'off','text':'окно Statistics закрыто'}},events=[],vmix_rows=[])
        try:
            panel.render(data)
            self.assertIn('нужен только для OBS',panel.sources.text())
            self.assertIn('OBS synthetic failure',panel.sources.toolTip())
            self.assertIn('TCP-счётчиков',panel.plots[0].message)
            self.assertIn('Statistics',panel.plots[1].message)
            data['history']['vmix:stream']=[(1,{'bitrate':0},'live')]
            panel.render(data);self.assertIn('Наведите',panel.plots[0].toolTip())
            data['streams'].append(dict(key='obs:stream',source='OBS',label='OBS',state='lost',active=False))
            panel.render(data);self.assertIn('OBS synthetic failure',panel.sources.text())
        finally:panel.shutdown();panel.deleteLater();self.app.processEvents()


if __name__=='__main__':unittest.main()

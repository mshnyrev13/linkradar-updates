"""History navigation, speed-test latency and local vMix RTMP regressions."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys, tempfile, unittest, json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qt_support import production_application,managed_window,settle_layout
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from linkradar.ui import desktop
from linkradar.ui.timeline import Timeline
from linkradar.stream import stream_monitor as engine,stream_sources as sources
from linkradar.network import speed_test


class Navigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=production_application(cls)

    def test_history_stays_fixed_while_live_advances_and_can_resume(self):
        bar=Timeline();bar.set_bounds(100,1100,60)
        bar.scroll.setValue(300);self.assertEqual(bar.focus_end,460)
        bar.set_bounds(100,1200,60);self.assertEqual(bar.focus_end,460)
        QTest.mouseClick(bar.live,Qt.MouseButton.LeftButton)
        self.assertIsNone(bar.focus_end);self.assertEqual(bar.scroll.value(),bar.scroll.maximum())

    def test_retention_and_zoom_clamp_without_negative_time(self):
        bar=Timeline();bar.set_bounds(0,50000,60);bar.scroll.setValue(0)
        self.assertEqual(bar.focus_end,6860)
        bar.set_bounds(0,50100,60);self.assertEqual(bar.focus_end,6960)
        bar.set_bounds(50000,50100,300);self.assertIsNone(bar.focus_end)
        self.assertEqual(bar.scroll.maximum(),0)

    def test_keyboard_scroll_resumes_live_at_right_edge(self):
        bar=Timeline();bar.set_bounds(100,1100,60)
        bar.scroll.setFocus();QTest.keyClick(bar.scroll,Qt.Key.Key_Home)
        self.assertEqual(bar.focus_end,160)
        QTest.keyClick(bar.scroll,Qt.Key.Key_End);self.assertIsNone(bar.focus_end)

    def test_stream_historical_interval_excludes_newer_points(self):
        m=engine.StreamMonitor(clock=lambda:1200)
        m.started=100;m.stream('vmix:stream',100,label='test')
        m.history['vmix:stream'].extend((ts,{'bitrate':ts},'live') for ts in (100,300,500,700,1000,1200))
        snap=m.snapshot(300,key='vmix:stream',focus_end=700)
        self.assertEqual([p[0] for p in snap['history']['vmix:stream']],[500,700])
        self.assertEqual(snap['now'],1200);self.assertEqual(snap['chart_end'],700)

    @unittest.skipUnless(sys.platform=='win32','Windows policy')
    def test_windows_discovery_does_not_start_hidden_graphene(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),managed_window(desktop.Window(Path(folder))) as w:
            w.timer.stop();w.show();settle_layout(self.app)
            self.assertTrue(w.port_panel.isHidden());self.assertFalse(w.port_panel.timer.isActive())
            with patch.object(w.port_panel,'lldp') as lldp:
                w.show_network({'interfaces':[],'local':{},'neighbor':{},'lldp_status':'test'})
                lldp.assert_not_called()


class SpeedLatency(unittest.TestCase):
    def test_speedtest_measures_latency_without_monitor_session_and_keeps_adapter(self):
        config={'download':{'probes':[{'url':'https://probe.cdn.yandex.net/down'}]},'upload':{'probes':[{'url':'https://probe.cdn.yandex.net/up'}]}}
        progress=[];test=speed_test.SpeedTest(progress=progress.append,interface='adapter-test')
        with patch.object(test,'config',return_value=config),patch.object(test,'phase',return_value={'mbps':5}),patch.object(desktop.server,'probe',return_value={'status':'ok','latency':0}) as probe:
            result=test.run()
        self.assertEqual(result['ping']['latency'],0)
        self.assertEqual(probe.call_args.kwargs['interface'],'adapter-test')
        self.assertEqual([p['phase'] for p in progress],['config','ping'])

    def test_missing_download_does_not_prevent_upload_or_fabricate_ping(self):
        test=speed_test.SpeedTest();config={'upload':{'probes':[{'url':'https://probe.cdn.yandex.net/up'}]}}
        with patch.object(test,'config',return_value=config),patch.object(test,'phase',return_value={'mbps':5}),patch.object(desktop.server,'probe',return_value={'status':'timeout','latency':None}):result=test.run()
        self.assertIsNotNone(result['upload']);self.assertIsNone(result['ping']['latency']);self.assertIn('download',result['errors'])


class VmixRTMP(unittest.TestCase):
    progress='frame= 38 fps=30.0 q=0.0 size= 1665kB time=00:00:01.23 bitrate= 2315.6kbits/s'

    def test_log_parser_allowlists_numbers_and_never_exposes_credentials(self):
        row=sources.parse_vmix_progress('Command: rtmp://user:password@host/live/SECRET\n'+self.progress)
        self.assertEqual(row['bitrate'],2315.6);self.assertEqual(row['fps'],30)
        self.assertNotIn('SECRET',json.dumps(row));self.assertNotIn('password',json.dumps(row))
        self.assertIsNone(sources.parse_vmix_progress(self.progress+'\nConversion failed!'))
        self.assertIsNone(sources.parse_vmix_progress('frame= malformed'))

    def test_only_fresh_logs_are_read_and_tail_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'streaming1 test.log';path.write_text('secret '*20000+'\n'+self.progress)
            os.utime(path,(1000,1000))
            rows=sources.vmix_encoder_logs(folder,now=1001)
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['frames'],38)
            self.assertEqual(sources.vmix_encoder_logs(folder,now=1011),[])
            self.assertEqual(set(rows[0]),{'channel','ts','generation','frames','fps','bytes','media_seconds','bitrate'})

    def test_log_does_not_replay_same_progress_or_invent_network_loss(self):
        m=engine.StreamMonitor(clock=lambda:1000)
        row={'channel':'1','ts':1000,'generation':'test',**sources.parse_vmix_progress(self.progress)}
        args=dict(obs=None,vmix_api=None,vmix_rows=None,connections=[],ndi=None)
        m.cycle(1000,vmix_logs=[row],**args);m.cycle(1001,vmix_logs=[row],**args)
        self.assertEqual(len(m.history['vmix:encoder:1']),1)
        stream=m.streams['vmix:encoder:1'];self.assertNotIn('dropped_rate',stream);self.assertNotIn('rtt',stream)
        m.cycle(1012,vmix_logs=[],**args);self.assertEqual(stream['state'],'lost');self.assertIsNone(stream['bitrate'])

    def test_local_vmix_api_uses_log_fallback_without_estats(self):
        m=engine.StreamMonitor(clock=lambda:1000)
        row={'channel':'1','ts':1000,'generation':'test',**sources.parse_vmix_progress(self.progress)}
        m.cycle(1000,obs=None,vmix_api={'streaming':True},vmix_rows=None,connections=[],ndi=None,vmix_logs=[row])
        self.assertEqual(m.streams['vmix:stream']['bitrate'],2315.6)
        self.assertIn('не TCP goodput',m.streams['vmix:stream']['note'])
        m.configure(vmix_url='http://192.0.2.1:8088/api');m.link_tcp()
        self.assertIsNone(m.streams['vmix:stream']['bitrate'])
        m.streams['vmix:stream'].update(bitrate=1000,fps=25)
        m.update_vmix_api(1001,{'streaming':False})
        self.assertIsNone(m.streams['vmix:stream']['bitrate']);self.assertIsNone(m.streams['vmix:stream']['fps'])

    def test_vmix_external_encoder_is_collected(self):
        conn={'pid':42,'remote':'192.0.2.1','remote_port':1935,'local_port':50000,'row':None}
        with patch.object(sources,'tcp_table',return_value=[conn]),patch.object(sources,'process_name',return_value='ffmpeg2.exe'),patch.object(sources,'tcp_estats',return_value=None):rows=sources.encoder_connections()
        self.assertEqual(rows[0]['app'],'vMix');self.assertEqual(rows[0]['protocol'],'RTMP')

    def test_vmix_bitrate_updates_and_clears_when_counters_disappear(self):
        m=engine.StreamMonitor();m.stream('vmix:stream',1,source='vMix',state='live')
        s=m.stream('tcp:vMix:test',1,source='vMix',protocol='RTMP',target='192.0.2.1:1935',state='connected',bitrate=2000,rtt=2)
        m.link_tcp();self.assertEqual(m.streams['vmix:stream']['bitrate'],2000)
        s['bitrate']=6000;m.link_tcp();self.assertEqual(m.streams['vmix:stream']['bitrate'],6000)
        s['bitrate']=None;m.link_tcp();self.assertIsNone(m.streams['vmix:stream']['bitrate'])
        s['state']='lost';m.link_tcp();self.assertIsNone(m.streams['vmix:stream']['rtt'])

    def test_numbered_stream_statistics_are_output_rows(self):
        for name in ('Stream','Stream 1','Streaming 2','Streaming','Record 1'):
            self.assertEqual(sources.vmix_kind(name),'output')
        self.assertEqual(sources.vmix_kind('StreamCamera'),'input')

if __name__=='__main__':unittest.main()

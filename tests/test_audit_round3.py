"""Round 3: visible extrema and elapsed-time stream rates, synthetic data only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPointF
import desktop
import server
import stream_monitor
import stream_sources
import stream_widgets
import measurement_metrics
from plot_sampling import probe_samples


def obs(skipped):
    return dict(stream=dict(outputActive=True, outputBytes=1000,
                           outputSkippedFrames=skipped, outputTotalFrames=1000,
                           outputDuration=10000), stats={}, video={}, outputs=[])


def vmix(skipped):
    return stream_sources.parse_vmix_rows([], [
        ['SRT Camera', str(skipped), '0', '0', '0', '', '']])


class RoundThree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_obs_loss_rate_uses_actual_elapsed_seconds(self):
        monitor = stream_monitor.StreamMonitor(clock=lambda: 100)
        monitor.update_obs(100, obs(0))
        monitor.update_obs(104, obs(12))
        stream = monitor.streams['obs:stream']
        self.assertEqual(stream['dropped_delta'], 12)
        self.assertEqual(stream.get('dropped_rate'), 3)
        monitor.sample('obs:stream', 104)
        self.assertEqual(monitor.history['obs:stream'][-1][1].get('dropped_rate'), 3)
        self.assertEqual(monitor.summary('obs:stream', 300, 104)['dropped'], 12)

    def test_vmix_loss_rate_uses_actual_elapsed_seconds(self):
        monitor = stream_monitor.StreamMonitor(clock=lambda: 100)
        monitor.update_vmix_rows(100, vmix(0))
        monitor.update_vmix_rows(104, vmix(12))
        stream = monitor.streams['vmix:stat:SRT Camera']
        self.assertEqual(stream['dropped_delta'], 12)
        self.assertEqual(stream.get('dropped_rate'), 3)

    def test_vmix_pause_does_not_count_unobserved_losses(self):
        monitor = stream_monitor.StreamMonitor(clock=lambda: 100)
        monitor.update_vmix_rows(100, vmix(0))
        monitor.update_vmix_rows(101, vmix(3))
        monitor.pause()
        monitor.update_vmix_rows(120, vmix(30))
        stream = monitor.streams['vmix:stat:SRT Camera']
        self.assertIsNone(stream['dropped_delta'])
        self.assertIsNone(stream.get('dropped_rate'))
        monitor.update_vmix_rows(122, vmix(34))
        self.assertEqual(stream['dropped_delta'], 4)
        self.assertEqual(stream['dropped_rate'], 2)

    def test_vmix_closed_statistics_restarts_the_baseline(self):
        monitor = stream_monitor.StreamMonitor(clock=lambda: 100)
        monitor.update_vmix_rows(100, vmix(0))
        monitor.update_vmix_rows(101, None)
        monitor.update_vmix_rows(120, vmix(30))
        self.assertIsNone(monitor.streams['vmix:stat:SRT Camera']['dropped_delta'])

    def test_nonpositive_interval_and_counter_reset_do_not_create_a_rate(self):
        monitor = stream_monitor.StreamMonitor(clock=lambda: 100)
        monitor.update_obs(100, obs(10))
        monitor.update_obs(100, obs(20))
        self.assertIsNone(monitor.streams['obs:stream']['dropped_rate'])
        monitor.update_obs(101, obs(1))
        self.assertIsNone(monitor.streams['obs:stream']['dropped_rate'])
        monitor.update_vmix_rows(100, vmix(10))
        monitor.update_vmix_rows(100, vmix(20))
        self.assertIsNone(monitor.streams['vmix:stat:SRT Camera']['dropped_rate'])

    def test_stream_table_and_chart_show_rate_but_history_keeps_count(self):
        monitor = stream_monitor.StreamMonitor(clock=lambda: 104)
        monitor.update_obs(100, obs(0));monitor.update_obs(104, obs(12))
        monitor.sample('obs:stream',104)
        panel = stream_widgets.StreamPanel(monitor=monitor)
        try:
            panel.selected='obs:stream';panel.refresh()
            row=next(i for i in range(panel.table.rowCount()) if panel.table.item(i,0).text()=='OBS · Трансляция')
            self.assertEqual(panel.table.item(row,8).text(),'3,00')
            self.assertEqual(panel.plots[1].series[0]['key'],'dropped_rate')
            self.assertEqual(panel.plots[1].points[-1][1]['dropped_rate'],3)
        finally:
            panel.shutdown();panel.deleteLater()

    def test_vmix_first_sample_is_unknown_and_delta_tooltip_gives_elapsed_time(self):
        monitor = stream_monitor.StreamMonitor(clock=lambda: 104)
        monitor.update_vmix_rows(100,vmix(0))
        panel = stream_widgets.StreamPanel(monitor=monitor)
        try:
            panel.refresh()
            self.assertEqual(panel.vmix_table.item(0,7).text(),'нужен второй опрос')
            monitor.update_vmix_rows(104,vmix(12));panel.refresh()
            self.assertIn('+12',panel.vmix_table.item(0,7).text())
            self.assertIn('4,00 с',panel.vmix_table.item(0,7).toolTip())
        finally:panel.shutdown();panel.deleteLater()

    def test_probe_sampling_preserves_boundaries_and_independent_jitter_peak(self):
        raw=[dict(ts=i*.5,status='timeout' if i in (1202,1203) else 'ok',
                  latency=None if i in (1202,1203) else 200 if i==1200 else 10,
                  ip='192.0.2.1' if i<1400 else '192.0.2.2',detail='') for i in range(1800)]
        points=measurement_metrics.smooth_jitter(raw,.5)
        graph,step=probe_samples(points,.5);times={p['ts']:p for p in graph}
        for i in (0,1200,1201,1202,1203,1204,1399,1400,1799):self.assertIn(i*.5,times)
        self.assertTrue(times[700]['break_before']);self.assertFalse(times[600.5]['break_before'])
        self.assertEqual(max(p['jitter'] or 0 for p in graph),max(p['jitter'] or 0 for p in points))
        self.assertNotIn('break_before',points[0])

    def test_probe_bucket_does_not_move_a_gap_to_its_first_point(self):
        raw=[dict(ts=i*.5+(20 if i>=1001 else 0),status='ok',latency=10,
                  jitter=0,ip='192.0.2.1',detail='') for i in range(2000)]
        graph,_=probe_samples(raw,.5);by_time={p['ts']:p for p in graph}
        self.assertFalse(by_time[499.5]['break_before'])
        self.assertTrue(by_time[520.5]['break_before'])

    def test_alternating_errors_keep_graph_bounded_without_inventing_continuity(self):
        raw=[dict(ts=i*.5,status='ok' if i%2 else 'timeout',latency=10 if i%2 else None,
                  jitter=None,ip='192.0.2.1',detail='') for i in range(86400)]
        graph,_=probe_samples(raw,.5)
        self.assertLessEqual(len(graph),900*12)
        self.assertEqual({p['status'] for p in graph},{'ok','timeout'})
        self.assertTrue(all(p['break_before'] for p in graph[1:] if p['status']=='ok'))

    def test_ping_chart_cursor_uses_cached_surface_but_new_data_invalidates(self):
        chart=desktop.Graph(True);chart.resize(500,180)
        points=[dict(ts=i,status='ok',latency=20,jitter=i/10) for i in range(100)]
        chart.set_data(points,100,100)
        try:
            with patch.object(chart,'draw_chart',wraps=chart.draw_chart) as draw:
                chart.grab();count=draw.call_count
                chart.mouseMoveEvent(SimpleNamespace(position=lambda:QPointF(270,60)));chart.grab()
                self.assertEqual(draw.call_count,count)
                chart.set_data(points,101,100);chart.grab();self.assertGreater(draw.call_count,count)
        finally:chart.deleteLater()

    def test_compact_history_preserves_rtt_and_jitter_extrema_and_last(self):
        with tempfile.TemporaryDirectory() as folder:
            store = server.Store(Path(folder)/'db', clock=lambda: 2000)
            config = server.validate_config(dict(continuous=True, interval=.5, timeout=.5,
                targets=[dict(host='192.0.2.1')]))
            tid = config['targets'][0]['id']
            store.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',
                ('s', 0, 2000, 'completed', json.dumps(config)))
            # Two raw points per former output bucket; one peak followed by
            # a lower RTT with a larger smoothed jitter is the important case.
            store.db.executemany('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',
                (('s', tid, i*.5, 'ok', 200 if i==1200 else 10,
                  '192.0.2.1', '') for i in range(1800)))
            store.db.commit()
            monitor = server.Monitor(store)
            monitor.hostname = lambda ip: ip
            try:
                full = monitor.snapshot('s', 43200)['targets'][0]
                compact = monitor.snapshot('s', 43200, compact=True)['targets'][0]
                graph = compact['graph']
                self.assertEqual(graph[-1]['ts'], 899.5)
                for metric in ('latency', 'jitter'):
                    raw=[dict(r) for r in store.db.execute('SELECT * FROM samples ORDER BY ts')]
                    values = [p[metric] for p in measurement_metrics.smooth_jitter(raw,.5) if p.get(metric) is not None]
                    visible = [p[metric] for p in graph if p.get(metric) is not None]
                    self.assertEqual(max(visible), max(values), metric)
                    self.assertEqual(min(visible), min(values), metric)
                self.assertEqual(compact['window_stats'], full['window_stats'])
            finally:
                store.db.close()


if __name__ == '__main__':
    unittest.main()

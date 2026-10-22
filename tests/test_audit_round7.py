"""Round 7 regressions: report boundaries, independent curves, Stream refresh."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys,unittest,time,threading,tempfile
from pathlib import Path
from collections import deque
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import stream_monitor as engine,stream_widgets
from plot_sampling import line_samples
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage,QPainter,QColor
from PySide6.QtCore import QThread
from qt_support import flush_report,close_window

class RoundSeven(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
 def test_summary_excludes_measurements_after_report_time(self):
  m=engine.StreamMonitor(clock=lambda:100);m.history['s']=deque([(90,{'bitrate':10},'live'),(110,{'bitrate':999},'live')])
  summary=m.summary('s',30,now=100)
  self.assertEqual(summary['samples'],1);self.assertEqual(summary['bitrate']['avg'],10)
 def test_numeric_time_labels_match_system_formats_including_fractional_time(self):
  from time_labels import format_timestamp
  for stamp in (0,1.125,1700000000.999,1800000000.125,2147483647.5):
   for utc in (False,True):
    calendar=(time.gmtime if utc else time.localtime)(stamp)
    for pattern in ('%H:%M','%H:%M:%S','%Y-%m-%d %H:%M:%S','%d.%m.%Y'):
     self.assertEqual(format_timestamp(stamp,pattern,utc=utc),time.strftime(pattern,calendar))
     self.assertEqual(format_timestamp(stamp,pattern,utc=utc,milliseconds=True),time.strftime(pattern,calendar)+f'.{int(stamp%1*1000):03d}')
 def test_report_uses_one_history_snapshot_even_if_collection_continues(self):
  m=engine.StreamMonitor(clock=lambda:100);m.started=80
  for key in ('first','second'):
   m.streams[key]=dict(key=key,label=key,state='live',protocol='RTMP',target='rtmp://ingest.example.test')
   m.history[key]=deque([(90,{'bitrate':10},'live')])
  original=engine.duration;injected=[]
  def concurrent_write(seconds):
   if not injected:
    injected.append(True)
    for points in m.history.values():points.append((95,{'bitrate':999},'live'))
   return original(seconds)
  with patch.object(engine,'duration',side_effect=concurrent_write):report=m.report(30,now=100)
  self.assertEqual(report.count('Битрейт, кбит/с: сред 10 · мин 10 · макс 10'),2)
 def test_report_and_snapshot_do_not_show_events_or_points_from_the_future(self):
  m=engine.StreamMonitor(clock=lambda:100);m.started=80
  m.events.extend([dict(ts=90,message='known'),dict(ts=110,message='future')])
  m.history['s']=deque([(90,{'bitrate':10},'live'),(110,{'bitrate':999},'live')])
  self.assertNotIn('future',m.report(30,now=100));snap=m.snapshot(30,key='s')
  self.assertEqual(len(snap['history']['s']),1);self.assertEqual(len(snap['events']),1)
 def test_stream_quiet_and_appending_log_reuses_existing_labels(self):
  now=[1800000000];m=engine.StreamMonitor(clock=lambda:now[0]);p=stream_widgets.StreamPanel(monitor=m);p.timer.stop()
  try:
   m.events.extend(dict(ts=now[0]-5000+i,key='',severity='info',message='Synthetic '+str(i)) for i in range(5000));p.refresh()
   with patch.object(stream_widgets,'format_timestamp',wraps=stream_widgets.format_timestamp) as formatter:
    p.refresh();self.assertEqual(formatter.call_count,0)
   m.event('','info','one new event')
   with patch.object(stream_widgets,'format_timestamp',wraps=stream_widgets.format_timestamp) as formatter:
    p.refresh();self.assertEqual(formatter.call_count,1)
   self.assertEqual(len(p.event_labels),5000)
   self.assertIn('one new event',p.events.toPlainText());self.assertNotIn('Synthetic 0\n',p.events.toPlainText())
   now[0]+=43201;p.refresh();self.assertFalse(p.event_labels);self.assertEqual(p.events.toPlainText(),'')
  finally:p.shutdown();p.deleteLater();self.app.processEvents()
 def test_missing_rtt_does_not_break_continuous_congestion_curve(self):
  plot=stream_widgets.SeriesPlot('Synthetic',[dict(key='rtt',label='RTT',color='#42b4ff'),dict(key='congestion',label='Congestion',color='#f4cd35',axis='right',max=1)])
  plot.resize(400,180);plot.set_data([(i,{'rtt':None if i%2 else 10,'congestion':.1},'live') for i in range(7200)],7200,7200)
  image=QImage(400,180,QImage.Format.Format_ARGB32);painter=QPainter(image);segments=[]
  class Recording:
   def __getattr__(self,name):return getattr(painter,name)
   def drawLine(self,*args):
    if painter.pen().color()==QColor('#f4cd35'):segments.append(args)
    return painter.drawLine(*args)
  try:
   plot.draw_chart(Recording());coverage=sum(abs(line[2]-line[0]) for line in segments)
   self.assertGreater(coverage,plot.plot_rect().width()*.9)
  finally:painter.end();plot.deleteLater();self.app.processEvents()
 def test_dense_graphene_gaps_are_bounded_and_do_not_invent_lines(self):
  points=[(i,None if i%2 else i%101) for i in range(43200)];sampled=line_samples(points,0,43200,260,1.6)
  self.assertLess(len(sampled),260*10)
  self.assertEqual(max(v for _,v,_ in sampled if v is not None),100)
  for a,b in zip(sampled,sampled[1:]):
   if a[1] is not None and b[1] is not None and not b[2]:self.assertEqual(b[0]-a[0],1)
 def test_report_button_returns_without_waiting_and_rejects_duplicates(self):
  m=engine.StreamMonitor();p=stream_widgets.StreamPanel(monitor=m);p.timer.stop();begun=threading.Event();release=threading.Event();copied=[]
  def slow(*args):begun.set();release.wait(1);return 'Synthetic report'
  class Clipboard:
   def setText(self,text):copied.append(text)
  try:
   with patch.object(m,'report',side_effect=slow) as report,patch.object(stream_widgets.QApplication,'clipboard',return_value=Clipboard()):
    tick=time.monotonic();p.copy_report();elapsed=time.monotonic()-tick
    self.assertLess(elapsed,.2);self.assertTrue(begun.wait(.5))
    p.copy_report();self.assertEqual(report.call_count,1);self.assertFalse(p.copy_button.isEnabled())
    caption=p.copy_button.text();p.reset_copy_caption();self.assertEqual(p.copy_button.text(),caption)
    release.set();flush_report(p);self.assertEqual(copied,['Synthetic report']);self.assertTrue(p.copy_button.isEnabled());self.assertIsNone(p.report_worker.result)
  finally:release.set();p.shutdown();p.deleteLater();self.app.processEvents()
 def test_report_failure_restores_controls_and_allows_retry(self):
  m=engine.StreamMonitor();p=stream_widgets.StreamPanel(monitor=m);p.timer.stop();copied=[]
  class Clipboard:
   def setText(self,text):copied.append(text)
  try:
   with patch.object(m,'report',side_effect=[OSError('synthetic'), 'retry report']),patch.object(stream_widgets.QMessageBox,'warning') as warning,patch.object(stream_widgets.QApplication,'clipboard',return_value=Clipboard()):
    p.copy_report();flush_report(p);warning.assert_called_once();self.assertTrue(p.save_button.isEnabled())
    p.copy_report();flush_report(p);self.assertEqual(copied,['retry report'])
  finally:p.shutdown();p.deleteLater();self.app.processEvents()
 def test_network_reader_captures_widgets_before_background_thread(self):
  import desktop,server
  from ui_tasks import Call
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   w=desktop.Window(Path(folder),service_folder=False);w.timer.stop();w.sid='synthetic';calls=[]
   try:
    w.network_fields['ip'].setText('192.0.2.10');reader=w.network_report_job();w.network_fields['ip'].setText('CHANGED')
    def snapshot(*args,**kwargs):calls.append(QThread.currentThread()!=self.app.thread());return {'targets':[]}
    with patch.object(w.monitor,'snapshot',side_effect=snapshot):
     job=Call(reader);job.start();self.assertTrue(job.wait(3000));self.assertIsNone(job.error)
    self.assertEqual(calls,[True]);self.assertIn('192.0.2.10',job.result[0]);self.assertNotIn('CHANGED',job.result[0]);job.deleteLater()
   finally:close_window(w);w.store.db.close();w.deleteLater();self.app.processEvents()
 def test_close_waits_for_pending_report_without_late_clipboard_write(self):
  import desktop
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   w=desktop.Window(Path(folder),service_folder=False);w.timer.stop();begun=threading.Event();release=threading.Event();copied=[]
   def report(*args):begun.set();release.wait(2);return 'Synthetic report'
   class Clipboard:
    def setText(self,text):copied.append(text)
   try:
    with patch.object(w.stream_panel.monitor,'report',side_effect=report),patch.object(stream_widgets.QApplication,'clipboard',return_value=Clipboard()):
     w.stream_panel.copy_report();self.assertTrue(begun.wait(.5));w.close();self.assertFalse(w.closed_clean)
     release.set();close_window(w);self.assertEqual(copied,[])
   finally:release.set();close_window(w);w.store.db.close();w.deleteLater();self.app.processEvents()

if __name__=='__main__':unittest.main()

"""Reproduced display/interaction regressions; synthetic data and isolated widgets."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys,time,unittest,math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QPointF,Qt
from PySide6.QtWidgets import QApplication
import desktop,port_widgets,stream_widgets,stream_monitor,graphene_port
from plot_sampling import nearest_point,line_samples

class RoundTwo(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
 def test_continuous_graphene_history_keeps_its_fill_after_sampling(self):
  c=port_widgets.PortPlot('Traffic',('rx','tx'));c.resize(320,180)
  c.data={'begin':0,'end':43200,'step':1,'series':{'rx':[(i,100.) for i in range(43200)],'tx':[]}}
  c.show();self.app.processEvents();r=c.plot_rect();image=c.grab().toImage();y=int(r.top()+r.height()*.5);xs=range(int(r.left()+4),int(r.right()-4))
  coverage=sum(image.pixelColor(x,y).green()>65 for x in xs)/len(xs)
  self.assertGreater(coverage,.9);c.close();c.deleteLater()
 def test_graphene_receive_keeps_text_selection_for_same_source(self):
  panel=port_widgets.PortPanel();panel.connected=('demo-switch','Gi1/0/7',300)
  d=dict(host='demo-switch',port='Gi1/0/7',period=300,begin=1000,end=1300,latest=1300,step=30,series={k:[(1300,1)] for k in graphene_port.SENSORS})
  try:
   panel.receive(d);panel.host.setSelection(2,4);panel.port.setCursorPosition(3)
   panel.receive(dict(d));self.assertEqual(panel.host.selectedText(),'mo-s');self.assertEqual(panel.port.cursorPosition(),3)
  finally:panel.shutdown();panel.deleteLater()
 def test_stream_refresh_does_not_scroll_back_to_selected_first_stream(self):
  monitor=stream_monitor.StreamMonitor(clock=lambda:1000)
  for i in range(60):monitor.stream(f'tcp:{i:03}',1000,label=f'Encoder {i}',state='live',protocol='RTMP',target='ingest.example.test')
  panel=stream_widgets.StreamPanel(monitor=monitor);panel.timer.stop();panel.resize(900,640);panel.show();self.app.processEvents()
  try:
   table=panel.table;table.verticalScrollBar().setValue(table.verticalScrollBar().maximum());self.app.processEvents();position=table.verticalScrollBar().value()
   self.assertGreater(position,10);panel.render(monitor.snapshot(300,key=panel.selected));self.app.processEvents()
   self.assertEqual(table.verticalScrollBar().value(),position)
  finally:panel.shutdown();panel.close();panel.deleteLater()
 def test_graph_tooltip_distinguishes_local_error_from_missing_icmp(self):
  graph=desktop.Graph();graph.resize(400,180);graph.set_data([dict(ts=1000,status='error',latency=None,jitter=None)],1000,300)
  graph.mouseMoveEvent(SimpleNamespace(position=lambda:QPointF(380,50)))
  self.assertIn('ошибка',graph.toolTip().lower());self.assertNotIn('нет ответа',graph.toolTip().lower())
  graph.set_data([dict(ts=1000,status='unreachable',latency=None,jitter=None)],1000,300);graph.mouseMoveEvent(SimpleNamespace(position=lambda:QPointF(380,50)))
  self.assertIn('недоступна',graph.toolTip());graph.deleteLater()
 def test_large_graphene_tick_labels_fit_inside_reserved_margin(self):
  c=port_widgets.PortPlot('Packets',('rx_packets','tx_packets'));c.resize(240,180)
  c.data=dict(begin=0,end=300,step=30,series={'rx_packets':[(0,8_234_567.89)],'tx_packets':[]})
  for text in c.axis_labels():self.assertLessEqual(c.fontMetrics().horizontalAdvance(text),c.plot_rect().left()-5)
  self.assertGreater(c.plot_rect().width(),100);c.deleteLater()
 def test_chart_surface_invalidates_on_data_size_and_font_but_not_cursor(self):
  c=port_widgets.PortPlot('Traffic',('rx','tx'));c.resize(320,180)
  data=dict(begin=0,end=10,step=1,series={'rx':[(0,10),(10,20)],'tx':[]});c.data=data
  with patch.object(c,'draw_chart',wraps=c.draw_chart) as draw:
   c.grab();count=draw.call_count;c.cursor=5;c.grab();self.assertEqual(draw.call_count,count)
   c.data=dict(data);c.grab();self.assertGreater(draw.call_count,count);count=draw.call_count
   c.resize(400,180);c.grab();self.assertGreater(draw.call_count,count);count=draw.call_count
   font=c.font();font.setPixelSize(15);c.setFont(font);c.grab();self.assertGreater(draw.call_count,count)
  c.deleteLater()
 def test_pair_sampling_preserves_real_breaks_and_extrema(self):
  points=[(i,None if 200<=i<=205 else 999 if i==399 else 10) for i in range(1000)]
  points.extend((i,10) for i in range(1100,1200))
  sampled=line_samples(points,0,1200,20,1.6);by_time={p[0]:p for p in sampled}
  for t in (199,200,205,206,399,999,1100):self.assertIn(t,by_time)
  self.assertTrue(by_time[1100][2]);self.assertFalse(by_time[399][2])
 def test_exact_raw_hover_search_is_logarithmic_and_earlier_ties_are_stable(self):
  class Rows(list):
   reads=0
   def __getitem__(self,index):self.reads+=1;return super().__getitem__(index)
   def __iter__(self):
    for i in range(len(self)):yield self[i]
  rows=Rows((i,i*2) for i in range(100000))
  self.assertEqual(nearest_point(rows,12345.5),(12345,24690));self.assertLess(rows.reads,60)
  self.assertEqual(nearest_point([(1,'first'),(1,'second'),(3,'third')],2),(1,'first'))
 def test_local_errors_do_not_use_the_lost_packet_red_marker(self):
  c=desktop.Graph();c.resize(400,180);c.set_data([dict(ts=900,status='error',latency=None,jitter=None)],1000,300)
  image=c.grab().toImage();orange=red=0
  for y in range(12,140):
   for x in range(45,390):
    color=image.pixelColor(x,y)
    orange+=color.red()>90 and .75*color.red()<color.green()<.9*color.red() and color.blue()<.7*color.green()
    red+=color.red()>90 and color.green()<.75*color.red() and color.blue()>1.08*color.green()
  self.assertGreater(orange,20);self.assertEqual(red,0);c.deleteLater()
 def test_stream_hover_uses_sorted_unmodified_raw_points(self):
  c=stream_widgets.SeriesPlot('RTT',[dict(key='rtt',label='RTT',color='#60a5fa')]);c.resize(400,180)
  raw=[(100,{'rtt':1},'live'),(50,{'rtt':999},'live'),(0,{'rtt':2},'live')]
  c.set_data(raw,100,100);rect=c.plot_rect();c.mouseMoveEvent(SimpleNamespace(position=lambda:QPointF(rect.center().x(),50)))
  self.assertIn('999',c.toolTip());self.assertEqual(raw[0][0],100);c.deleteLater()
 def test_finite_extreme_metrics_do_not_overflow_chart_scales(self):
  c=stream_widgets.SeriesPlot('RTT',[dict(key='rtt',label='RTT',color='#60a5fa')]);c.resize(400,180)
  c.set_data([(1,{'rtt':1.7e308},'live')],1,1)
  top=c.scale(c.series[0]);self.assertTrue(math.isfinite(top));self.assertGreaterEqual(top,1.7e308);c.grab();c.deleteLater()
  p=port_widgets.PortPlot('Packets',('rx_packets','tx_packets'));p.resize(320,180)
  p.data=dict(begin=0,end=1,step=1,series={'rx_packets':[(1,1.7e308)],'tx_packets':[]})
  self.assertTrue(math.isfinite(p.chart_info()['maximum']));self.assertNotIn('—',p.axis_labels());p.grab();p.deleteLater()
 def test_surface_rebuilds_for_a_new_display_pixel_ratio(self):
  c=port_widgets.PortPlot('Traffic',('rx','tx'));c.resize(320,180);c.grab()
  with patch.object(c,'devicePixelRatioF',return_value=2.):
   c.grab();self.assertEqual(c._surface.width(),640);self.assertEqual(c._surface.height(),360);self.assertEqual(c._surface.devicePixelRatioF(),2.)
  c.deleteLater()
 def test_narrow_stream_chart_reserves_a_separate_legend_row(self):
  c=stream_widgets.SeriesPlot('RTT, мс · congestion',[dict(key='rtt',label='RTT',color='#60a5fa'),dict(key='congestion',label='congestion 0–1',color='#f4cd35',axis='right')]);c.resize(240,180)
  self.assertTrue(c.legend_wraps());self.assertGreaterEqual(c.plot_rect().top(),43);c.deleteLater()

if __name__=='__main__':unittest.main()

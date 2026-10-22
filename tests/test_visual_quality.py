"""Behavioral and pixel regressions for chart readability and coordinate mapping."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys,time,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication
import desktop,port_widgets,stream_widgets

def event(x,y=50):return SimpleNamespace(position=lambda:QPointF(x,y))

class VisualQuality(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
 def test_flap_chart_hover_at_drawn_right_edge_is_last_time(self):
  chart=port_widgets.PortPlot('Errors',('rx_errs','flap'));chart.resize(320,180)
  chart.data={'begin':1000,'end':1300,'step':5,'series':{'rx_errs':[(1000,0),(1300,2)],'flap':[(1000,0),(1300,1)]}}
  seen=[];chart.hovered.connect(seen.append);chart.mouseMoveEvent(event(chart.width()-34))
  self.assertAlmostEqual(seen[-1],1300)
  chart.mouseMoveEvent(event(0));self.assertEqual(seen[-1],1000);chart.deleteLater()
 def test_stream_right_axis_hover_uses_last_point(self):
  chart=stream_widgets.SeriesPlot('RTT',[{'key':'rtt','label':'RTT','color':'#60a5fa'},{'key':'congestion','label':'C','color':'#f4cd35','axis':'right'}]);chart.resize(320,180)
  # The penultimate point is deliberately closer under the old wrong geometry.
  points=[(1000,{'rtt':1},'live'),(1260,{'rtt':2},'live'),(1300,{'rtt':3},'live')]
  chart.set_data(points,1300,300);chart.mouseMoveEvent(event(chart.width()-44))
  self.assertTrue(chart.toolTip().startswith(time.strftime('%H:%M:%S',time.localtime(1300))));self.assertIn('RTT: 3',chart.toolTip());chart.deleteLater()
 def test_submillisecond_jitter_has_visible_vertical_range(self):
  chart=desktop.Graph(True);chart.resize(500,150);chart.set_data([{'ts':1000+i,'status':'ok','latency':10,'jitter':v} for i,v in enumerate([.05,.2,.4,.7,.1])],1004,4)
  chart.show();self.app.processEvents();image=chart.grab().toImage();ys=[]
  for y in range(15,chart.height()-25):
   for x in range(55,chart.width()-25):
    color=image.pixelColor(x,y)
    if color.red()>180 and 80<color.green()<190 and color.blue()<160:ys.append(y)
  self.assertTrue(ys);self.assertGreater(max(ys)-min(ys),50);chart.close();chart.deleteLater()
 def test_graphene_failure_marks_retained_history_and_recovery_clears_warning(self):
  from unittest.mock import patch
  import graphene_port
  panel=port_widgets.PortPanel();panel.connected=('qa-switch','Gi1/0/1',300)
  data={'host':'qa-switch','port':'Gi1/0/1','period':300,'begin':1000,'end':1300,'latest':1300,'step':5,'series':{key:[(1300,1)] for key in graphene_port.SENSORS}}
  try:
   with patch('time.time',return_value=1300):
    panel.receive(data);panel.failure((panel.connected,'Источник недоступен'))
    self.assertIs(panel.plots[0].data,data);self.assertTrue(panel.status.text().startswith('Нет свежих данных'))
    self.assertIn('#ef6275',panel.status.styleSheet());panel.receive(data)
    self.assertNotIn('Нет свежих данных',panel.status.text());self.assertIn('#91a2bb',panel.status.styleSheet())
  finally:panel.shutdown();panel.deleteLater()
 def test_programmatic_long_server_shows_beginning(self):
  field=desktop.ServerSelector();field.setText('long-ingest-server.example.test')
  self.assertEqual(field.lineEdit().cursorPosition(),0);self.assertEqual(field.text(),'long-ingest-server.example.test');field.deleteLater()

if __name__=='__main__':unittest.main()

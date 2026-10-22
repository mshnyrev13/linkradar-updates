from qt_support import close_window,flush_io,managed_window,production_application
import os,sys,tempfile,unittest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import desktop
from PySide6.QtGui import QPainter,QImage
from PySide6.QtCore import QRectF,QCoreApplication,QEvent

def settle(app):
 # Block balancing runs from a zero-delay timer queued by resize events.
 for _ in range(3):app.processEvents()

class RouteDisplayTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=production_application(cls)
 def test_expanded_route_shows_fifteen_hops_and_mean_curve(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),managed_window(desktop.Window(Path(folder))) as window:
   window.timer.stop();window.table.setRowCount(20)
   for row in range(20):
    for col in range(9):
     item=desktop.QTableWidgetItem(str(row+1) if col==0 else '')
     if col==8:item.setData(desktop.Qt.ItemDataRole.UserRole,(None,None,None,None,300) if row==1 else (5,40+row*7,200,30,300))
     window.table.setItem(row,col,item)
   window.resize(1100,1100);window.show()
   settle(self.app)
   window.panes.setSizes([680,400]);self.app.processEvents()
   rect=window.table.visualItemRect(window.table.item(14,8))
   self.assertLessEqual(rect.bottom(),window.table.viewport().height())
   img=window.table.viewport().grab().toImage()
   a=QRectF(window.table.visualItemRect(window.table.item(0,8))).adjusted(8,0,-8,0)
   b=QRectF(window.table.visualItemRect(window.table.item(2,8))).adjusted(8,0,-8,0)
   x=int((a.left()+a.width()*40/300+b.left()+b.width()*54/300)/2);y=int((a.center().y()+b.center().y())/2)
   self.assertTrue(any(img.pixelColor(x+dx,y+dy).green()>140 for dx in range(-2,3) for dy in range(-2,3)))
 def test_three_columns_resize_without_resetting_manual_widths(self):
  def proportional(sizes):
   unit=[size/weight for size,weight in zip(sizes,desktop.BLOCK_WEIGHTS)]
   self.assertLessEqual(max(unit)-min(unit),3)
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'):
   window=desktop.Window(Path(folder));window.timer.stop();window.resize(1440,960);window.show();settle(self.app)
   self.assertEqual(window.panes.orientation(),desktop.Qt.Orientation.Horizontal)
   x=lambda widget:widget.mapTo(window,widget.rect().topLeft()).x()
   self.assertLess(x(window.address),x(window.table));self.assertLess(x(window.table),x(window.graph))
   self.assertEqual(window.panes.widget(0).height(),window.panes.widget(1).height())
   before=[window.port_splitter.sizes()[0],window.traffic_splitter.widget(1).height()];proportional(before)
   window.resize(1440,1100);settle(self.app)
   after=[window.port_splitter.sizes()[0],window.traffic_splitter.widget(1).height()];proportional(after)
   self.assertTrue(all(a>b for a,b in zip(after,before)))
   width=window.table.width();window.panes.setSizes([480,720]);settle(self.app)
   self.assertLess(window.table.width(),width)
   manually_set=window.panes.sizes()
   window.resize(1440,960);settle(self.app)
   self.assertEqual(window.panes.sizes(),manually_set)
   proportional([window.port_splitter.sizes()[0],window.traffic_splitter.widget(1).height()])
   close_window(window);window.store.db.close();window.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
 def test_all_latency_bands_are_visible_on_wide_scale(self):
  img=QImage(100,300,QImage.Format.Format_RGB32);img.fill(0);p=QPainter(img)
  desktop.latency_bands(p,QRectF(0,0,100,300),300);p.end()
  self.assertEqual(img.pixelColor(50,250).name(),'#17352b')
  self.assertEqual(img.pixelColor(50,150).name(),'#403820')
  self.assertEqual(img.pixelColor(50,50).name(),'#40252f')
 def test_route_scale_includes_full_range(self):
  hops=[{'stats':{'avg':12,'max':93},'last':{'latency':8}}]
  self.assertEqual(desktop.route_scale(hops),96)
  hops.append({'stats':{'avg':None,'max':None},'last':{'latency':None}})
  self.assertEqual(desktop.route_scale(hops),96)
  self.assertEqual(desktop.route_scale([]),10)
 def test_window_resize_shrinks_table_and_graphs_without_losing_hops(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'):
   window=desktop.Window(Path(folder));window.timer.stop();window.table.setRowCount(20)
   for row in range(20):window.table.setItem(row,0,desktop.QTableWidgetItem(str(row+1)))
   window.resize(1100,960);window.show();settle(self.app)
   large=(window.height(),window.table.viewport().height(),window.jitter.height(),window.graph.height())
   window.resize(1100,700);settle(self.app)
   small=(window.height(),window.table.viewport().height(),window.jitter.height(),window.graph.height())
   for before,after in zip(large,small):self.assertLessEqual(after,before)
   self.assertLess(small[0],large[0]);self.assertLess(small[1],large[1])
   self.assertLess(sum(small[2:]),sum(large[2:]))
   self.assertLess(small[1]//20,large[1]//20)
   self.assertEqual(window.table.rowCount(),20)
   self.assertGreater(window.table.verticalScrollBar().maximum(),0)
   window.table.scrollToItem(window.table.item(19,0));self.app.processEvents()
   self.assertLess(window.table.visualItemRect(window.table.item(19,0)).bottom(),window.table.viewport().height())
   window.resize(1440,1000);settle(self.app);before_drag=window.table.viewport().width()
   window.panes.setSizes([480,720]);settle(self.app)
   self.assertLess(window.table.viewport().width(),before_drag)
   close_window(window);window.store.db.close();window.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
if __name__=='__main__':unittest.main()

import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys,time,tempfile,threading,gc,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import desktop,server,background_engine,port_widgets as ui,graphene_port as source
from PySide6.QtCore import QCoreApplication,QEvent


class DisconnectedPort(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=desktop.QApplication.instance() or desktop.QApplication([])
 def test_disconnect_keeps_history_same_lldp_resumes_and_new_port_replaces(self):
  panel=ui.PortPanel()
  try:
   with patch.object(ui.PortFetch,'start'):
    neighbor={'switch':'switch-a.example.test','port':'Gi1/0/17'};panel.lldp(neighbor);key=panel.connected
    series={sensor:[(990,1),(1000,2)] for sensor in source.SENSORS}
    data={'host':'switch-a','port':'GigabitEthernet1/0/17','period':300,'requested':key,'begin':700,'end':1000,'series':series,**source.info(series)}
    panel.receive(data);panel.lldp({})
    self.assertIs(panel.snapshot,data);self.assertEqual(panel.connected,key);self.assertFalse(panel.timer.isActive())
    self.assertEqual(panel.host.text(),'switch-a');self.assertEqual(panel.port.text(),'GigabitEthernet1/0/17')
    self.assertTrue(all(plot.data is data for plot in panel.plots));self.assertIn('последние данные',panel.status.text())
    panel.receive({**data,'end':2000});self.assertIs(panel.snapshot,data)
    with patch.object(panel,'refresh') as refresh:
     panel.lldp(neighbor);refresh.assert_called_once()
    self.assertTrue(panel.timer.isActive());self.assertIs(panel.snapshot,data)
    self.assertEqual(panel.resolved,('switch-a','GigabitEthernet1/0/17'))
    panel.lldp({});panel.period.setCurrentIndex(0);self.assertIs(panel.snapshot,data)
    self.assertTrue(all(plot.data['period']==30 for plot in panel.plots))
    panel.lldp({'switch':'switch-a.example.test','port':'Gi1/0/18'})
    self.assertIsNone(panel.snapshot);self.assertEqual(panel.connected[:2],('switch-a.example.test','Gi1/0/18'))
    panel.receive(data);self.assertIsNone(panel.snapshot);self.assertTrue(panel.timer.isActive())
  finally:
   panel.shutdown();panel.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()


class PauseEngine(unittest.TestCase):
 def wait_for(self,predicate):
  deadline=time.monotonic()+3
  while time.monotonic()<deadline:
   if predicate():return
   time.sleep(.01)
  self.fail('State did not settle')
 def test_inflight_ping_is_discarded_and_same_session_continues(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   store=server.Store(Path(folder)/'db');monitor=server.Monitor(store)
   sid=monitor.start({'continuous':True,'interval':.5,'timeout':.5,'targets':[{'host':'ya.ru'}]});config=monitor.config;target=config['targets'][0]
   inflight=threading.Event();release=threading.Event();calls=[]
   def probe(*args):
    calls.append(time.time())
    if len(calls)==2:inflight.set();release.wait(3)
    return {'ts':time.time(),'status':'ok','latency':10,'ip':'192.0.2.1','detail':''}
   with patch.object(server,'probe',side_effect=probe):
    worker=threading.Thread(target=monitor.worker,args=(sid,target,config,time.time(),monitor.stop_event));worker.start()
    try:
     self.assertTrue(inflight.wait(2));self.assertTrue(monitor.pause(sid));release.set()
     count=len(store.rows('SELECT * FROM samples'));self.assertEqual(count,1)
     before=monitor.snapshot(sid)['session']['analysis_end'];time.sleep(.6)
     self.assertEqual(len(calls),2);self.assertEqual(len(store.rows('SELECT * FROM samples')),count)
     self.assertEqual(monitor.snapshot(sid)['session']['analysis_end'],before)
     monitor.set_interval(1);self.assertEqual(monitor.sid,sid);self.assertTrue(monitor.paused)
     self.assertFalse(monitor.pause(sid));self.wait_for(lambda:len(store.rows('SELECT * FROM samples'))>count)
     self.assertEqual(monitor.sid,sid)
    finally:
     release.set();monitor.stop();worker.join(4);self.wait_for(lambda:monitor.sid is None);store.db.close()
 def test_pause_cancels_inflight_traceroute(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   store=server.Store(Path(folder)/'db');monitor=server.Monitor(store);sid=monitor.start({'continuous':True,'targets':[{'host':'192.0.2.1'}]})
   entered=threading.Event();cancelled=threading.Event()
   def trace(ip,timeout,max_hops,cancel,save):
    entered.set()
    while not cancel.is_set():time.sleep(.01)
    cancelled.set();save({'ts':time.time(),'hop':1,'ip':ip,'status':'ok','latency':3,'detail':''})
    return {'hops':[],'state':'partial'}
   with patch.object(server,'trace_cycle',side_effect=trace):
    worker=threading.Thread(target=monitor.route_worker,args=(sid,monitor.config['targets'][0],monitor.config,time.time(),monitor.stop_event,False));worker.start()
    try:
     self.assertTrue(entered.wait(2));monitor.pause(sid);self.assertTrue(cancelled.wait(2));time.sleep(.05)
     self.assertEqual(store.rows('SELECT * FROM hop_samples'),[]);self.assertEqual(store.rows('SELECT * FROM routes'),[])
    finally:monitor.stop();worker.join(3);self.wait_for(lambda:monitor.sid is None);store.db.close()
 def test_background_pause_survives_gui_reconnect_and_recovery(self):
  with tempfile.TemporaryDirectory(prefix='nw-') as folder,patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   stop=threading.Event();worker=threading.Thread(target=background_engine.run,args=(folder,stop));worker.start()
   try:
    self.wait_for(lambda:background_engine.connect(folder) is not None)
    proxy=background_engine.connect(folder);sid=proxy.start({'continuous':True,'targets':[{'host':'ya.ru'}]})
    self.assertTrue(proxy.pause(sid));proxy.set_interval(.5)
    second=background_engine.connect(folder);self.assertEqual(second.sid,sid);self.assertTrue(second.status['paused'])
    self.assertIn('pause',second.status['capabilities']);self.assertEqual(second.status['config']['interval'],.5)
    store=server.Store(Path(folder)/'network.sqlite3',recover=False);recovered=server.Monitor(store)
    recovered.resume(sid);self.assertTrue(recovered.paused);self.assertTrue(recovered.probe_cancel.is_set())
    recovered.stop();self.wait_for(lambda:recovered.sid is None);store.db.close()
   finally:stop.set();worker.join(6)
   self.assertFalse(worker.is_alive())


if __name__=='__main__':unittest.main()

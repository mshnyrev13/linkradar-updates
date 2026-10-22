import os,sys,json,tempfile,unittest,gc
from pathlib import Path
from unittest.mock import Mock,patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import graphene_port as g
import port_widgets as ui
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication,QEvent

def streaming_response(r):
 def chunks(*args):
  try:return iter([json.dumps(r.json()).encode()])
  except ValueError:return iter([r.content])
 r.iter_content.side_effect=chunks
 return r

def mock_client():
 client=Mock();streaming_response(client.get.return_value);return client

class GrapheneDataTests(unittest.TestCase):
 def catalog(self,*answers):
  client=mock_client();responses=[]
  for values in answers:
   r=streaming_response(Mock());r.status_code=200;r.json.return_value={'ok':True,'result':{'suggest':values}};responses.append(r)
  client.post.side_effect=responses;return client
 def test_lldp_aliases_use_verified_catalog(self):
  client=self.catalog([],['switch-a'],['GigabitEthernet1/0/16','GigabitEthernet1/0/17'])
  self.assertEqual(g.resolve('switch-a.yndx.net','Gi1/0/17',client),('switch-a','GigabitEthernet1/0/17'))
  self.assertEqual(client.post.call_args.kwargs['json']['fields']['host'],'switch-a')
  self.assertTrue(client.post.call_args.kwargs['verify'])
 def test_exact_catalog_name_preferred(self):
  client=self.catalog(['switch.yndx.net'],['Gi1/0/17','GigabitEthernet1/0/17'])
  self.assertEqual(g.resolve('switch.yndx.net','Gi1/0/17',client),('switch.yndx.net','Gi1/0/17'))
 def test_unknown_or_ambiguous_catalog_is_rejected(self):
  for client in (self.catalog(['switch-other']),self.catalog(['switch'],['Gi1/0/16']),self.catalog(['switch'],['Gi1/0/17','GigabitEthernet1/0/17'])):
   with self.assertRaises(g.SourceError):g.resolve('switch','gigabitethernet 1/0/17',client)
 def test_exact_port_filter_and_window(self):
  client=mock_client();client.get.return_value.status_code=200;client.get.return_value.content=b'{}'
  client.get.return_value.json.return_value={'results':[{'series':[{'columns':['time','rx'],'values':[[950000,10],[980000,None],[1000000,20]]}]}]}
  data=g.fetch('switch-01','Gi1/0/3',30,end=1000,session=client)
  query=json.loads(client.get.call_args.kwargs['params']['q'])
  self.assertEqual(query['filter'][0]['host'],['switch-01']);self.assertEqual(query['filter'][0]['iface'],['Gi1/0/3'])
  self.assertEqual(data['series']['rx'],[(980,None),(1000,20)])
  self.assertEqual(data['series']['tx'],[]);self.assertEqual(data['step'],25)
  self.assertFalse(client.get.call_args.kwargs['allow_redirects'])
 def test_short_window_ends_at_real_delayed_samples(self):
  client=mock_client();r=client.get.return_value;r.status_code=200;r.content=b'{}'
  r.json.return_value={'results':[{'series':[{'columns':['time','rx'],'values':[[770000,1],[800000,2],[830000,None],[860000,3]]}]}]}
  for period in (30,60,300):
   data=g.fetch('switch','Gi1/0/17',period,end=1000,session=client)
   self.assertEqual(data['end'],860);self.assertEqual(data['begin'],860-period)
   self.assertEqual(data['observed_end'],1000);self.assertEqual(data['latest'],860)
   self.assertEqual(data['series']['rx'][-1],(860,3))
   self.assertTrue(all(860-period<=ts<=860 for ts,v in data['series']['rx']))
   self.assertEqual(data['step'],30)
  self.assertEqual(g.fetch('switch','Gi1/0/17',30,end=1000,session=client)['series']['rx'],[(830,None),(860,3)])
 def test_empty_history_stays_empty(self):
  client=mock_client();r=client.get.return_value;r.status_code=200;r.content=b'{}';r.json.return_value={'results':[]}
  data=g.fetch('switch','Gi1/0/17',60,end=1000,session=client)
  self.assertIsNone(data['latest']);self.assertEqual((data['begin'],data['end']),(940,1000))
  self.assertTrue(all(not points for points in data['series'].values()))
 def test_missing_invalid_and_ambiguous_are_not_zeros(self):
  raw={'results':[{'series':[{'columns':['time','tx'],'values':[[1000000,None],[1010000,float('nan')],[1020000,-1]]}]}]}
  data=g.decode(raw,1000,1100);self.assertEqual(data['tx'],[(1000,None),(1010,None),(1020,None)])
  self.assertIsNone(g.info(data)['latest'])
  raw['results'][0]['series'].append({'columns':['time','tx'],'values':[[1000000,3]]})
  with self.assertRaises(g.SourceError):g.decode(raw,1000,1100)
  with self.assertRaises(g.SourceError):g.decode({'results':[{'error':'private details'}]},1000,1100)
 def test_no_wildcards_or_foreign_destination(self):
  for host,port in [('switch*','Gi1/0/3'),('https://evil','Gi1/0/3'),('switch','Gi*'),('switch','x" sensor=flap')]:
   with self.assertRaises(g.SourceError):g.validate_target(host,port)
  self.assertIn('graphene.yandex-team.ru',g.url('switch','Gi1/0/3',300))
 def test_auth_redirect_and_html_do_not_become_empty_success(self):
  client=mock_client();r=client.get.return_value;r.status_code=302
  with self.assertRaises(g.SourceError):g.fetch('switch','Gi1/0/3',300,session=client)
  r.status_code=200;r.content=b'html';r.json.side_effect=ValueError()
  with self.assertRaises(g.SourceError):g.fetch('switch','Gi1/0/3',300,session=client)

class GrapheneUiTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
 def test_canonical_source_survives_lldp_and_focus_changes(self):
  panel=ui.PortPanel()
  try:
   with patch.object(ui.PortFetch,'start'):
    neighbor={'switch':'switch-a.example.test','port':'Gi1/0/17'};panel.lldp(neighbor)
    key=panel.connected;series={sensor:[(1000,1)] for sensor in g.SENSORS}
    data={'host':'switch-a','port':'GigabitEthernet1/0/17','period':300,'requested':key,'begin':700,'end':1000,'series':series,**g.info(series)}
    panel.receive(data);worker=panel.worker;panel.lldp(neighbor)
    self.assertIs(panel.worker,worker);self.assertIs(panel.snapshot,data)
    self.assertEqual(panel.host.text(),'switch-a');self.assertEqual(panel.port.text(),'GigabitEthernet1/0/17')
    panel.manual();panel.source_edited();self.assertIs(panel.snapshot,data)
    panel.period.setCurrentIndex(3);self.assertEqual(panel.resolved,('switch-a','GigabitEthernet1/0/17'))
  finally:
   panel.shutdown();panel.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()
 def test_completed_entry_starts_and_new_entry_clears_old_source(self):
  panel=ui.PortPanel()
  try:
   with patch.object(ui.PortFetch,'start'):
    panel.host.setText('switch');panel.host.textEdited.emit('switch');panel.host.editingFinished.emit()
    self.assertIsNone(panel.connected)
    panel.port.setText('Gi1/0/3');panel.port.textEdited.emit('Gi1/0/3');panel.port.editingFinished.emit()
    self.assertEqual(panel.connected,('switch','Gi1/0/3',300))
    panel.port.setText('Gi1/0/4');panel.port.textEdited.emit('Gi1/0/4')
    self.assertIsNone(panel.connected);self.assertIsNone(panel.snapshot)
    panel.port.editingFinished.emit();self.assertEqual(panel.connected,('switch','Gi1/0/4',300))
    panel.failure((panel.connected,'Graphene недоступен'))
    self.assertTrue(all(p.message=='Нет свежих данных · Graphene недоступен' for p in panel.plots))
  finally:
   panel.shutdown();panel.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()
 def test_source_change_discards_stale_response_and_flap_scale(self):
  panel=ui.PortPanel()
  try:
   with patch.object(ui.PortFetch,'start'):
    panel.host.setText('switch');panel.port.setText('Gi1/0/3');panel.connect_source()
    end=1000;series={key:[(990,1),(1000,None)] for key in g.SENSORS}
    data={'host':'switch','port':'Gi1/0/3','period':300,'begin':700,'end':end,'series':series,**g.info(series)}
    panel.receive(data);self.assertIsNotNone(panel.snapshot)
    panel.resize(1100,145);panel.show();self.app.processEvents()
    self.assertFalse(panel.grab().isNull())
    panel.period.setCurrentIndex(0);self.assertIsNone(panel.snapshot)
    panel.receive(data);self.assertIsNone(panel.snapshot)
    panel.manual();panel.lldp({'switch':'different','port':'Gi1/0/4'});self.assertEqual(panel.connected[:2],('switch','Gi1/0/3'))
  finally:
   panel.shutdown();panel.close();panel.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()

if __name__=='__main__':unittest.main()

from qt_support import close_window,flush_io,managed_window,production_application,settle_layout
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys,json,tempfile,time,threading,gc,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import desktop,server,session_export,background_engine
from PySide6.QtCore import QCoreApplication,QEvent

class SessionExport(unittest.TestCase):
 def test_continuous_session_has_no_automatic_deadline(self):
  config=server.validate_config({'continuous':True,'targets':[{'host':'ya.ru'}]})
  self.assertEqual(server.session_deadline(config,1),float('inf'))
  self.assertEqual(server.session_deadline({**config,'continuous':False},1),901)
 def test_all_rows_and_extra_data_without_viewport_limit(self):
  with tempfile.TemporaryDirectory() as folder:
   s=server.Store(Path(folder)/'db');config=server.validate_config({'targets':[{'host':'ya.ru'}]})
   s.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',('s',1,None,'running',json.dumps(config)))
   target=config['targets'][0]['id']
   for i in range(205):
    s.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',('s',target,i,'ok',12,'192.0.2.1',''))
    s.event('s',target,'info','event '+str(i))
   session_export.record(s,'s','speed','test',{'download':100,'upload':80})
   session_export.record(s,'s','graphene','sensor',{'value':None},2)
   text=session_export.export_txt(s,'s');doc=json.loads(text[text.index('{'):])
   self.assertEqual(len(doc['samples']),205);self.assertEqual(len(doc['events']),205)
   self.assertEqual(doc['statistics'][target]['avg'],12)
   self.assertEqual(len(doc['session_data']),2)
   attached=server.Store(Path(folder)/'db',recover=False)
   self.assertEqual(attached.rows('SELECT status FROM sessions')[0]['status'],'running')
   self.assertEqual(len(attached.rows('SELECT * FROM events')),205);attached.db.close();s.db.close()

class Controls(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=production_application(cls)
 def test_unchanged_gateway_preserves_popup_highlight_and_edit_cursor(self):
  field=desktop.ServerSelector();field.set_gateway('192.0.2.1');field.resize(260,30);field.show();field.showPopup();self.app.processEvents()
  try:
   field.view().setCurrentIndex(field.model().index(3,0))
   changed=[];field.model().rowsRemoved.connect(lambda *args:changed.append('removed'));field.model().rowsInserted.connect(lambda *args:changed.append('inserted'));field.model().dataChanged.connect(lambda *args:changed.append('data'))
   for _ in range(20):field.set_gateway('192.0.2.1')
   self.assertEqual(changed,[]);self.assertEqual(field.view().currentIndex().row(),3)
   field.hidePopup();field.setText('example.test');field.lineEdit().setSelection(2,4);field.set_gateway('192.0.2.1')
   self.assertEqual(field.lineEdit().selectedText(),'ampl')
  finally:field.hidePopup();field.close();field.deleteLater()
 def test_changed_gateway_updates_existing_row_without_changing_typed_server(self):
  field=desktop.ServerSelector();field.set_gateway('192.0.2.1');field.setText('example.test');field.lineEdit().setCursorPosition(3)
  changes=[];field.model().rowsRemoved.connect(lambda *args:changes.append('removed'));field.model().rowsInserted.connect(lambda *args:changes.append('inserted'))
  field.set_gateway('192.0.2.2')
  self.assertEqual(changes,[]);self.assertEqual(field.itemData(3),'192.0.2.2');self.assertEqual(field.text(),'example.test');self.assertEqual(field.lineEdit().cursorPosition(),3)
  field.choose_preset(3);self.assertEqual(field.text(),'192.0.2.2');field.set_gateway(None);self.assertEqual(field.count(),3);self.assertEqual(field.text(),'192.0.2.2');field.deleteLater()

 def test_server_presets_and_editable_address(self):
  field=desktop.ServerSelector();self.assertEqual([field.itemText(i) for i in range(field.count())],['src-ugc.strm.yandex.net','ya.ru','google.com'])
  field.setText('192.0.2.10');self.assertEqual(field.text(),'192.0.2.10');field.deleteLater()
 def test_log_scroll_and_selection_stay_while_updates_arrive(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'):
   w=desktop.Window(Path(folder));w.timer.stop();w.logs.show();w.show();self.app.processEvents()
   events=[{'ts':i,'message':'event '+str(i)} for i in range(300)]
   w.update_logs(list(reversed(events)));self.app.processEvents()
   bar=w.logs.verticalScrollBar();bar.setValue(30);cursor=w.logs.textCursor();cursor.setPosition(15);cursor.setPosition(22,cursor.MoveMode.KeepAnchor);w.logs.setTextCursor(cursor)
   bar.setValue(30);selection=w.logs.textCursor().selectedText()
   w.update_logs(list(reversed(events)));self.assertEqual(bar.value(),30);self.assertEqual(w.logs.textCursor().selectedText(),selection)
   events.append({'ts':301,'message':'new'});w.update_logs(list(reversed(events)));self.assertEqual(bar.value(),30)
   self.assertIn('event 0',w.logs.toPlainText());self.assertIn('new',w.logs.toPlainText())
   close_window(w);w.store.db.close();w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect()
 def test_graphene_height_resizes_and_polls_every_five_seconds(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),managed_window(desktop.Window(Path(folder))) as w:
   w.show();settle_layout(self.app);before=w.port_panel.plots[0].height()
   w.port_splitter.setSizes([300,500]);self.app.processEvents()
   self.assertGreater(w.port_panel.plots[0].height(),before);self.assertEqual(w.port_panel.timer.interval(),5000)
   self.assertGreater(w.speed_button.mapTo(w,w.speed_button.rect().topLeft()).y(),w.address.mapTo(w,w.address.rect().topLeft()).y())
   self.assertEqual(w.speed_button.parent(),w.address.parent())
 def test_route_parser_uses_probe_rtt_not_process_duration(self):
  from route_engine import parse_hop
  self.assertEqual(parse_hop(' 3 2a00:1248:7ffd:1::82 21.630 ms')['latency'],21.630)

class BackgroundService(unittest.TestCase):
 def test_pipe_acl_rejects_untrusted_sid_before_loading_windows_api(self):
  import windows_pipe
  with self.assertRaises(ValueError):windows_pipe.configure('S-1-5-21)Everyone')
 @classmethod
 def setUpClass(cls):cls.app=desktop.QApplication.instance() or desktop.QApplication([])
 def test_gui_close_clears_background_session_and_export_data(self):
  with tempfile.TemporaryDirectory(prefix='nw-') as folder,patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   stop=threading.Event();worker=threading.Thread(target=background_engine.run,args=(folder,stop),daemon=True);worker.start()
   deadline=time.monotonic()+5;proxy=None
   while time.monotonic()<deadline:
    proxy=background_engine.connect(folder)
    if proxy:break
    time.sleep(.02)
   self.assertIsNotNone(proxy)
   try:
    config=server.validate_config({'targets':[{'host':'ya.ru'}]});sid=proxy.start(config)
    second=background_engine.connect(folder);self.assertEqual(second.sid,sid)
    result=background_engine.request(folder,'export',sid=sid);self.assertIn(sid,result)
    with patch.object(desktop.Discovery,'start'):
     window=desktop.Window(Path(folder))
     try:
      # Current engine: the GUI opens no database and adds session data through IPC only.
      self.assertTrue(window.remote);self.assertIsNone(window.store);self.assertEqual(window.sid,sid)
      window.record('speed','gui',{'download':7});window.note('synthetic GUI event');flush_io(window)
      self.assertIsNone(window.data_error,window.data_error)
      exported=background_engine.request(folder,'export',sid=sid);self.assertIn('synthetic GUI event',exported);self.assertIn('"download": 7',exported)
     finally:
      close_window(window);window.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    self.assertIsNone(background_engine.request(folder,'status')['active_id'])
    snapshot=second.snapshot(sid,300);self.assertIsNone(snapshot['active_id']);self.assertIsNone(snapshot['session'])
    with self.assertRaises(RuntimeError):background_engine.request(folder,'export',sid=sid)
    db=server.Store(Path(folder)/'network.sqlite3',recover=False)
    for table in ('sessions','samples','hop_samples','routes','events','session_data'):self.assertEqual(db.rows('SELECT * FROM '+table),[])
    time.sleep(.6);self.assertEqual(db.rows('SELECT * FROM events'),[]);db.db.close()
    with self.assertRaises(RuntimeError):background_engine.request(folder,'shell')
   finally:stop.set();worker.join(8)
   self.assertFalse(worker.is_alive())

if __name__=='__main__':unittest.main()

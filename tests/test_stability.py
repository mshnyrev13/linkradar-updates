"""Regressions from the 2.9.3 audit; synthetic data, no hardware/network probes."""
from qt_support import close_window,flush_io
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import io,json,tempfile,time,threading,unittest,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server,measurement_metrics as mm,stream_monitor,desktop,session_export,export_transfer,graphene_port,dns_resolver
from PySide6.QtCore import QCoreApplication,QEvent

class Correctness(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=desktop.QApplication.instance() or desktop.QApplication([])
 def test_current_values_do_not_come_from_peak_bucket(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   w=desktop.Window(Path(folder));w.timer.stop();w.address.setText('192.0.2.1');w.toggle();sid=w.sid;tid=w.monitor.config['targets'][0]['id'];now=time.time()
   w.store.execute('UPDATE sessions SET started=? WHERE id=?',(now-451,sid))
   w.store.db.executemany('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',((sid,tid,now-451+i*.5,'ok',200 if i==900 else 10,'192.0.2.1','') for i in range(902)));w.store.db.commit()
   try:
    for period in (600,3600,43200):
     snap=w.monitor.snapshot(sid,period);w.render(snap)
     self.assertEqual(w.route_summary.current,10);self.assertEqual(w.cards[0].text(),'23,0 мс')
   finally:close_window(w);w.store.db.close();w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
 def test_stale_probe_not_presented_as_live(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   w=desktop.Window(Path(folder));w.timer.stop();w.address.setText('192.0.2.1');w.toggle();sid=w.sid;tid=w.monitor.config['targets'][0]['id']
   try:
    w.store.execute('UPDATE sessions SET started=? WHERE id=?',(time.time()-120,sid));w.store.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',(sid,tid,time.time()-60,'ok',10,'192.0.2.1',''))
    w.render(w.monitor.snapshot(sid));self.assertEqual(w.live.text(),'НЕТ СВЕЖИХ ПРОБ')
   finally:close_window(w);w.store.db.close();w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
 def test_metrics_export_and_accumulator_agree_and_break_on_path(self):
  points=[dict(ts=i*.5,status='ok',latency=v,ip='192.0.2.1',detail='Адаптер en7 (192.0.2.10) · echo') for i,v in enumerate([10,170])]
  a=server.Accumulator(.5)
  for p in points:a.add(p)
  self.assertEqual(a.stats(0,.5)['jitter'],10);self.assertEqual(server.metrics(points,.5)['jitter'],10)
  points[1]['detail']='Адаптер en0 (192.0.2.11) · echo';a=server.Accumulator(.5)
  for p in points:a.add(p)
  self.assertIsNone(a.stats(0,.5)['jitter']);self.assertIsNone(server.metrics(points,.5)['jitter'])
 def test_stream_cannot_start_second_worker_while_old_cycle_finishes(self):
  m=stream_monitor.StreamMonitor();entered=threading.Event();release=threading.Event();calls=[]
  def cycle():calls.append(threading.get_ident());entered.set();release.wait(3)
  m.cycle=cycle;m.start();self.assertTrue(entered.wait(1));old=m.thread
  try:
   m.pause();m.start();self.assertIs(m.thread,old);self.assertEqual(len(calls),1)
   release.set();old.join(2);self.assertFalse(old.is_alive());m.start();self.assertIsNot(m.thread,old)
  finally:release.set();m.stop_event.set();m.thread.join(2);old.join(2)
 def test_probe_timestamp_excludes_slow_access_preparation(self):
  with patch.object(server.time,'time',side_effect=[100,120]),patch.object(server.interface_probes,'binding_for',return_value={'id':'en7'}),patch.object(server.interface_probes,'echo',return_value=dict(kind='echo',latency=18,ip='192.0.2.1')),patch.object(server.interface_probes,'detail',return_value='bound'):
   answer=server.probe({'host':'192.0.2.1','mode':'icmp'},.5,'192.0.2.1',interface='en7')
  self.assertEqual(answer['ts'],120);self.assertEqual(answer['latency'],18)
 def test_local_route_errors_are_not_counted_as_transit_loss(self):
  result=server.parse_ping('ping: sendto: No route to host',2)
  self.assertEqual(result['status'],'error');self.assertIsNone(server.metrics([dict(result,ts=1,ip='192.0.2.1')])['loss'])
 def test_remote_icmp_unreachable_is_a_failed_attempt_not_a_local_error(self):
  with patch.object(server.interface_probes,'binding_for',return_value={'id':'en7'}),patch.object(server.interface_probes,'echo',return_value=dict(kind='unreachable',latency=18,ip='192.0.2.254')),patch.object(server.interface_probes,'detail',return_value='unreachable'):
   answer=server.probe({'host':'192.0.2.1','mode':'icmp'},.5,'192.0.2.1',interface='en7')
  self.assertEqual(answer['status'],'unreachable');self.assertIsNone(answer['latency'])
  self.assertEqual(server.metrics([answer])['loss'],100);self.assertEqual(server.metrics([answer])['errors'],0)
 def test_scoped_ipv6_route_recognizes_an_unscoped_echo_peer(self):
  import route_engine
  binding=dict(id='en7',index=7,source='fe80::2')
  with patch.object(server.interface_probes,'binding_for',return_value=binding),patch.object(server.interface_probes,'echo',return_value=dict(kind='echo',latency=2,ip='fe80::1')):
   route=route_engine.trace_cycle('fe80::1%en7',.5,20,interface='en7')
  self.assertEqual(route['state'],'complete');self.assertEqual(len(route['hops']),1)
 def test_ipv6_resolution_keeps_scope_and_rejects_another_selected_interface(self):
  import socket
  p=server.interface_probes
  with patch.object(p.platform,'system',return_value='Darwin'),patch.object(p,'darwin_addresses',return_value=['fe80::2']),patch.object(p.socket,'if_nametoindex',return_value=7):
   self.assertEqual(p.binding_for('en7','fe80::1%7')['index'],7)
   with self.assertRaises(p.InterfaceUnavailable):p.binding_for('en7','fe80::1%en8')
   with patch.object(p.dns_resolver,'getaddrinfo',return_value=[(socket.AF_INET6,1,6,'',('fe80::1',0,0,7))]):self.assertEqual(p.resolve_bound('fe80::1%en7','ipv6','en7'),'fe80::1%7')

class StreamingExport(unittest.TestCase):
 def test_streaming_document_agrees_with_derived_statistics(self):
  with tempfile.TemporaryDirectory() as folder:
   store=server.Store(Path(folder)/'db');config=server.validate_config({'continuous':True,'targets':[{'host':'192.0.2.1'}]});tid=config['targets'][0]['id']
   store.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',('s',0,None,'running',json.dumps(config)))
   for i,v in enumerate((10,170)):store.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',('s',tid,i,'ok',v,'192.0.2.1',''))
   try:
    doc=json.loads(session_export.export_txt(store,'s').split('\n\n',1)[1]);self.assertEqual(doc['statistics'][tid]['jitter'],doc['derived_samples'][-1]['jitter']);self.assertEqual(doc['statistics'][tid]['mean_delta'],160)
   finally:store.db.close()
 def test_atomic_transfer_failure_preserves_existing_file(self):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'report.txt';path.write_text('old')
   def fail(output):output.write(b'partial');raise ValueError('truncated')
   with self.assertRaises(ValueError):export_transfer.atomic_write(path,fail)
   self.assertEqual(path.read_text(encoding='utf-8'),'old');self.assertEqual(len(list(Path(folder).iterdir())),1)
 def test_reader_does_not_hold_writer_mutex(self):
  with tempfile.TemporaryDirectory() as folder:
   store=server.Store(Path(folder)/'db');done=threading.Event()
   with store.reader() as reader:
    reader.execute('SELECT * FROM sessions').fetchall()
    thread=threading.Thread(target=lambda:(store.event('s','','info','test'),done.set()));thread.start();self.assertTrue(done.wait(1));thread.join()
    self.assertEqual(reader.execute('SELECT count(*) FROM events').fetchone()[0],0)
   self.assertEqual(len(store.rows('SELECT * FROM events')),1);store.db.close()

class NativeReadModel(unittest.TestCase):
 def test_slow_route_cadence_does_not_erase_jitter_and_matches_export(self):
  with tempfile.TemporaryDirectory() as folder:
   store,m,tid=self.fixture(folder)
   try:
    store.execute('DELETE FROM samples');store.execute('DELETE FROM hop_samples')
    for i,value in enumerate((10,170,10,170)):
     store.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',('s',tid,970+i*6,'ok',value,'192.0.2.1',''))
     store.execute('INSERT INTO hop_samples VALUES (?,?,?,?,?,?,?,?,?,?)',('s',tid,970+i*6,str(i),1,'one','192.0.2.1','ok',value,''))
    export=json.loads(session_export.export_txt(store,'s').split('\n\n',1)[1]);expected=export['hop_statistics'][tid+'/one']['jitter']
    self.assertGreater(expected,0)
    for period in (30,300,43200):
     view=m.snapshot('s',period,compact=True,selected_node='one')['targets'][0]['hops'][0]
     self.assertEqual(view['last']['jitter'],expected);self.assertEqual(view['stats']['jitter'],expected)
    # The destination's ordinary ping still uses its configured cadence.
    self.assertIsNone(export['statistics'][tid]['jitter'])
   finally:store.db.close()
 def test_event_cursor_does_not_rewind_after_prune_and_engine_restart(self):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'db';first=server.Store(path);first.event('s','','info','old');cursor=first.event_cursor
   first.execute('DELETE FROM events');first.db.close()
   second=server.Store(path,recover=False)
   try:second.event('s','','info','new');self.assertGreater(second.event_cursor,cursor)
   finally:second.db.close()
 def fixture(self,folder):
  store=server.Store(Path(folder)/'db',clock=lambda:1000)
  cfg=server.validate_config({'continuous':True,'interval':.5,'route_interval':.5,'timeout':.5,'targets':[{'host':'192.0.2.1'}]});tid=cfg['targets'][0]['id']
  store.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',('s',0,1000,'completed',json.dumps(cfg)))
  for i,v in enumerate((10,170,12,None,8)):
   status='ok' if v is not None else 'timeout';ts=998+i*.5
   store.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',('s',tid,ts,status,v,'192.0.2.1',''))
   for hop in (1,2):store.execute('INSERT INTO hop_samples VALUES (?,?,?,?,?,?,?,?,?,?)',('s',tid,ts,str(i),hop,str(hop),'192.0.2.'+str(hop),status,v,''))
  mon=server.Monitor(store);mon.hostname=lambda ip:ip
  return store,mon,tid
 def test_compact_view_matches_raw_window_and_only_selected_hop_graph(self):
  with tempfile.TemporaryDirectory() as folder:
   store,m,tid=self.fixture(folder)
   try:
    full=m.snapshot('s',300);compact=m.snapshot('s',300,compact=True,selected_node='2')
    a=full['targets'][0];b=compact['targets'][0]
    for key in ('avg','min','max','loss','jitter','count'):self.assertEqual(a['window_stats'][key],b['window_stats'][key],key)
    self.assertEqual(b['window_last']['latency'],8)
    self.assertEqual([bool(h['graph']) for h in b['hops']],[False,True])
    for x,y in zip(a['hops'],b['hops']):
     for key in ('avg','min','max','loss','count'):self.assertEqual(x['stats'][key],y['stats'][key],key)
   finally:store.db.close()
 def test_event_cursor_survives_deleting_the_last_retained_event(self):
  with tempfile.TemporaryDirectory() as folder:
   store,m,_=self.fixture(folder)
   try:
    store.event('s','','info','first');one=m.snapshot('s',compact=True);cursor=one['events_cursor']
    store.execute('DELETE FROM events');store.event('s','','info','second')
    two=m.snapshot('s',compact=True,event_after=cursor)
    self.assertEqual([e['message'] for e in two['events']],['second']);self.assertGreater(two['events_cursor'],cursor)
   finally:store.db.close()
 def test_export_stream_integrity_and_truncation(self):
  import hashlib
  class Connection:
   def __init__(self):self.frames=[]
   def send_bytes(self,data):self.frames.append(data)
   def recv_bytes(self,limit):
    if not self.frames:raise EOFError()
    value=self.frames.pop(0);assert len(value)<=limit;return value
   def poll(self,seconds):return True
  c=Connection();sender=export_transfer.Sender(c);text='Проверка🙂'*80000;sender.write(text);sender.finish()
  self.assertGreater(len(c.frames),2);out=io.BytesIO();self.assertEqual(export_transfer.receive(c,out),len(text.encode()));self.assertEqual(out.getvalue(),text.encode())
  c.frames=[b'Dpartial']
  with self.assertRaises(EOFError):export_transfer.receive(c,io.BytesIO())
  c.frames=[b'Dpartial',b'E'+json.dumps({'bytes':7,'sha256':hashlib.sha256(b'wrong').hexdigest()}).encode()]
  with self.assertRaises(ValueError):export_transfer.receive(c,io.BytesIO())

class EngineLifecycle(unittest.TestCase):
 def test_late_reverse_dns_cannot_restore_names_after_clear(self):
  with tempfile.TemporaryDirectory() as folder:
   store=server.Store(Path(folder)/'db');m=server.Monitor(store);entered=threading.Event();release=threading.Event();done=threading.Event()
   def lookup(ip):entered.set();release.wait(2);done.set();return 'old.example',[],[]
   try:
    with patch.object(server.socket,'gethostbyaddr',side_effect=lookup):
     m.hostname('192.0.2.1');self.assertTrue(entered.wait(1));m.clear();release.set();self.assertTrue(done.wait(1));time.sleep(.02)
     self.assertEqual(m.ptr_names,{})
   finally:release.set();store.db.close()
 def test_route_follows_ping_ip_with_retention_and_cancels_when_it_changes(self):
  with tempfile.TemporaryDirectory() as folder,patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   store=server.Store(Path(folder)/'db',retention_seconds=server.RETENTION_SECONDS);m=server.Monitor(store)
   cfg=server.validate_config({'continuous':True,'interval':.5,'route_interval':.5,'timeout':.5,'targets':[{'host':'load-balanced.example'}]});target=cfg['targets'][0];sid='s';stop=threading.Event();seen=[]
   m.latest_samples[(sid,target['id'])]=dict(ts=time.time(),ip='192.0.2.10',status='ok',latency=10,detail='')
   def trace(ip,timeout,hops,cancel,save):
    seen.append(ip);m.latest_samples[(sid,target['id'])]=dict(ts=time.time(),ip='192.0.2.11',status='ok',latency=11,detail='')
    self.assertTrue(cancel.is_set());save(dict(ts=time.time(),hop=1,ip='192.0.2.10',status='ok',latency=99,detail=''))
    stop.set();return dict(hops=[],state='partial',destination=ip)
   try:
    with patch.object(server,'trace_cycle',side_effect=trace),patch.object(server,'resolve',side_effect=AssertionError('route independently resolved DNS')):
     m.route_worker(sid,target,cfg,time.time(),stop,False)
    self.assertEqual(seen,['192.0.2.10']);self.assertEqual(store.rows('SELECT * FROM hop_samples'),[])
   finally:store.db.close()
 def test_old_destination_hops_are_hidden_after_ip_change_but_retained_on_disk(self):
  with tempfile.TemporaryDirectory() as folder:
   store,m,tid=NativeReadModel().fixture(folder)
   try:
    m.route_epoch[('s',tid)]=1001
    snap=m.snapshot('s',compact=True);self.assertEqual(snap['targets'][0]['hops'],[])
    self.assertEqual(len(store.rows('SELECT * FROM hop_samples')),10)
   finally:store.db.close()
 def test_idempotent_record_retries_a_lost_reply(self):
  import background_engine
  p=background_engine.Proxy('.',dict(active_id='s'))
  rows=[['s','speed','one',1,{'download':8}]]
  with patch.object(background_engine,'request',side_effect=[EOFError(),1]) as request,patch.object(background_engine.time,'sleep'):
   p.record(rows)
  self.assertEqual(request.call_count,2);self.assertEqual(request.call_args_list[0],request.call_args_list[1])
 def test_unanswered_authentication_does_not_block_other_clients(self):
  import background_engine
  from multiprocessing.connection import Client
  with tempfile.TemporaryDirectory(prefix='lr-') as folder,patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   stop=threading.Event();worker=threading.Thread(target=background_engine.run,args=(folder,stop));worker.start();stalled=None
   try:
    deadline=time.monotonic()+5;proxy=None
    while proxy is None and time.monotonic()<deadline:proxy=background_engine.connect(folder);time.sleep(.01)
    self.assertIsNotNone(proxy)
    where,family=background_engine.address(folder);stalled=Client(where,family=family,authkey=None)
    begin=time.monotonic();self.assertIsNotNone(background_engine.connect(folder));self.assertLess(time.monotonic()-begin,2)
   finally:
    if stalled:stalled.close()
    stop.set();worker.join(6)
   self.assertFalse(worker.is_alive())
 def test_start_waits_for_concurrent_clear_instead_of_losing_new_session(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   store=server.Store(Path(folder)/'db');m=server.Monitor(store);cfg={'continuous':True,'targets':[{'host':'192.0.2.1'}]};m.start(cfg)
   entered=threading.Event();release=threading.Event();done=threading.Event();original=store.clear;created=[]
   def clear():entered.set();release.wait(2);original()
   with patch.object(store,'clear',side_effect=clear):
    clearing=threading.Thread(target=m.clear);clearing.start();self.assertTrue(entered.wait(2))
    starting=threading.Thread(target=lambda:(created.append(m.start(cfg)),done.set()));starting.start()
    try:self.assertFalse(done.wait(.05))
    finally:release.set();clearing.join(3);starting.join(3)
   try:self.assertTrue(done.is_set());self.assertEqual(store.rows('SELECT id FROM sessions'),[{'id':created[0]}])
   finally:m.clear();store.db.close()
 def test_long_profile_uses_short_owner_only_ipc_socket_path(self):
  if os.name=='nt':self.skipTest('Unix socket path')
  import background_engine,stat
  with tempfile.TemporaryDirectory() as folder:
   a,family=background_engine.address(Path(folder)/('x'*120));b,_=background_engine.address(Path(folder)/('y'*120))
   self.assertEqual(family,'AF_UNIX');self.assertLess(len(a.encode()),100);self.assertNotEqual(a,b)
   info=Path(a).parent.lstat();self.assertTrue(stat.S_ISDIR(info.st_mode));self.assertEqual(info.st_mode&0o077,0)

class AsyncSources(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=desktop.QApplication.instance() or desktop.QApplication([])
 def test_graphene_limits_transfer_before_loading_full_body(self):
  class Response:
   closed=False;read=0
   def iter_content(self,size):
    for i in range(100):self.read+=1;yield b'x'*32
   def close(self):self.closed=True
  response=Response()
  with self.assertRaises(graphene_port.SourceError):graphene_port.read_json(response,limit=50)
  self.assertTrue(response.closed);self.assertEqual(response.read,2)
 def test_graphene_manual_selection_can_return_to_latest_lldp(self):
  import port_widgets
  panel=port_widgets.PortPanel()
  try:
   with patch.object(panel,'refresh'):
    panel.manual();panel.lldp({'switch':'switch-new','port':'Gi1/0/2'});self.assertIsNone(panel.connected)
    panel.use_lldp();self.assertTrue(panel.auto);self.assertEqual(panel.connected[:2],('switch-new','Gi1/0/2'))
  finally:panel.shutdown();panel.deleteLater()
 def test_hung_dns_calls_are_shared_and_bounded(self):
  release=threading.Event();calls=[]
  def lookup(*args):calls.append(args);release.wait(2);return []
  try:
   with patch.object(dns_resolver.socket,'getaddrinfo',side_effect=lookup):
    for i in range(10):
     with self.assertRaises(OSError):dns_resolver.getaddrinfo('blocked.example',None,0,1,timeout=.001)
    self.assertEqual(len(calls),1)
  finally:
   release.set()
   for job in list(dns_resolver._jobs.values()):job['done'].wait(2)
 def test_csv_whitespace_and_controls_cannot_start_formulas(self):
  for value in ('=1',' +1','\t=1','\r@SUM(1)','\n-1'):
   self.assertEqual(server.csv_value(value),"'"+value)
  for value in ('normal',1,None):self.assertEqual(server.csv_value(value),value)
 def test_missing_data_dir_is_an_explained_argument_error(self):
  self.assertIsNone(desktop.option_value([],'--data-dir'))
  self.assertEqual(desktop.option_value(['--data-dir','/tmp/test'],'--data-dir'),'/tmp/test')
  for args in (['--data-dir'],['--data-dir','--smoke-test']):
   with self.assertRaisesRegex(ValueError,'Не указано'):desktop.option_value(args,'--data-dir')
 def test_numeric_probe_bypasses_busy_dns_workers(self):
  import socket
  jobs={('blocked'+str(i),0,0,1):{'done':threading.Event()} for i in range(dns_resolver.LIMIT)}
  with patch.object(dns_resolver,'_jobs',jobs):
   with self.assertRaisesRegex(OSError,'DNS занят'):dns_resolver.getaddrinfo('new.example',80,0,1,timeout=.01)
   addresses=dns_resolver.getaddrinfo('127.0.0.1',80,0,1,timeout=.01)
  self.assertEqual(addresses[0][4],('127.0.0.1',80))
 def test_vmix_api_cannot_execute_query_commands_or_follow_redirects(self):
  import stream_sources as source
  from unittest.mock import MagicMock
  with patch.object(source.requests.Session,'get') as get:
   for url in ('http://127.0.0.1:8088/api?Function=StartStreaming','http://127.0.0.1:8088/api#command','http://127.0.0.1:8088/other'):
    with self.assertRaisesRegex(source.SourceError,'без команд'):source.vmix_api(url)
   get.assert_not_called()
  response=MagicMock();response.__enter__.return_value=response;response.status_code=302
  with patch.object(source.requests.Session,'get',return_value=response) as get:
   with self.assertRaisesRegex(source.SourceError,'HTTP 302'):source.vmix_api('http://127.0.0.1:8088/api')
   self.assertFalse(get.call_args.kwargs['allow_redirects']);response.raw.read1.assert_not_called()
 def test_vmix_transport_error_is_sanitized(self):
  import stream_sources as source
  from unittest.mock import MagicMock
  from urllib3.exceptions import ReadTimeoutError
  response=MagicMock();response.__enter__.return_value=response;response.status_code=200
  response.raw.read1.side_effect=ReadTimeoutError(None,None,'private detail')
  with patch.object(source.requests.Session,'get',return_value=response):
   with self.assertRaises(source.SourceError) as caught:source.vmix_api('http://user:secret@127.0.0.1:8088/api')
  self.assertNotIn('secret',str(caught.exception));self.assertNotIn('private detail',str(caught.exception))
 def test_ndi_does_not_fabricate_second_by_second_measurements(self):
  m=stream_monitor.StreamMonitor(clock=lambda:100)
  m.cycle(100,obs=None,vmix_api=None,vmix_rows=None,connections=None,ndi=[{'name':'cam','ip':'192.0.2.1','port':5960}])
  m.cycle(101,obs=None,vmix_api=None,vmix_rows=None,connections=None,ndi=None)
  self.assertEqual(len(m.history['ndi:cam']),1)
  m.cycle(150,obs=None,vmix_api=None,vmix_rows=None,connections=None,ndi=None)
  self.assertEqual(m.streams['ndi:cam']['state'],'lost')
 def test_paused_stream_history_still_expires(self):
  now=[100];m=stream_monitor.StreamMonitor(clock=lambda:now[0],history_seconds=10)
  m.stream('test',100,state='live');m.sample('test',100);m.last_poll=100
  now[0]=120;snap=m.snapshot(key='test');self.assertEqual(snap['history'],{});self.assertEqual(snap['streams'],[])
 def test_http_selected_adapter_binds_before_connect_and_never_falls_back(self):
  import bound_http,socket
  from unittest.mock import MagicMock
  rows=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('192.0.2.1',443))]
  sock=MagicMock()
  with patch.object(bound_http.dns_resolver,'getaddrinfo',return_value=rows),patch.object(bound_http.interface_probes,'binding_for',return_value={'id':'en7','index':7,'source':'192.0.2.10'}),patch.object(bound_http.platform,'system',return_value='Darwin'),patch.object(bound_http.socket,'socket',return_value=sock):
   self.assertIs(bound_http.connect('example.org',443,'en7',2),sock)
   sock.setsockopt.assert_called_once_with(socket.IPPROTO_IP,25,7);sock.bind.assert_called_once_with(('192.0.2.10',0))
   names=[c[0] for c in sock.mock_calls];self.assertLess(names.index('bind'),names.index('connect'))
  with patch.object(bound_http.dns_resolver,'getaddrinfo',return_value=rows),patch.object(bound_http.interface_probes,'binding_for',side_effect=OSError('unplugged')),patch.object(bound_http.socket,'socket') as create:
   with self.assertRaisesRegex(OSError,'unplugged'):bound_http.connect('example.org',443,'en7',2)
   create.assert_not_called()
 def test_http_binding_does_not_change_global_connection_pool_classes(self):
  import bound_http,urllib3
  original=urllib3.poolmanager.pool_classes_by_scheme['https'];a=bound_http.adapter('en7');b=bound_http.adapter('en8')
  try:
   self.assertIs(urllib3.poolmanager.pool_classes_by_scheme['https'],original)
   self.assertIsNot(a.poolmanager.pool_classes_by_scheme['https'],b.poolmanager.pool_classes_by_scheme['https'])
  finally:a.close();b.close()
 def test_stream_errors_and_statistics_do_not_retain_url_secrets(self):
  import stream_sources as source
  address='http://login:very-private-password@192.0.2.1:8088/api'
  with patch.object(source.requests.Session,'get',side_effect=OSError('unreachable')):
   with self.assertRaises(source.SourceError) as caught:source.vmix_api(address)
  self.assertEqual(str(caught.exception),'vMix API не отвечает: http://192.0.2.1:8088')
  raw=[['SRT Cam','0','0','0','0','Connected srt://login:very-private-password@192.0.2.1:9000?passphrase=hidden-token','']]
  rows=source.parse_vmix_rows([],raw);m=stream_monitor.StreamMonitor();m.update_vmix_rows(time.time(),rows)
  text=json.dumps(rows)+m.report()
  for secret in ('login','very-private-password','hidden-token','passphrase='):self.assertNotIn(secret,text)
  self.assertEqual(source.safe_url('rtmp://192.0.2.1/ONLY_SECRET_KEY'),'rtmp://192.0.2.1')

class QtBackgroundWork(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=desktop.QApplication.instance() or desktop.QApplication([])
 def test_slow_command_does_not_block_qt_and_close_waits_without_destroying_thread(self):
  release=threading.Event();entered=threading.Event();ticks=[]
  import ui_tasks
  from PySide6.QtCore import QTimer
  queue=ui_tasks.IOQueue();queue.start();timer=QTimer();timer.timeout.connect(lambda:ticks.append(time.monotonic()));timer.start(5)
  queue.submit(lambda:(entered.set(),release.wait(2)))
  try:
   self.assertTrue(entered.wait(1));deadline=time.monotonic()+.06
   while time.monotonic()<deadline:self.app.processEvents();time.sleep(.001)
   queue.shutdown();self.assertTrue(queue.isRunning());self.assertGreater(len(ticks),3)
  finally:release.set();queue.wait(2000);timer.stop();self.assertFalse(queue.isRunning())
 def test_log_selection_survives_retention_of_old_prefix(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'):
   w=desktop.Window(Path(folder));w.timer.stop()
   try:
    events=[dict(ts=i,message='event '+str(i)) for i in range(100)];w.update_logs(list(reversed(events)))
    cursor=w.logs.textCursor();cursor.setPosition(w.logs.toPlainText().index('event 50'));cursor.movePosition(cursor.MoveOperation.Right,cursor.MoveMode.KeepAnchor,8);w.logs.setTextCursor(cursor)
    self.assertEqual(w.logs.textCursor().selectedText(),'event 50');w.update_logs(list(reversed(events[5:]+[dict(ts=101,message='new')])))
    self.assertEqual(w.logs.textCursor().selectedText(),'event 50');self.assertNotIn('event 0\n',w.logs.toPlainText())
   finally:close_window(w);w.store.db.close();w.deleteLater()
 def test_old_snapshot_cannot_undo_a_newer_pause_or_period_choice(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'):
   w=desktop.Window(Path(folder));w.timer.stop()
   try:
    w.paused=True;w.state_revision=4
    w.render({'_ui_revision':3,'paused':False,'targets':[],'active_id':None})
    self.assertTrue(w.paused);self.assertEqual(w.snapshot,{})
    w.render({'_ui_revision':4,'_ui_period':43200,'paused':False,'targets':[],'active_id':None})
    self.assertTrue(w.paused);self.assertEqual(w.snapshot,{})
   finally:close_window(w);w.store.db.close();w.deleteLater()
 def test_preferences_restore_absent_adapter_without_switching_to_auto(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'):
   Path(folder,'ui-preferences.json').write_text(json.dumps(dict(interface='en999',interval=2,period=3600,size=[940,680])))
   w=desktop.Window(Path(folder));w.timer.stop()
   try:
    self.assertEqual(w.selected_interface,'en999');self.assertEqual(w.nic.currentData(),'en999');self.assertEqual(w.discovery.selected,'en999')
    self.assertEqual(w.interval.currentData(),2);self.assertEqual(w.period.currentData(),3600)
   finally:close_window(w);w.store.db.close();w.deleteLater()
 def test_graphene_retries_unacknowledged_rows_after_a_write_failure(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'):
   w=desktop.Window(Path(folder));w.timer.stop();w.sid='s'
   data=dict(host='switch',port='Gi1/0/1',series={key:[(time.time(),1)] for key in graphene_port.SENSORS})
   batches=[];ok=[False]
   def record(kind,key,value,rows=None,done=None):
    if done:batches.append(rows);done(ok[0])
    return ok[0]
   try:
    with patch.object(w,'record',side_effect=record):
     w.record_port(data);self.assertEqual(w.port_seen,{});self.assertEqual(w.port_pending,{})
     ok[0]=True;w.record_port(data);self.assertEqual(len(w.port_seen),9);self.assertEqual(batches[0],batches[1])
   finally:close_window(w);w.store.db.close();w.deleteLater()
 def test_failed_data_warning_stays_in_an_otherwise_valid_export(self):
  with tempfile.TemporaryDirectory() as folder:
   store,m,_=NativeReadModel().fixture(folder);path=Path(folder)/'report.txt'
   try:
    worker=desktop.ExportWorker(store,None,'s',path,{},warning=lambda:'disk failed')
    worker.run();text=path.read_text(encoding='utf-8');self.assertTrue(text.startswith('ВНИМАНИЕ:'))
    doc=json.loads(text.split('\n\n',1)[1]);self.assertEqual(len(doc['samples']),5)
   finally:store.db.close()
 def test_failed_interval_change_restores_the_actual_interval(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   w=desktop.Window(Path(folder));w.timer.stop();w.address.setText('192.0.2.1');w.toggle();old=w.interval.currentData()
   try:
    with patch.object(w.monitor,'set_interval',side_effect=OSError('failed')),patch.object(desktop.QMessageBox,'warning'):
     w.interval.setCurrentIndex(2)
    self.assertEqual(w.interval.currentData(),old);self.assertEqual(w.timer.interval(),int(old*1000))
   finally:close_window(w);w.store.db.close();w.deleteLater()

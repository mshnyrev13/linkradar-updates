"""Adapter selection, source binding, route identity and in-flight cancellation."""
from qt_support import close_window,flush_io
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import ctypes as C
import errno
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch,MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import interface_probes as probes,server,route_engine,desktop,background_engine
from PySide6.QtCore import QCoreApplication,QEvent

BIND={'id':'en7','index':7,'source':'192.0.2.10'}

class BoundProbes(unittest.TestCase):
 def test_disconnected_adapter_never_falls_back_to_cli(self):
  with patch.object(probes,'binding_for',side_effect=probes.InterfaceUnavailable('Адаптер отключён')),patch.object(server.subprocess,'run') as run:
   reply=server.probe({'host':'192.0.2.1','mode':'icmp'},.5,'192.0.2.1',interface='en7')
  self.assertEqual(reply['status'],'error');self.assertIsNone(reply['latency']);run.assert_not_called()
 def test_auto_resolves_family_available_on_selected_adapter(self):
  rows=[(socket.AF_INET6,1,6,'',('2001:db8::1',0,0,0)),(socket.AF_INET,1,6,'',('192.0.2.1',0))]
  def binding(identity,ip):
   if ':' in ip:raise probes.InterfaceUnavailable('Нет IPv6')
   return BIND
  with patch.object(socket,'getaddrinfo',return_value=rows),patch.object(probes,'binding_for',side_effect=binding):
   self.assertEqual(probes.resolve_bound('example.com','auto','en7'),'192.0.2.1')
 def test_bound_ping_retains_measured_rtt(self):
  with patch.object(probes,'binding_for',return_value=BIND),patch.object(probes,'echo',return_value={'kind':'echo','ip':'192.0.2.1','latency':18.25}) as echo,patch.object(server.subprocess,'run') as run:
   answer=server.probe({'host':'192.0.2.1','mode':'icmp'},.5,'192.0.2.1',interface='en7')
  self.assertEqual(answer['latency'],18.25);self.assertIn('en7 (192.0.2.10)',answer['detail']);self.assertEqual(echo.call_args.args[1],BIND);run.assert_not_called()
 def test_route_uses_same_binding_and_exact_ttl_rtts(self):
  answers=[{'kind':'ttl','ip':'192.0.2.254','latency':2.5},{'kind':'timeout','ip':None,'latency':None},{'kind':'echo','ip':'192.0.2.1','latency':18.25}]
  with patch.object(probes,'binding_for',return_value=BIND),patch.object(probes,'echo',side_effect=answers) as echo,patch.object(route_engine.subprocess,'Popen') as popen:
   result=route_engine.trace_cycle('192.0.2.1',.5,20,interface='en7')
  self.assertEqual(result['state'],'complete');self.assertEqual([h['latency'] for h in result['hops']],[2.5,None,18.25]);self.assertEqual([c.args[3] for c in echo.call_args_list],[1,2,3]);popen.assert_not_called()
 def test_unplug_during_route_checks_adapter_at_next_hop(self):
  with patch.object(probes,'binding_for',side_effect=[BIND,BIND,probes.InterfaceUnavailable('Отключён')]),patch.object(probes,'echo',return_value={'kind':'ttl','ip':'192.0.2.254','latency':2}) as echo:
   with self.assertRaises(probes.InterfaceUnavailable):route_engine.trace_cycle('192.0.2.1',.5,20,interface='en7')
  self.assertEqual(echo.call_count,1)
 def test_darwin_socket_bound_before_send_and_uses_reply_rtt(self):
  sock=MagicMock();sock.__enter__.return_value=sock
  ident,seq=11,12;reply=struct.pack('!BBHHH',0,0,0,ident,seq)
  sock.recvfrom.return_value=(reply,('192.0.2.1',0))
  with patch.object(probes,'darwin_network_access') as access,patch.object(socket,'socket',return_value=sock),patch.object(probes.secrets,'randbelow',side_effect=[ident,seq]),patch.object(probes.time,'perf_counter',side_effect=[10,10.001,10.001,10.018]):
   result=probes.darwin_echo('192.0.2.1',BIND,.5)
  access.assert_called_once_with('192.0.2.1',BIND,.5,None)
  self.assertEqual(result['latency'],18);sock.setsockopt.assert_any_call(socket.IPPROTO_IP,25,7);sock.bind.assert_called_once_with(('192.0.2.10',0))
  names=[c[0] for c in sock.mock_calls];self.assertLess(names.index('setsockopt'),names.index('sendto'))
 def test_darwin_permission_failure_sends_no_icmp_and_recovers_on_retry(self):
  access=MagicMock();access.__enter__.return_value=access
  access.connect.side_effect=[OSError(errno.EHOSTUNREACH,'No route to host'),None]
  icmp=MagicMock();icmp.__enter__.return_value=icmp
  icmp.recvfrom.return_value=(struct.pack('!BBHHH',0,0,0,11,12),('192.0.2.1',0))
  with patch.object(socket,'socket',side_effect=[access,access,icmp]) as create,patch.object(probes.secrets,'randbelow',side_effect=[11,12]):
   with self.assertRaisesRegex(probes.InterfaceUnavailable,'en7.*Локальная сеть'):
    probes.darwin_echo('192.0.2.1',BIND,.5)
   self.assertEqual(create.call_count,1);icmp.sendto.assert_not_called()
   with patch.object(probes.time,'perf_counter',side_effect=[10,10.001,10.001,10.018]):
    result=probes.darwin_echo('192.0.2.1',BIND,.5)
  self.assertEqual(result['latency'],18)
  access.setsockopt.assert_any_call(socket.IPPROTO_IP,25,7)
  access.bind.assert_called_with(('192.0.2.10',0))
  access.connect.assert_called_with(('192.0.2.1',9))
  access.send.assert_not_called();access.sendto.assert_not_called()
 def test_darwin_access_check_keeps_ipv6_scope_and_sends_no_datagram(self):
  access=MagicMock();access.__enter__.return_value=access
  with patch.object(socket,'socket',return_value=access):
   probes.darwin_network_access('fe80::1%en7',{**BIND,'source':'fe80::2'},.5)
  access.setsockopt.assert_called_once_with(socket.IPPROTO_IPV6,125,7)
  access.bind.assert_called_once_with(('fe80::2',0,0,7))
  access.connect.assert_called_once_with(('fe80::1',9,0,7))
  access.send.assert_not_called();access.sendto.assert_not_called()
 def test_cancelled_darwin_probe_does_not_request_network_access(self):
  stop=threading.Event();stop.set()
  with patch.object(socket,'socket') as create:
   self.assertEqual(probes.darwin_echo('192.0.2.1',BIND,.5,stop=stop)['kind'],'cancelled')
  create.assert_not_called()
 def test_macos_helper_denial_never_falls_back_to_socket_or_cli_ping(self):
  child=MagicMock();child.__enter__.return_value=child
  child.poll.return_value=3;child.returncode=3;child.stdout.read.return_value='local_network_denied\n'
  with patch.object(probes,'darwin_access_helper',return_value=Path('/test/LinkRadarNetworkAccess')),patch.object(probes.subprocess,'Popen',return_value=child),patch.object(socket,'socket') as create,patch.object(probes.subprocess,'run') as run:
   with self.assertRaisesRegex(probes.InterfaceUnavailable,'macOS не разрешила'):
    probes.darwin_echo('192.0.2.1',BIND,.5)
  create.assert_not_called();run.assert_not_called()
 def test_macos_helper_cache_separates_adapters_and_source_addresses(self):
  child=MagicMock();child.__enter__.return_value=child
  child.poll.return_value=0;child.returncode=0;child.stdout.read.return_value='ready\n'
  with patch.object(probes,'_access_ready',{}),patch.object(probes,'darwin_access_helper',return_value=Path('/test/LinkRadarNetworkAccess')),patch.object(probes.subprocess,'Popen',return_value=child) as popen:
   probes.darwin_network_access('192.0.2.1',BIND,.5)
   probes.darwin_network_access('192.0.2.1',BIND,.5)
   probes.darwin_network_access('192.0.2.1',{**BIND,'id':'en8','index':8},.5)
   probes.darwin_network_access('192.0.2.1',{**BIND,'source':'192.0.2.11'},.5)
  self.assertEqual(popen.call_count,3)
  self.assertEqual(popen.call_args_list[1].args[0][-1],'8')
 def test_pause_cancels_pending_macos_access_without_sending_icmp(self):
  child=MagicMock();child.__enter__.return_value=child
  child.poll.side_effect=[None,0]
  stop=MagicMock();stop.is_set.side_effect=[False,False,True]
  with patch.object(probes,'_access_ready',{}),patch.object(probes,'darwin_access_helper',return_value=Path('/test/LinkRadarNetworkAccess')),patch.object(probes.subprocess,'Popen',return_value=child),patch.object(socket,'socket') as create:
   self.assertEqual(probes.darwin_echo('192.0.2.1',BIND,.5,stop=stop)['kind'],'cancelled')
  create.assert_not_called()
 def test_reply_matching_rejects_foreign_ttl_and_malformed_packets(self):
  ident,seq=3,4;inner=bytearray(20);inner[0]=0x45;inner[9]=1;inner[16:20]=socket.inet_aton('192.0.2.1')
  quote=bytes(inner)+struct.pack('!BBHHH',8,0,0,ident,seq)
  ttl=struct.pack('!BBHI',11,0,0,0)+quote
  self.assertEqual(probes.match_reply(ttl,'192.0.2.1',ident,seq),'ttl')
  self.assertIsNone(probes.match_reply(ttl,'192.0.2.1',ident,seq+1))
  self.assertIsNone(probes.match_reply(ttl,'192.0.2.2',ident,seq))
  for n in range(len(ttl)):self.assertIsNone(probes.match_reply(ttl[:n],'192.0.2.1',ident,seq))
 def test_ipv6_ttl_and_echo_matching(self):
  ip='2001:db8::1';header=bytearray(40);header[0]=0x60;header[6]=58;header[24:40]=socket.inet_pton(socket.AF_INET6,ip)
  ttl=struct.pack('!BBHI',3,0,0,0)+header+struct.pack('!BBHHH',128,0,0,7,8)
  self.assertEqual(probes.match_reply(ttl,ip,7,8),'ttl');self.assertIsNone(probes.match_reply(ttl,ip,7,9))
  self.assertEqual(probes.match_reply(struct.pack('!BBHHH',129,0,0,7,8),ip,7,8),'echo')
 def test_jitter_does_not_bridge_adapter_switch(self):
  a={'ts':1,'status':'ok','ip':'192.0.2.1','latency':18,'detail':probes.detail(BIND)}
  b={**a,'ts':2,'latency':90,'detail':probes.detail({**BIND,'id':'en0','source':'192.0.2.11'})}
  self.assertIsNone(server.smooth_jitter([a,b],1)[1]['jitter']);self.assertIsNone(server.metrics([a,b])['jitter'])
 def test_windows_ipv4_api_receives_selected_source_and_ttl(self):
  api=MagicMock();api.IcmpCreateFile.return_value=123
  def send(*args):
   row=C.cast(args[9],C.POINTER(probes.EchoReply)).contents;row.status=11013;row.address=int.from_bytes(socket.inet_aton('192.0.2.254'),'little');row.rtt=3;return 1
  api.IcmpSendEcho2Ex.side_effect=send
  with patch.object(C,'WinDLL',return_value=api,create=True):result=probes.windows_echo('192.0.2.1',BIND,.5,ttl=1)
  args=api.IcmpSendEcho2Ex.call_args.args
  self.assertEqual(args[4],int.from_bytes(socket.inet_aton(BIND['source']),'little'));self.assertEqual(C.cast(args[8],C.POINTER(probes.IpOptions)).contents.ttl,1)
  self.assertEqual(result,{'kind':'ttl','ip':'192.0.2.254','latency':3});api.IcmpCloseHandle.assert_called_once_with(123)

class AdapterSwitch(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=desktop.QApplication.instance() or desktop.QApplication([])
 def test_gui_selection_updates_active_engine_and_preserves_pause(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   w=desktop.Window(Path(folder));w.timer.stop()
   try:
    w.address.setText('192.0.2.1');w.toggle();sid=w.sid;w.toggle();self.assertTrue(w.monitor.paused)
    w.nic.addItem('USB','en7');w.nic.setCurrentIndex(w.nic.findData('en7'))
    self.assertEqual(w.monitor.sid,sid);self.assertTrue(w.monitor.paused);self.assertEqual(w.monitor.config['interface'],'en7')
    self.assertEqual(w.session_config['interface'],'en7');self.assertTrue(w.monitor.probe_cancel.is_set())
    w.nic.setCurrentIndex(0);self.assertIsNone(w.monitor.config['interface'])
   finally:close_window(w);w.store.db.close();w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
 def test_switch_discards_old_inflight_probe(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   store=server.Store(Path(folder)/'network.sqlite3');monitor=server.Monitor(store);sid=monitor.start({'continuous':True,'interface':'en0','interval':.5,'timeout':.5,'targets':[{'host':'192.0.2.1'}]})
   entered=threading.Event();released=threading.Event();new=threading.Event()
   def fake(target,timeout,ip,interface=None,stop=None):
    if interface=='en0':entered.set();released.wait(2)
    else:new.set()
    return {'ts':time.time(),'status':'ok','latency':18,'ip':'192.0.2.1','detail':interface}
   worker=threading.Thread(target=monitor.worker,args=(sid,monitor.config['targets'][0],monitor.config,time.time(),monitor.stop_event))
   with patch.object(server,'probe',side_effect=fake):
    worker.start()
    try:
     self.assertTrue(entered.wait(2));monitor.set_interface('en7');released.set();self.assertTrue(new.wait(2));time.sleep(.03)
     rows=store.rows('SELECT detail FROM samples');self.assertTrue(rows);self.assertTrue(all(r['detail']=='en7' for r in rows))
    finally:monitor.stop();released.set();worker.join(3);monitor.clear();store.db.close()

 def test_ipc_changes_binding_without_creating_new_session(self):
  with tempfile.TemporaryDirectory(prefix='nw-bind-') as folder,patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   stop=threading.Event();worker=threading.Thread(target=background_engine.run,args=(folder,stop));worker.start()
   try:
    deadline=time.monotonic()+3;proxy=None
    while time.monotonic()<deadline and proxy is None:
     proxy=background_engine.connect(folder);time.sleep(.01)
    self.assertIsNotNone(proxy);self.assertIn('bound_interface',proxy.status['capabilities'])
    sid=proxy.start({'continuous':True,'targets':[{'host':'192.0.2.1'}]});proxy.pause(sid);proxy.set_interface('7')
    current=background_engine.connect(folder);self.assertEqual(current.sid,sid);self.assertTrue(current.status['paused']);self.assertEqual(current.status['config']['interface'],'7')
    proxy.clear()
   finally:
    stop.set();worker.join(5);self.assertFalse(worker.is_alive())
 def test_old_route_reply_is_discarded_on_interface_switch(self):
  with tempfile.TemporaryDirectory() as folder,patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
   store=server.Store(Path(folder)/'network.sqlite3');monitor=server.Monitor(store);sid=monitor.start({'continuous':True,'interface':'en0','interval':.5,'timeout':.5,'targets':[{'host':'192.0.2.1'}]})
   entered=threading.Event();released=threading.Event();new=threading.Event()
   def trace(ip,timeout,max_hops,cancel,save,interface=None):
    if interface=='en0':entered.set();released.wait(2)
    else:new.set()
    hop={'ts':time.time(),'hop':1,'ip':ip,'status':'ok','latency':18,'detail':interface}
    save(hop);return {'hops':[hop],'destination':ip,'state':'complete'}
   worker=threading.Thread(target=monitor.route_worker,args=(sid,monitor.config['targets'][0],monitor.config,time.time(),monitor.stop_event,False))
   with patch.object(server,'trace_cycle',side_effect=trace),patch.object(probes,'resolve_bound',return_value='192.0.2.1'):
    worker.start()
    try:
     self.assertTrue(entered.wait(2));monitor.set_interface('en7');released.set();self.assertTrue(new.wait(2));time.sleep(.03)
     rows=store.rows('SELECT detail FROM hop_samples');self.assertTrue(rows);self.assertTrue(all(r['detail']=='en7' for r in rows))
    finally:monitor.stop();released.set();worker.join(3);monitor.clear();store.db.close()

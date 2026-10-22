"""Route identity, time focus, cancellation and offline Wireshark fixtures."""
import json
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from route_engine import parse_hop, route_command, trace_cycle
from packet_tools import PacketTools, tool


def fixture_pcap():
    """Two harmless Ethernet/IPv4/UDP frames with documentation-only IPs."""
    eth = b'\x00\x11\x22\x33\x44\x55\x66\x77\x88\x99\xaa\xbb\x08\x00'
    udp = struct.pack('!HHHH', 5000, 5001, 8, 0)
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 28, 1, 0, 64, 17, 0,
                     socket.inet_aton('192.0.2.1'), socket.inet_aton('192.0.2.2'))
    frame = eth + ip + udp
    content = struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)
    for second in (1700000000, 1700000001):
        content += struct.pack('<IIII', second, 0, len(frame), len(frame)) + frame
    return content


def fixture_tcp_pcap():
    content=struct.pack('<IHHIIII',0xa1b2c3d4,2,4,0,0,65535,1)
    exchanges=[(False,100,0,2,b''),(True,200,101,18,b''),(False,101,201,16,b''),
               (False,101,201,24,b'test'),(False,101,201,24,b'test'),(True,201,105,20,b'')]
    for i,(reverse,seq,ack,flags,payload) in enumerate(exchanges):
        src,dst=('192.0.2.2','192.0.2.1') if reverse else ('192.0.2.1','192.0.2.2')
        ports=(443,5000) if reverse else (5000,443)
        tcp=struct.pack('!HHIIBBHHH',ports[0],ports[1],seq,ack,0x50,flags,65535,0,0)+payload
        ip=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(tcp),i,0,64,6,0,socket.inet_aton(src),socket.inet_aton(dst))
        frame=b'\x00\x11\x22\x33\x44\x55\x66\x77\x88\x99\xaa\xbb\x08\x00'+ip+tcp
        content+=struct.pack('<IIII',1700000000,i*100000,len(frame),len(frame))+frame
    return content


class Routes(unittest.TestCase):
    def test_tcp_refusal_and_timeout_remain_distinct(self):
        target={'host':'127.0.0.1','mode':'tcp','port':12345}
        for error,status in ((ConnectionRefusedError(),'refused'),(socket.timeout(),'timeout')):
            with self.subTest(status=status),patch('server.socket.create_connection',side_effect=error):
                result=server.probe(target,.5,ip='127.0.0.1')
                self.assertEqual(result['status'],status)
                self.assertIsNone(result['latency'])

    def test_ipv6_and_windows_timeout_not_zero(self):
        self.assertEqual(parse_hop(' 7 2a02:6b8::2:242 8.651 ms')['latency'], 8.651)
        h = parse_hop(' 2 * * * Request timed out.')
        self.assertEqual(h['status'], 'timeout'); self.assertIsNone(h['latency'])
        self.assertEqual(parse_hop(' 1 <1 мс 2 мс 3 мс 192.0.2.1')['latency'], 2)
        self.assertIn('-6', route_command('::1', 1, 12, 'Windows'))
        with patch('route_engine.shutil.which', return_value='/usr/sbin/traceroute6'):
            self.assertIn('-I', route_command('::1', 1, 12, 'Darwin'))

    def test_cancel_terminates_slow_process(self):
        stop=threading.Event(); threading.Timer(.15,stop.set).start()
        with patch('route_engine.route_command', return_value=[sys.executable,'-c','import time; time.sleep(10)']):
            started=time.monotonic(); result=trace_cycle('127.0.0.1',1,12,stop)
        self.assertLess(time.monotonic()-started,2)
        self.assertEqual(result['hops'], [])

    def test_history_focus_separates_changed_nodes(self):
        with tempfile.TemporaryDirectory() as root:
            store=server.Store(Path(root)/'db');monitor=server.Monitor(store)
            config=server.validate_config({'targets':[{'host':'192.0.2.2','mode':'route'}]});tid=config['targets'][0]['id']
            store.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',('s',100,200,'stopped',json.dumps(config)))
            for ts, address in ((110,'192.0.2.1'),(120,'192.0.2.1'),(160,'192.0.2.9')):
                store.execute('INSERT INTO hop_samples VALUES (?,?,?,?,?,?,?,?,?,?)',('s',tid,ts,'cycle',1,'1:'+address,address,'ok',10,'') )
            early=monitor.snapshot('s',60,130)['targets'][0]['hops'][0]
            late=monitor.snapshot('s',100,200)['targets'][0]['hops'][0]
            self.assertEqual(early['ip'],'192.0.2.1');self.assertEqual(early['stats']['count'],2)
            self.assertEqual(late['ip'],'192.0.2.9');self.assertEqual(late['stats']['count'],1)
            store.execute('INSERT INTO routes VALUES (?,?,?,?,?)',('s',tid,210,'complete',json.dumps({'hops':[], 'destination':'192.0.2.2'})))
            self.assertIsNotNone(monitor.snapshot('s',100)['targets'][0]['current_route'])
            self.assertIsNone(monitor.snapshot('s',100,200)['targets'][0]['current_route'])
            store.db.close()

    def test_partial_route_is_error_not_destination_loss(self):
        with tempfile.TemporaryDirectory() as root:
            store=server.Store(Path(root)/'db');monitor=server.Monitor(store)
            config=server.validate_config({'targets':[{'host':'192.0.2.2','mode':'route'}]});t=config['targets'][0];stop=threading.Event()
            monitor.accumulators[('s',t['id'])]=server.Accumulator()
            def fake(*args):
                threading.Timer(.05,stop.set).start()
                return {'hops':[], 'state':'partial'}
            with patch('server.trace_cycle',side_effect=fake),patch('server.resolve',return_value='192.0.2.2'):
                monitor.route_worker('s',t,config,time.time(),stop)
            rows=store.rows('SELECT * FROM samples');self.assertEqual(rows[0]['status'],'error')
            self.assertIsNone(server.metrics(rows)['loss']);store.db.close()

    def test_pinned_address_family(self):
        with self.assertRaises(ValueError):
            server.validate_config({'targets':[{'host':'example.org','family':'ipv4','address':'::1'}]})

    def test_icmp_collects_hops_without_double_counting_destination(self):
        with tempfile.TemporaryDirectory() as root:
            store=server.Store(Path(root)/'db');m=server.Monitor(store);cycles=[]
            def fake_trace(ip,timeout,max_hops,stop,on_hop):
                cycles.append(1);ts=time.time()
                hops=[{'hop':1,'ip':'192.0.2.1','status':'ok','latency':float(len(cycles)),'ts':ts,'detail':''},
                      {'hop':2,'ip':ip,'status':'ok','latency':3.,'ts':ts,'detail':''}]
                for h in hops:on_hop(h)
                return {'hops':hops,'state':'complete','destination':ip}
            def fake_probe(target,timeout,ip=None):
                return {'ts':time.time(),'ip':'192.0.2.2','status':'ok','latency':12.,'detail':''}
            try:
                with patch('server.trace_cycle',side_effect=fake_trace),patch('server.probe',side_effect=fake_probe),patch('server.resolve',return_value='192.0.2.2'),patch('server.socket.gethostbyaddr',return_value=('router.example',[],[])):
                    sid=m.start({'interval':.5,'timeout':.5,'route_interval':.5,'targets':[{'host':'192.0.2.2','mode':'icmp'}]})
                    try:
                        deadline=time.monotonic()+5
                        while time.monotonic()<deadline:
                            # A trace can start before its callback has persisted the hops.
                            if (len(store.rows('SELECT * FROM hop_samples WHERE hop=2'))>=3
                                    and len(store.rows('SELECT * FROM samples'))>=3):break
                            time.sleep(.01)
                        # Compare one committed prefix. Measuring threads must not add
                        # another ping between the snapshot and the database assertion.
                        with m.lock:
                            t=m.snapshot(sid)['targets'][0]
                            hop_count=len(store.rows('SELECT * FROM hop_samples WHERE hop=2'))
                            ping_count=len(store.rows('SELECT * FROM samples'))
                        self.assertGreaterEqual(hop_count,3)
                        self.assertEqual(t['stats']['avg'],12.)
                        self.assertGreaterEqual(t['hops'][0]['stats']['count'],3)
                        self.assertGreater(t['hops'][0]['stats']['max'],t['hops'][0]['stats']['min'])
                        final=t['hops'][-1]
                        self.assertEqual(final['source'],'route')
                        self.assertEqual(final['stats']['count'],hop_count)
                        self.assertEqual(final['stats']['avg'],3.)
                        self.assertEqual(t['stats']['count'],ping_count)
                    finally:
                        m.stop();m.timer_thread.join(5)
                        self.assertFalse(m.timer_thread.is_alive())
            finally:
                store.db.close()

    def test_explicit_resume_keeps_id_deadline_and_measurements(self):
        with tempfile.TemporaryDirectory() as root:
            store=server.Store(Path(root)/'db');m=server.Monitor(store)
            config=server.validate_config({'duration_minutes':1,'targets':[{'host':'127.0.0.1','mode':'tcp'}]})
            started=time.time()-10;t=config['targets'][0]
            store.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',('s',started,started+9,'stopped',json.dumps(config)))
            for i,v in ((1,10),(2,20)):
                store.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',('s',t['id'],started+i,'ok',v,'127.0.0.1',''))
            with patch.object(m,'session_workers',return_value=[]):m.resume('s')
            snap=m.snapshot('s')
            self.assertEqual(snap['session']['id'],'s');self.assertEqual(snap['session']['started'],started)
            self.assertEqual(snap['targets'][0]['stats']['count'],2)
            self.assertEqual(snap['targets'][0]['stats']['avg'],15.)
            self.assertLessEqual(snap['session']['remaining'],50)
            m.stop()
            deadline=time.time()+2
            while m.sid and time.time()<deadline:time.sleep(.02)
            store.db.close()


class Packets(unittest.TestCase):
    def test_bad_capture_header_and_path_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            p=PacketTools(root)
            with self.assertRaises(ValueError):p.upload(b'x'*24)
            with self.assertRaises(ValueError):p.path('../../etc/passwd')
            with self.assertRaises(ValueError):p.capture(1,'not-an-ip',5)

    @unittest.skipUnless(tool('tshark'), 'TShark not installed')
    def test_real_tshark_offline_filter(self):
        with tempfile.TemporaryDirectory() as root:
            p=PacketTools(root);identifier=p.upload(fixture_pcap())
            result=p.analyze(identifier,'udp')
            self.assertEqual(result['packets'],2);self.assertEqual(result['bytes'],84)
            self.assertEqual(result['duration'],1);self.assertEqual(result['tcp_retransmissions'],0)
            self.assertEqual(result['frames'][0]['src'],'192.0.2.1')
            self.assertEqual(p.analyze(identifier,'tcp')['packets'],0)
            with self.assertRaises(ValueError):p.analyze(identifier,'this is invalid filter')

    @unittest.skipUnless(tool('tshark'), 'TShark not installed')
    def test_tcp_signals_from_real_dissector(self):
        with tempfile.TemporaryDirectory() as root:
            p=PacketTools(root);result=p.analyze(p.upload(fixture_tcp_pcap()))
            self.assertEqual(result['packets'],6)
            self.assertEqual(result['tcp_resets'],1)
            self.assertGreaterEqual(result['tcp_retransmissions'],1)
            self.assertGreater(result['tcp_ack_rtt_avg_ms'],0)

if __name__=='__main__':unittest.main()

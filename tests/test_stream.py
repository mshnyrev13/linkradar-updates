"""Stream tab: source parsers, obs-websocket client, aggregation, report and widgets.

Synthetic fixtures only; real OBS/vMix are not required.
"""
from qt_support import close_window,flush_io,flush_report
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import stream_sources as sources
import stream_monitor

def frame(payload,opcode=1):
    payload=payload.encode() if isinstance(payload,str) else payload;size=len(payload)
    return bytes([0x80|opcode])+(bytes([size]) if size<126 else bytes([126])+struct.pack('!H',size))+payload

def read_frame(conn):
    def exact(n):
        data=b''
        while len(data)<n:
            chunk=conn.recv(n-len(data))
            if not chunk:raise EOFError
            data+=chunk
        return data
    first,second=exact(2);size=second&127
    if size==126:size=struct.unpack('!H',exact(2))[0]
    mask=exact(4);payload=exact(size)
    return bytes(b^mask[i%4] for i,b in enumerate(payload)).decode()

class FakeObs:
    """obs-websocket 5 server subset: handshake, authentication and fixed responses."""
    def __init__(self,password,responses):
        self.password=password;self.responses=responses;self.requests=[]
        self.server=socket.socket();self.server.bind(('127.0.0.1',0));self.server.listen();self.port=self.server.getsockname()[1]
        self.thread=threading.Thread(target=self.serve,daemon=True);self.thread.start()
    def serve(self):
        try:conn,_=self.server.accept()
        except OSError:return
        with conn:
            head=b''
            while b'\r\n\r\n' not in head:head+=conn.recv(4096)
            headers={l.split(':',1)[0].lower():l.split(':',1)[1].strip() for l in head.decode().split('\r\n')[1:] if ':' in l}
            accept=base64.b64encode(hashlib.sha1((headers['sec-websocket-key']+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            conn.sendall(f'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: obswebsocket.json\r\n\r\n'.encode())
            conn.sendall(frame(json.dumps({'op':0,'d':{'obsWebSocketVersion':'5.6.0','rpcVersion':1,'authentication':{'salt':'salt1','challenge':'chal1'}}})))
            identify=json.loads(read_frame(conn))
            if identify['d'].get('authentication')!=sources.obs_auth(self.password,'salt1','chal1'):
                conn.sendall(frame(struct.pack('!H',4009)+b'Authentication failed.',8));return
            conn.sendall(frame(json.dumps({'op':2,'d':{'negotiatedRpcVersion':1}})))
            try:
                while True:
                    request=json.loads(read_frame(conn))['d'];self.requests.append(request)
                    data=self.responses.get(request['requestType'])
                    conn.sendall(frame(json.dumps({'op':7,'d':{'requestType':request['requestType'],'requestId':request['requestId'],
                        'requestStatus':{'result':data is not None,'code':100 if data is not None else 204},'responseData':data or {}}})))
            except (EOFError,OSError):pass
    def close(self):self.server.close()

OBS_RESPONSES={'GetVersion':{'obsVersion':'32.1.2','obsWebSocketVersion':'5.6.0'},
    'GetStats':{'cpuUsage':12.5,'memoryUsage':500,'activeFps':60,'averageFrameRenderTime':2.1,'renderSkippedFrames':0,'renderTotalFrames':1000,'outputSkippedFrames':0,'outputTotalFrames':1000},
    'GetVideoSettings':{'fpsNumerator':60,'fpsDenominator':1,'baseWidth':1920,'baseHeight':1080,'outputWidth':1920,'outputHeight':1080},
    'GetStreamStatus':{'outputActive':True,'outputReconnecting':False,'outputDuration':5000,'outputCongestion':0.02,'outputBytes':1000000,'outputSkippedFrames':0,'outputTotalFrames':300},
    'GetRecordStatus':{'outputActive':False,'outputBytes':0},
    'GetStreamServiceSettings':{'streamServiceType':'rtmp_custom','streamServiceSettings':{'server':'rtmp://user:pw@ingest.example.net/live/SECRETKEY','key':'SECRETKEY'}},
    'GetOutputList':{'outputs':[{'outputName':'adv_stream','outputKind':'rtmp_output','outputActive':True},{'outputName':'NDI Main','outputKind':'ndi_output','outputActive':True}]},
    'GetOutputStatus':{'outputActive':True,'outputBytes':0,'outputSkippedFrames':0,'outputTotalFrames':10}}

class Sources(unittest.TestCase):
    def test_safe_url_never_keeps_keys_credentials_or_srt_parameters(self):
        self.assertEqual(sources.safe_url('rtmp://user:pw@a.rtmp.youtube.com/live2/abcd-efgh-key'),'rtmp://a.rtmp.youtube.com')
        self.assertEqual(sources.safe_url('srt://10.0.0.5:9000?streamid=#!::r=live/key,m=publish&passphrase=secret'),'srt://10.0.0.5:9000')
        self.assertEqual(sources.safe_url('rtmps://[2001:db8::1]:443/app/key'),'rtmps://[2001:db8::1]:443')
        self.assertEqual(sources.safe_url('not a url'),'')
        self.assertEqual(sources.protocol_of('srt://h:1'),'SRT');self.assertEqual(sources.protocol_of(''),'RTMP')
    def test_vmix_api_reads_local_xml_with_bounded_http_transport(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                body=b'<vmix><version>26.0</version><streaming>True</streaming></vmix>'
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            result=sources.vmix_api('http://127.0.0.1:'+str(server.server_port)+'/api')
            self.assertEqual(result['version'],'26.0');self.assertTrue(result['streaming'])
        finally:server.shutdown();server.server_close();thread.join(2)
    def test_obs_client_authenticates_and_reports_without_stream_key(self):
        server=FakeObs('pa55',OBS_RESPONSES)
        try:
            client=sources.ObsClient('127.0.0.1',server.port,'pa55');data=client.poll();client.close()
        finally:server.close()
        self.assertEqual(data['version']['obsVersion'],'32.1.2');self.assertTrue(data['stream']['outputActive'])
        self.assertEqual(data['service'],{'type':'rtmp_custom','server':'rtmp://ingest.example.net','protocol':'RTMP'})
        self.assertEqual([o['name'] for o in data['outputs']],['NDI Main'])
        self.assertNotIn('SECRETKEY',json.dumps(data));self.assertNotIn('pw@',json.dumps(data))
        self.assertTrue(all(r['requestType']!='GetStreamServiceSettings' or 'requestData' not in r for r in server.requests))
    def test_obs_wrong_password_and_missing_server_are_reported(self):
        server=FakeObs('right',OBS_RESPONSES)
        try:
            with self.assertRaisesRegex(sources.SourceError,'Неверный пароль'):sources.ObsClient('127.0.0.1',server.port,'wrong').poll()
        finally:server.close()
        closed=socket.socket();closed.bind(('127.0.0.1',0));port=closed.getsockname()[1];closed.close()
        with self.assertRaisesRegex(sources.SourceError,'не отвечает'):sources.ObsClient('127.0.0.1',port,'x',timeout=.5).poll()
    def test_local_obs_settings_used_only_for_this_computer(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'config.json';path.write_text(json.dumps({'server_enabled':True,'server_port':4466,'auth_required':True,'server_password':'local'}),encoding='utf-8')
            self.assertEqual(sources.local_obs_settings(path),{'port':4466,'password':'local','enabled':True})
            server=FakeObs('local',OBS_RESPONSES)
            try:
                path.write_text(json.dumps({'server_enabled':True,'server_port':server.port,'auth_required':True,'server_password':'local'}),encoding='utf-8')
                client=sources.ObsClient('127.0.0.1',None,'',config_path=path);self.assertTrue(client.poll()['stream']['outputActive']);client.close()
            finally:server.close()
            path.write_text(json.dumps({'server_enabled':False,'server_port':4455}),encoding='utf-8')
            with self.assertRaisesRegex(sources.SourceError,'выключен'):sources.ObsClient('localhost',None,'',config_path=path).connect()
            self.assertEqual(sources.local_obs_settings(Path(folder)/'missing.json'),{})
    def test_vmix_xml(self):
        xml='''<vmix><version>28.0.0.39</version><edition>4K</edition><inputs>
          <input key="a" number="1" type="SRT" title="SRT Cam" state="Running">x</input>
          <input key="b" number="2" type="NDI" title="NDI PC" state="Paused"/>
          <input key="c" number="3" type="Colour" title="Black" state="Paused"/></inputs>
          <recording>False</recording><external>True</external><streaming channel1="True" channel2="False">True</streaming><fullscreen>False</fullscreen></vmix>'''
        data=sources.parse_vmix_xml(xml)
        self.assertTrue(data['streaming']);self.assertEqual(data['streaming_channels'],['channel1']);self.assertTrue(data['external']);self.assertFalse(data['recording'])
        self.assertEqual([i['type'] for i in data['inputs'] if sources.STREAM_INPUT.search(i['type'])],['SRT','NDI'])
        with self.assertRaisesRegex(sources.SourceError,'http'):sources.vmix_api('file:///etc/passwd')
    def test_vmix_statistics_real_vmix26_overview_and_srt_headers(self):
        # Read from vMix Pro 26.0.0.45 Statistics (Overview tab, no inputs with drops).
        headers=['Input','Source Dropped','Renderer Dropped','Resync','Queue','Video','Audio']
        rows=sources.parse_vmix_list(headers,[['Output','0','0','0','0','Fullscreen Dropped: 0 0 1 (14 ms)','']])
        self.assertEqual((rows[0]['name'],rows[0]['kind'],rows[0]['fullscreen_dropped'],rows[0]['latency_ms'],rows[0]['source_dropped']),('Output','output',1,14,0))
        later=sources.parse_vmix_list(headers,[['Output','0','0','0','0','Fullscreen Dropped: 3 0 10 (11 ms)','']])
        self.assertEqual(sources.vmix_deltas(later,rows)[0]['delta']['fullscreen_dropped'],12)
        # Real SRT listener input fed by OBS: no "Connected ip:port", "(144)" without a unit is not taken as latency.
        srt_in=sources.parse_vmix_list(headers,[['SRT  5000','14788','0','26','2','33 33 33 34 (144)','120/40 ms Dropped: 24784/0 Resampled: 80/485720']])[0]
        self.assertEqual((srt_in['kind'],srt_in['source_dropped'],srt_in['resync'],srt_in['queue']),('srt_input',14788,26,2))
        self.assertEqual((srt_in['audio_buffer_ms'],srt_in['audio_dropped'],srt_in['audio_resampled']),(120,24784,485800))
        self.assertIsNone(srt_in.get('latency_ms'));self.assertNotIn('endpoint',srt_in)
        # SRT tab headers are real; the row values are synthetic (no SRT output was live).
        srt_headers=['Connection','Duration','Bandwidth (mbps)','Packet Loss (%)','Total Data','RTT (ms)']
        self.assertEqual(sources.parse_vmix_list(srt_headers,[]),[])
        srt=sources.parse_vmix_list(srt_headers,[['Output 1','00:01:10','5.92','0.15','52 MB','12']])[0]
        self.assertEqual((srt['kind'],srt['name'],srt['bandwidth_kbps'],srt['packet_loss_pct'],srt['rtt_ms']),('srt','Output 1',5920,0.15,12))
        self.assertIn('Packet Loss (%): 0.15',srt['video'])
        self.assertIsNone(sources.parse_vmix_list(srt_headers,[['Output 1','','—','','','']])[0]['bandwidth_kbps'])
    def test_vmix_statistics_rows_and_deltas(self):
        headers=['Name','Source Dropped','Renderer Dropped','Resync','Queue','Video','Audio']
        first=sources.parse_vmix_rows(headers,[['Output','0','1','0','2','Fullscreen Dropped: 1 2 3 (40ms)','48/48ms Dropped: 0/0 Resampled: 1/1'],
                                               ['SRT Cam','5','0','0','0','Connected 10.0.0.9:9000','40/40ms Dropped: 2/1 Resampled: 0/0']])
        self.assertEqual(first[0]['kind'],'output');self.assertEqual(first[0]['fullscreen_dropped'],6);self.assertEqual(first[0]['latency_ms'],40)
        self.assertEqual(first[1]['kind'],'srt_input');self.assertEqual(first[1]['endpoint'],'10.0.0.9:9000');self.assertEqual(first[1]['audio_dropped'],3)
        second=sources.parse_vmix_rows([],[['Output','0','1','0','2','Fullscreen Dropped: 1 2 5 (40ms)',''],['SRT Cam','3','0','0','0','','']])
        sources.vmix_deltas(second,first)
        self.assertEqual(second[0]['delta']['fullscreen_dropped'],2)
        self.assertNotIn('source_dropped',second[1]['delta'])  # counter reset is not loss
    def test_estats_rtt_sentinel_and_zero(self):
        # Values seen as administrator on Windows 10: no RTT sample yet, then a loopback transfer.
        base={'DataBytesOut':0,'DataBytesIn':0,'DataSegsOut':0,'PktsRetrans':0,'BytesRetrans':0,'Timeouts':0,'CongSignals':0}
        empty=sources.estats_values({**base,'SampleRtt':0xffffffff,'SmoothedRtt':0,'MinRtt':0xffffffff,'MaxRtt':0,'CountRtt':0})
        self.assertEqual((empty['rtt'],empty['rtt_min'],empty['rtt_max']),(None,None,None))
        loop=sources.estats_values({**base,'DataBytesOut':2000052,'DataSegsOut':40,'SampleRtt':0,'SmoothedRtt':0,'MinRtt':0,'MaxRtt':1,'CountRtt':1216})
        self.assertEqual((loop['bytes_out'],loop['rtt'],loop['rtt_min'],loop['rtt_max']),(2000052,0,0,1))
    def test_mdns_ndi_records(self):
        def name(text):return b''.join(bytes([len(p)])+p.encode() for p in text.split('.'))+b'\0'
        def record(owner,kind,body):return owner+struct.pack('!HHIH',kind,1,120,len(body))+body
        service=name('_ndi._tcp.local');header=struct.pack('!6H',0,0x8400,0,1,0,2)
        ptr_owner_offset=12;instance=bytes([len('PC (Cam 1)')])+b'PC (Cam 1)'+bytes([0xc0,ptr_owner_offset])
        packet=header+record(service,12,instance)
        instance_offset=12+len(service)+10
        packet+=record(bytes([0xc0,instance_offset]),33,struct.pack('!3H',0,0,5961)+name('pc.local'))
        packet+=record(name('pc.local'),1,socket.inet_aton('192.0.2.10'))
        found=sources.ndi_sources(sources.parse_mdns(packet))
        self.assertEqual(found,[{'name':'PC (Cam 1)','host':'pc.local','ip':'192.0.2.10','port':5961}])
        self.assertEqual(sources.classify_connection({'remote':'192.0.2.10','remote_port':5961,'local_port':50000},{('192.0.2.10',5961)}),'NDI')
        self.assertEqual(sources.classify_connection({'remote':'198.51.100.1','remote_port':1935,'local_port':50000}),'RTMP')
        self.assertTrue(sources.mdns_query().endswith(struct.pack('!HH',12,0x8001)))

@unittest.skipUnless(sys.platform=='win32','Windows API')
class WindowsSources(unittest.TestCase):
    def test_reads_list_view_of_another_process_window(self):
        script=r'''Add-Type -AssemblyName System.Windows.Forms
$f=New-Object Windows.Forms.Form;$f.Text='Statistics';$f.ShowInTaskbar=$false;$f.StartPosition='Manual';$f.Location=New-Object Drawing.Point(-2000,-2000)
$s=New-Object Windows.Forms.ListView;$s.View='Details';$s.Dock='Fill'
foreach($c in 'Connection','Duration','Bandwidth (mbps)','Packet Loss (%)','Total Data','RTT (ms)'){[void]$s.Columns.Add($c,80)}
$j=New-Object Windows.Forms.ListViewItem('Output 1');foreach($v in '00:00:05','6.0','0.5','4 MB','20'){[void]$j.SubItems.Add($v)};[void]$s.Items.Add($j)
$l=New-Object Windows.Forms.ListView;$l.View='Details';$l.Dock='Fill'
foreach($c in 'Input','Source Dropped','Renderer Dropped','Resync','Queue','Video','Audio'){[void]$l.Columns.Add($c,80)}
$i=New-Object Windows.Forms.ListViewItem('SRT Cam');foreach($v in '4','0','0','1','Connected 10.0.0.9:9000','40/40ms Dropped: 1/0 Resampled: 0/0'){[void]$i.SubItems.Add($v)};[void]$l.Items.Add($i)
$f.Controls.Add($s);$f.Controls.Add($l);$f.Add_Shown({$s.Visible=$false});$t=New-Object Windows.Forms.Timer;$t.Interval=20000;$t.Add_Tick({$f.Close()});$t.Start();[void]$f.ShowDialog()'''
        process=subprocess.Popen(['powershell','-NoProfile','-NonInteractive','-Command',script],creationflags=0x08000000)
        try:
            found=None;deadline=time.time()+15
            # The SRT list is hidden in the Shown handler; a window found before that is not ready yet.
            while time.time()<deadline:
                found=sources.find_statistics_window(('powershell.exe',))
                if found and len(found[1])==2 and not sources.user32.IsWindowVisible(found[1][1]):break
                time.sleep(.2)
            self.assertIsNotNone(found)
            # Like vMix: the hidden SRT tab list exists too; the visible list comes first.
            pid,lists=found;self.assertEqual(len(lists),2)
            rows=sources.parse_vmix_list(*sources.read_list_view(pid,lists[0]))
            self.assertEqual(rows[0]['name'],'SRT Cam');self.assertEqual(rows[0]['source_dropped'],4);self.assertEqual(rows[0]['endpoint'],'10.0.0.9:9000');self.assertEqual(rows[0]['audio_dropped'],1)
            with patch.object(sources,'find_statistics_window',return_value=found):rows=sources.vmix_statistics()
            self.assertEqual([(r['kind'],r['name']) for r in rows],[('srt_input','SRT Cam'),('srt','Output 1')]);self.assertEqual(rows[1]['bandwidth_kbps'],6000)
        finally:process.kill();process.wait(5)
    def test_tcp_table_lists_own_connection_and_estats_degrades_without_rights(self):
        server=socket.socket();server.bind(('127.0.0.1',0));server.listen();client=socket.create_connection(server.getsockname());accepted,_=server.accept()
        try:
            client.sendall(b'x'*50000);mine=[c for c in sources.tcp_table() if c['pid']==os.getpid() and c['local_port']==client.getsockname()[1]]
            self.assertEqual(len(mine),1);self.assertEqual(sources.process_name(os.getpid()),os.path.basename(sys.executable).lower())
            stats=sources.tcp_estats(mine[0])
            if stats is not None:self.assertGreaterEqual(stats['bytes_out'],50000)
        finally:client.close();accepted.close();server.close()

class Clock:
    def __init__(self):self.now=1_800_000_000.0
    def __call__(self):return self.now

def obs_data(seconds,bytes_,skipped,active=True,reconnecting=False,congestion=.01,render=0):
    data=json.loads(json.dumps({k:v for k,v in OBS_RESPONSES.items()}))
    stream={'outputActive':active,'outputReconnecting':reconnecting,'outputDuration':seconds*1000,'outputCongestion':congestion,'outputBytes':bytes_,'outputSkippedFrames':skipped,'outputTotalFrames':seconds*60}
    data['GetStats']['renderSkippedFrames']=render
    return {'version':data['GetVersion'],'stats':data['GetStats'],'video':data['GetVideoSettings'],'stream':stream,'record':data['GetRecordStatus'],
            'service':{'type':'rtmp_custom','server':'rtmp://ingest.example.net','protocol':'RTMP'},'outputs':[]}

class Monitor(unittest.TestCase):
    def poll(self,monitor,clock,obs,**extra):
        clock.now+=1;monitor.cycle(clock.now,obs=obs,vmix_api=extra.get('vmix_api',sources.SourceError('vMix API не отвечает')),vmix_rows=extra.get('vmix_rows'),
                                   connections=extra.get('connections',[]),ndi=extra.get('ndi',[]))
    def test_bitrate_drops_reconnects_and_report(self):
        clock=Clock();monitor=stream_monitor.StreamMonitor(clock=clock)
        self.poll(monitor,clock,obs_data(10,1_000_000,0))
        self.poll(monitor,clock,obs_data(11,1_750_000,0))
        stream=monitor.streams['obs:stream'];self.assertEqual(stream['state'],'live');self.assertAlmostEqual(stream['bitrate'],6000)
        self.poll(monitor,clock,obs_data(12,2_500_000,30,congestion=.7,render=4))
        self.assertEqual(monitor.streams['obs:stream']['dropped_delta'],30)
        self.poll(monitor,clock,obs_data(13,2_500_000,30,reconnecting=True))
        self.poll(monitor,clock,obs_data(14,100,30))  # counter reset after reconnect
        self.assertIsNone(monitor.streams['obs:stream']['bitrate']);self.assertEqual(monitor.streams['obs:stream']['reconnects'],1)
        messages='\n'.join(e['message'] for e in monitor.events)
        for text in ('потеряно 30','перегрузка канала','переподключение','пропущено рендером'):self.assertIn(text,messages)
        summary=monitor.summary('obs:stream',300);self.assertEqual(summary['dropped'],30);self.assertEqual(summary['bitrate']['max'],6000)
        report=monitor.report(300,['ICMP до ya.ru: средн. 5 мс'])
        for text in ('UTC','rtmp://ingest.example.net','Потеряно'.lower(),'ICMP до ya.ru','Congestion','переподключений 1'):self.assertIn(text,report.lower() if text.islower() else report)
        self.assertNotIn('SECRETKEY',report)
        snapshot=monitor.snapshot(300,key='obs:stream');self.assertEqual(list(snapshot['history']),['obs:stream']);self.assertEqual(len(snapshot['history']['obs:stream']),5)
    def test_obs_counter_reset_after_reconnect_twitch_obs32(self):
        # Shape of GetStreamStatus around a reconnect seen live (OBS 32.1.2 → Twitch, TCP reset):
        # bytes/frames/duration restart from zero; «reconnecting» lasted ~3 s.
        def status(seconds,bytes_,total,skipped=0,active=True,reconnecting=False):
            data=obs_data(seconds,bytes_,skipped,active,reconnecting);data['stream'].update(outputTotalFrames=total,outputDuration=seconds*1000);return data
        clock=Clock();monitor=stream_monitor.StreamMonitor(clock=clock)
        conn={'app':'OBS','process':'obs64.exe','protocol':'RTMP','remote':'35.55.40.35','remote_port':1935,'local_port':50001,
              'estats':{'bytes_out':0,'segments_out':0,'retrans_segments':0,'rtt':0}}
        self.poll(monitor,clock,status(27.299,20_550_890,819,skipped=2),connections=[conn])
        self.poll(monitor,clock,status(28.366,21_392_667,851,skipped=2,reconnecting=True),connections=[dict(conn,estats=dict(conn['estats'],bytes_out=842_000,segments_out=600))])
        self.poll(monitor,clock,status(0,0,0,reconnecting=False))
        self.poll(monitor,clock,status(1.599,1_357_835,48))
        stream=monitor.streams['obs:stream']
        self.assertEqual(stream['reconnects'],1)  # the reset after «reconnecting» is not counted twice
        self.assertEqual((stream['frames_total'],stream['frames_dropped']),(851+48,2));self.assertAlmostEqual(stream['duration'],28.366+1.599)
        # A reconnect between two polls shows only as a byte counter reset.
        self.poll(monitor,clock,status(0.5,300_000,15))
        self.assertEqual(stream['reconnects'],2);self.assertEqual(stream['frames_total'],851+48+15)
        self.assertIn('счётчики OBS начались заново',''.join(e['message'] for e in monitor.events))
        self.poll(monitor,clock,status(0,0,0,active=False))
        self.assertIsNone(stream['duration']);self.assertAlmostEqual(stream['aired'],28.366+1.599+.5)
        report=monitor.report(3600)
        self.assertIn('в эфире 00:00:30',report);self.assertIn('переподключений 2',report)
        self.assertIn('· 00:00:05',report.splitlines()[2])  # period starts at the first poll without start()
        self.assertIn('вероятно, локальный VPN/прокси',report)
        # A new broadcast starts its totals from zero.
        self.poll(monitor,clock,status(1,500_000,30));self.assertEqual(stream['frames_total'],30)
    def test_pause_preserves_known_obs_segments_and_resets_rate_baseline(self):
        clock=Clock();monitor=stream_monitor.StreamMonitor(clock=clock)
        self.poll(monitor,clock,obs_data(20,1_000_000,5))
        self.poll(monitor,clock,obs_data(1,100_000,0))
        before=monitor.streams['obs:stream']['frames_total']
        monitor.pause();clock.now+=10
        self.poll(monitor,clock,obs_data(2,200_000,1))
        stream=monitor.streams['obs:stream']
        self.assertGreater(stream['frames_total'],before);self.assertEqual(stream['frames_dropped'],6)
        self.assertIsNone(stream['bitrate']);self.assertIsNone(stream['dropped_delta'])
        self.assertTrue(stream['observation_gap']);self.assertIn('Наблюдение прерывалось',monitor.report())
    def test_remote_encoder_does_not_receive_local_tcp_metrics(self):
        clock=Clock();monitor=stream_monitor.StreamMonitor(clock=clock)
        monitor.settings.update(obs_host='192.0.2.10',vmix_url='http://192.0.2.11:8088/api')
        local={'app':'OBS','process':'obs64.exe','protocol':'RTMP','remote':'198.51.100.7','remote_port':1935,'local_port':50001,
               'estats':{'bytes_out':0,'segments_out':0,'retrans_segments':0,'rtt':99}}
        self.poll(monitor,clock,obs_data(10,1_000_000,0),connections=[local])
        self.assertIsNone(monitor.streams['obs:stream']['rtt']);self.assertNotIn('tcp_target',monitor.streams['obs:stream'])
    def test_slow_sources_do_not_block_the_poll(self):
        monitor=stream_monitor.StreamMonitor();release=threading.Event()
        began=time.monotonic();self.assertIsNone(monitor.fetch('slow',lambda:release.wait(5) and 42,1,100));self.assertLess(time.monotonic()-began,.2)
        self.assertIsNone(monitor.fetch('slow',lambda:0,1,101))  # still running: no new result, no second thread
        release.set();monitor.jobs['slow']['thread'].join(2)
        self.assertEqual(monitor.fetch('slow',lambda:7,1,101.5),42)
        monitor.jobs['slow']['thread'].join(2);self.assertEqual(monitor.fetch('slow',lambda:8,1,101.6),7)
        self.assertIsNone(monitor.fetch('slow',lambda:9,1,101.7))  # next read is due at 102.5
        monitor.reset();self.assertEqual(monitor.jobs,{})
    def test_sources_errors_vmix_rows_tcp_and_history_limit(self):
        clock=Clock();monitor=stream_monitor.StreamMonitor(clock=clock,history_seconds=10)
        rows=lambda drops:sources.parse_vmix_rows([],[['SRT Cam',str(drops),'0','0','0','Connected 10.0.0.9:9000','']])
        conn={'app':'OBS','process':'obs64.exe','protocol':'RTMP','remote':'198.51.100.7','remote_port':1935,'local_port':50001,
              'estats':{'bytes_out':0,'segments_out':0,'retrans_segments':0,'rtt':40}}
        self.poll(monitor,clock,sources.SourceError('OBS не отвечает'),vmix_rows=rows(1),connections=[conn])
        self.assertEqual(monitor.status['obs']['state'],'error');self.assertEqual(monitor.status['vmix_api']['state'],'error')
        conn2=dict(conn,estats={'bytes_out':500_000,'segments_out':400,'retrans_segments':20,'rtt':320})
        self.poll(monitor,clock,sources.SourceError('OBS не отвечает'),vmix_rows=rows(4),connections=[conn2])
        tcp=monitor.streams['tcp:OBS:198.51.100.7:1935:50001'];self.assertAlmostEqual(tcp['bitrate'],4000);self.assertAlmostEqual(tcp['retrans_pct'],5)
        self.assertEqual(stream_monitor.level('rtt',tcp['rtt']),2)
        self.assertIn('+3 потерь',''.join(e['message'] for e in monitor.events))
        srt=sources.parse_vmix_list(['Connection','Duration','Bandwidth (mbps)','Packet Loss (%)','Total Data','RTT (ms)'],[['Output 1','00:00:09','4.5','2','5 MB','80']])
        monitor.update_vmix_rows(clock.now,rows(4)+srt);link=monitor.streams['vmix:srt:Output 1']
        self.assertEqual((link['protocol'],link['bitrate'],link['rtt']),('SRT',4500,80));self.assertIn('потери пакетов 2 %',link['note'])
        self.assertEqual(monitor.vmix_rows[0]['delta'],{'source_dropped':0,'renderer_dropped':0});self.assertEqual(monitor.vmix_rows[1]['delta'],{})
        monitor.vmix_info={'inputs':[{'number':'3','type':'SRT','title':'Камера 2','state':'Running'}]}
        monitor.update_vmix_rows(clock.now,sources.parse_vmix_list([],[['Камера 2','0','0','0','0','33 33 33 34 (144)','']]))
        self.assertEqual(monitor.streams['vmix:stat:Камера 2']['protocol'],'SRT вход')
        self.poll(monitor,clock,sources.SourceError('OBS не отвечает'),vmix_rows=None,connections=[])
        self.assertEqual(monitor.status['vmix_stats']['text'],'окно Statistics закрыто')
        self.assertEqual(monitor.streams['vmix:stat:SRT Cam']['state'],'lost')
        clock.now+=30;self.poll(monitor,clock,sources.SourceError('x'))
        self.assertNotIn('tcp:OBS:198.51.100.7:1935:50001',monitor.streams)
        monitor.clear();self.assertEqual(monitor.streams,{});self.assertEqual(list(monitor.events),[])

class Widgets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import desktop
        from qt_support import production_application
        cls.desktop=desktop;cls.app=production_application(cls)
    def test_stream_panel_renders_streams_charts_and_vmix_table(self):
        import stream_widgets
        clock=Clock();monitor=stream_monitor.StreamMonitor(clock=clock);helper=Monitor()
        for i in range(5):helper.poll(monitor,clock,obs_data(10+i,1_000_000*(i+1),i*2),vmix_rows=sources.parse_vmix_rows([],[['Output','0','0','0','0','Fullscreen Dropped: 0 0 %d (40ms)'%i,'']]))
        panel=stream_widgets.StreamPanel(lambda:['сеть ок'],monitor=monitor)
        try:
            panel.resize(1100,800);panel.show();panel.refresh();self.app.processEvents()
            self.assertEqual(panel.selected,'obs:stream');self.assertGreater(panel.table.rowCount(),0)
            self.assertEqual(panel.table.item(0,3).text(),'В ЭФИРЕ');self.assertEqual(panel.table.item(0,5).text(),'8000')
            self.assertEqual(panel.vmix_table.rowCount(),1);self.assertIn('fullscreen_dropped +1',panel.vmix_table.item(0,7).text())
            self.assertEqual(len(panel.plots[0].points),5);panel.period.setCurrentIndex(0);self.app.processEvents();image=panel.plots[0].grab().toImage()
            self.assertTrue(any(image.pixelColor(x,y).green()>150 and image.pixelColor(x,y).red()<160 for x in range(50,image.width()) for y in range(25,image.height()-25)))
            self.assertIn('сеть ок',panel.report_text());panel.copy_report();flush_report(panel);self.assertIn('отчёт о трансляции',self.app.clipboard().text())
            self.assertIn('потеряно',panel.events.toPlainText())
        finally:panel.shutdown();panel.close()
        self.assertEqual(monitor.streams,{})
    def test_window_opened_on_stream_tab_does_not_grow(self):
        from unittest.mock import patch
        from PySide6.QtCore import QCoreApplication,QEvent
        with tempfile.TemporaryDirectory() as folder,patch.object(self.desktop.Discovery,'start'):
            w=self.desktop.Window(Path(folder));w.timer.stop()
            try:
                w.resize(900,640);w.tabs.setCurrentIndex(1);w.show()
                for _ in range(5):self.app.processEvents()
                height=w.height();w.resize(1300,height)
                for _ in range(5):self.app.processEvents()
                # Hidden Network splitters are empty; balancing them used to raise the minimum without bound.
                self.assertEqual(w.height(),height);self.assertLessEqual(w.minimumHeight(),height)
                w.tabs.setCurrentIndex(0)
                for _ in range(3):self.app.processEvents()
                self.assertEqual(w.panes.widget(0).height(),w.panes.widget(1).height());self.assertEqual(w.height(),height)
            finally:
                close_window(w);w.store.db.close();w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    def test_columns_are_resizable_and_share_width_in_user_proportions(self):
        import stream_widgets
        from PySide6.QtWidgets import QHeaderView,QPushButton
        panel=stream_widgets.StreamPanel(monitor=stream_monitor.StreamMonitor())
        try:
            panel.resize(1100,800);panel.show();self.app.processEvents()
            for table in (panel.table,panel.vmix_table):
                header=table.horizontalHeader()
                self.assertTrue(all(header.sectionResizeMode(i)==QHeaderView.ResizeMode.Interactive for i in range(table.columnCount())))
                self.assertLessEqual(abs(sum(table.columnWidth(i) for i in range(table.columnCount()))-table.viewport().width()),1)
            before=[panel.table.columnWidth(i) for i in range(3)]
            panel.resize(1500,800);self.app.processEvents()
            after=[panel.table.columnWidth(i) for i in range(3)]
            self.assertTrue(all(b<a for a,b in zip(after,before)))  # every column grows, not only the last
            self.assertAlmostEqual(after[2]/after[0],before[2]/before[0],delta=.05)
            panel.table.setColumnWidth(0,400);self.app.processEvents()  # user drag
            panel.resize(1200,800);self.app.processEvents()
            self.assertGreater(panel.table.columnWidth(0),panel.table.columnWidth(2))
            self.assertIsInstance(panel.copy_button,QPushButton);self.assertIsInstance(panel.save_button,QPushButton)
            self.assertLess(panel.copy_button.geometry().right(),panel.save_button.geometry().left())
            self.assertEqual(panel.copy_button.cursor().shape(),stream_widgets.Qt.CursorShape.PointingHandCursor)
        finally:panel.shutdown();panel.close()
    def test_window_has_stream_tab_and_taller_latency_charts(self):
        from unittest.mock import patch
        from PySide6.QtCore import QCoreApplication,QEvent
        with tempfile.TemporaryDirectory() as folder,patch.object(self.desktop.Discovery,'start'):
            w=self.desktop.Window(Path(folder));w.timer.stop()
            try:
                w.resize(1100,900);w.show()
                for _ in range(3):self.app.processEvents()
                self.assertEqual([w.tabs.tabText(i) for i in range(w.tabs.count())],['Сеть','Stream'])
                self.assertEqual(w.panes.orientation(),self.desktop.Qt.Orientation.Horizontal)
                self.assertEqual(w.panes.widget(0).height(),w.panes.widget(1).height())
                self.assertGreater(w.graph.height(),w.jitter.height()*2)
                self.assertGreaterEqual(w.jitter.height(),56);self.assertGreaterEqual(w.graph.height(),140)
                self.assertIn('ICMP-мониторинг не запущен',w.network_report())
                w.tabs.setCurrentIndex(1);self.app.processEvents();self.assertTrue(w.stream_panel.isVisible())
                w.tabs.setCurrentIndex(0)
                for _ in range(3):self.app.processEvents()
                self.assertEqual(w.panes.widget(0).height(),w.panes.widget(1).height())
            finally:
                close_window(w);w.store.db.close();w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            self.assertTrue(w.stream_panel.closing)

if __name__=='__main__':unittest.main()

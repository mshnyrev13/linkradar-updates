import sys,unittest,threading,time,gc,socket
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import speed_test as s
from unittest.mock import patch

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Length','262144');self.end_headers()
        try:
            for i in range(16):self.wfile.write(bytes(16384));self.wfile.flush();time.sleep(.015)
        except (BrokenPipeError,ConnectionResetError):pass
    def do_POST(self):
        remaining=int(self.headers['Content-Length'])
        while remaining:
            chunk=self.rfile.read(min(65536,remaining));remaining-=len(chunk);time.sleep(.005)
        self.send_response(200);self.end_headers();self.wfile.write(b'OK')

class SpeedTests(unittest.TestCase):
    def test_upload_stream_has_exact_length_and_bounded_storage(self):
        import requests
        body=s.UploadBody(16*1024*1024+7,'Boundary')
        self.assertLessEqual(len(body.chunk),65536)
        chunks=iter(body);prefix=next(chunks)
        self.assertIn(b'name="data"',prefix)
        sizes=[];last=None
        for chunk in chunks:sizes.append(len(chunk));last=chunk
        self.assertEqual(sum(sizes)+len(prefix),len(body))
        self.assertEqual(last,b'\r\n--Boundary--\r\n')
        self.assertLessEqual(max(sizes),65536)
        prepared=requests.Request('POST','https://example.test/',data=body).prepare()
        self.assertEqual(int(prepared.headers['Content-Length']),len(body))
        self.assertNotIn('Transfer-Encoding',prepared.headers)

    def test_slow_upload_bootstraps_before_timeout_at_one_mbit(self):
        class SlowUpload(Handler):
            protocol_version='HTTP/1.1'
            lock=threading.Lock();next_chunk=time.perf_counter()
            def setup(self):
                super().setup();self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            def do_POST(self):
                remaining=int(self.headers['Content-Length'])
                try:
                    while remaining:
                        data=self.rfile.read(min(8192,remaining))
                        if not data:return
                        with self.lock:
                            SlowUpload.next_chunk=max(time.perf_counter(),SlowUpload.next_chunk)+len(data)*8/1e6
                            until=SlowUpload.next_chunk
                        time.sleep(max(0,until-time.perf_counter()));remaining-=len(data)
                    self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'OK')
                except (BrokenPipeError,ConnectionResetError):pass
        gc.collect()
        http=ThreadingHTTPServer(('127.0.0.1',0),SlowUpload);http.daemon_threads=True
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:
            result=s.SpeedTest(seconds=2,parallel=4).phase('upload',[f'http://127.0.0.1:{http.server_port}/'])
            self.assertGreater(result['mbps'],.8);self.assertLess(result['mbps'],1.2)
            self.assertFalse(result['partial']);self.assertEqual(result['seconds'],2)
            self.assertAlmostEqual(result['mbps'],result['measurement_bytes']*8/result['seconds']/1e6,places=2)
        finally:http.shutdown();http.server_close()
    def test_default_test_measures_both_directions_for_fifteen_seconds(self):
        test=s.SpeedTest();durations=[]
        config={direction:{'probes':[{'url':'https://x.cdn.yandex.net/probe'}]} for direction in ('download','upload')}
        def phase(direction,urls):
            durations.append((direction,test.seconds));return {'mbps':10,'seconds':test.seconds}
        with patch.object(test,'config',return_value=config),patch.object(test,'phase',side_effect=phase):
            result=test.run()
        self.assertEqual(durations,[('download',15),('upload',15)])
        self.assertEqual(result['download']['seconds'],15);self.assertEqual(result['upload']['seconds'],15)
    def test_transport_read_failure_is_a_measurement_error(self):
        from urllib3.exceptions import ReadTimeoutError
        test=s.SpeedTest(seconds=.1,parallel=1)
        with patch.object(test,'open',side_effect=ReadTimeoutError(None,None,'timed out')):
            with self.assertRaisesRegex(ValueError,'не удалось'):test.phase('download',['http://127.0.0.1/'])
    def test_stability_includes_idle_seconds(self):
        self.assertTrue(s.summarize_rates([10]*60)['stable'])
        result=s.summarize_rates([0]*30+[20]*30)
        self.assertFalse(result['stable']);self.assertEqual(result['p10'],0);self.assertEqual(result['p90'],20)
    def test_config_restricts_tls_and_provider(self):
        for u in ['http://x.cdn.yandex.net/a','https://example.com/a','https://u:p@x.cdn.yandex.net/a','https://x.cdn.yandex.net:444/a']:
            with self.assertRaises(ValueError):s.valid_url(u)
        rows=s.probes({'download':{'probes':[{'url':'https://x.cdn.yandex.net/big'},{'url':'https://x.cdn.yandex.net/small','timeout':100}]}},'download')
        self.assertEqual(rows,['https://x.cdn.yandex.net/big'])
    def test_real_http_download_upload_and_cancel(self):
        # Release cyclic Qt fixtures on the main thread before HTTP workers allocate.
        gc.collect()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url='http://127.0.0.1:'+str(server.server_port)+'/'
            updates=[];test=s.SpeedTest(seconds=1.7,parallel=2,progress=updates.append)
            for direction in ['download','upload']:
                r=test.phase(direction,[url]);self.assertGreater(r['mbps'],0);self.assertGreater(r['bytes'],0);self.assertFalse(r['partial'])
            fractions=[u['total_progress'] for u in updates]
            self.assertEqual(fractions,sorted(fractions))
            self.assertTrue(all(0<=u['total_progress']<=.5 for u in updates if u['phase']=='download'))
            self.assertTrue(all(.5<=u['total_progress']<=1 for u in updates if u['phase']=='upload'))
            test.stop.set()
            with self.assertRaises(s.Cancelled):test.phase('download',[url])
        finally:server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()

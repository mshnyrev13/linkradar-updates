"""Real loopback TCP + HTTP API tests; never contact external hosts."""
import json
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import packet_tools


class LocalIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.http.daemon_threads = True
        cls.http.token = secrets.token_hex(24)
        cls.http.packets = server.PacketTools(cls.temp.name)
        cls.http.monitor = server.Monitor(server.Store(Path(cls.temp.name) / "network.sqlite3"))
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.http.server_port

    @classmethod
    def tearDownClass(cls):
        cls.http.monitor.stop()
        deadline = time.time() + 4
        while cls.http.monitor.sid and time.time() < deadline:
            time.sleep(.05)
        cls.http.shutdown()
        cls.http.server_close()
        cls.http.monitor.store.db.close()
        cls.temp.cleanup()

    def request(self, path, body=None, headers=None):
        headers = headers or {}
        headers.setdefault("X-Local-Token", self.http.token)
        payload = json.dumps(body).encode() if body is not None else None
        if payload:
            headers["Content-Type"] = "application/json"
        return urlopen(Request(self.base + path, payload, headers), timeout=3)

    def test_tcp_success_and_refusal(self):
        t = {"host": "127.0.0.1", "port": self.http.server_port, "mode": "tcp"}
        self.assertEqual(server.probe(t, 1)["status"], "ok")
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
        # Probe a closed port, not an open bound-but-non-listening socket (macOS
        # can drop SYNs to the latter). Windows can retry before WSAECONNREFUSED,
        # so allow the OS to report refusal instead of exhausting a 1 s deadline.
        started=time.monotonic()
        result=server.probe(dict(t, port=port), 5)
        self.assertEqual(result["status"], "refused")
        print('TCP_REFUSAL '+json.dumps({'status':result['status'],'seconds':round(time.monotonic()-started,3)}))

    def test_api_lifecycle_and_exports(self):
        result = json.load(self.request("/api/start", {"name": "Integration test", "duration_minutes": 1,
                                                       "targets": [{"host": "127.0.0.1", "mode": "tcp", "port": self.http.server_port}]}))
        sid = result["id"]
        with self.assertRaises(HTTPError) as duplicate:
            self.request("/api/start", {"targets": [{"host": "127.0.0.1"}]})
        self.assertEqual(duplicate.exception.code, 400)
        deadline = time.time() + 3
        while time.time() < deadline:
            snapshot = json.load(self.request("/api/state?session=" + sid))
            if snapshot["targets"][0]["stats"]["count"] >= 2:
                break
            time.sleep(.1)
        self.assertGreaterEqual(snapshot["targets"][0]["stats"]["count"], 2)
        self.assertEqual(snapshot["verdict"], "pending")
        self.request("/api/comment", {"session": sid, "message": "Начало проверки"}).close()
        exported = json.load(self.request("/api/export.json?session=" + sid))
        self.assertEqual(len(exported["raw_samples"]), exported["targets"][0]["stats"]["count"])
        self.assertTrue(any(e["message"] == "Начало проверки" for e in exported["all_events"]))
        csv = self.request("/api/export.csv?session=" + sid).read().decode("utf-8-sig")
        self.assertIn("latency_ms", csv)
        self.assertEqual(len(csv.splitlines()) - 1, len(exported["raw_samples"]))
        self.request("/api/stop", {}).close()
        deadline = time.time() + 3
        while self.http.monitor.sid and time.time() < deadline:
            time.sleep(.05)
        stopped = json.load(self.request("/api/state?session=" + sid))
        self.assertEqual(stopped["session"]["status"], "stopped")
        self.assertEqual(stopped["verdict"], "pending")

    @unittest.skipUnless(packet_tools.tool('tshark'),'Optional TShark is not installed')
    def test_packet_upload_and_filtered_analysis(self):
        from test_routes_packets import fixture_pcap
        payload=fixture_pcap()
        req=Request(self.base+'/api/packets/upload',payload,{'X-Local-Token':self.http.token,'Content-Type':'application/octet-stream'})
        identifier=json.load(urlopen(req,timeout=3))['id']
        result=json.load(self.request('/api/packets/analyze',{'id':identifier,'filter':'udp'}))
        self.assertEqual(result['packets'],2)
        self.assertEqual(self.request('/api/packets/file?id='+identifier).read(),payload)
        with self.assertRaises(HTTPError):
            self.request('/api/packets/analyze',{'id':'../outside','filter':''})

    def test_packet_upload_without_optional_tshark_returns_controlled_error(self):
        from test_routes_packets import fixture_pcap
        payload=fixture_pcap()
        req=Request(self.base+'/api/packets/upload',payload,{'X-Local-Token':self.http.token,'Content-Type':'application/octet-stream'})
        identifier=json.load(urlopen(req,timeout=3))['id']
        self.assertEqual(self.request('/api/packets/file?id='+identifier).read(),payload)
        with patch.object(packet_tools,'tool',return_value=None),self.assertRaises(HTTPError) as missing:
            self.request('/api/packets/analyze',{'id':identifier,'filter':'udp'})
        self.assertEqual(missing.exception.code,400)

    def test_pause_resume_preserves_history(self):
        sid=json.load(self.request('/api/start',{'targets':[{'host':'127.0.0.1','mode':'tcp','port':self.http.server_port}]}))['id']
        time.sleep(.2)
        self.assertTrue(json.load(self.request('/api/pause',{'session':sid}))['paused'])
        time.sleep(.2)
        a=json.load(self.request('/api/state?session='+sid))
        self.assertTrue(a['paused'])
        count=a['targets'][0]['stats']['count']
        time.sleep(1.2)
        b=json.load(self.request('/api/state?session='+sid))
        self.assertEqual(b['targets'][0]['stats']['count'],count)
        self.assertFalse(json.load(self.request('/api/pause',{'session':sid}))['paused'])
        time.sleep(1.2)
        c=json.load(self.request('/api/state?session='+sid))
        self.assertGreater(c['targets'][0]['stats']['count'],count)
        self.request('/api/stop',{}).close()
        deadline=time.time()+3
        while self.http.monitor.sid and time.time()<deadline:time.sleep(.05)
        with self.assertRaises(HTTPError):self.request('/api/pause',{'session':sid})

    def test_security_origin_host_and_token(self):
        for headers in ({"Origin": "https://other-site.example"}, {"Host": "evil.example"}, {"X-Local-Token": "invalid"}):
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as denied:
                self.request("/api/history", headers=headers)
            self.assertEqual(denied.exception.code, 403)
        self.assertIn(b"LinkRadar", self.request("/").read())
        with self.assertRaises(HTTPError) as invalid:
            self.request("/api/state?window=nan")
        self.assertEqual(invalid.exception.code, 400)


if __name__ == "__main__":
    unittest.main()

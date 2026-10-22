"""Security regressions: private fixtures only, no privilege or installed services."""
import json
import os
import random
import socket
import stat
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import background_engine as ipc
import error_log
import graphene_port
import interface_probes
import ipc_transport
import network_info
import server
import service_paths
import stream_sources
import traffic_capture
import bound_http
import requests
from multiprocessing.connection import Connection as NativeConnection


@unittest.skipIf(os.name=='nt','POSIX socket framing')
class Framing(unittest.TestCase):
    def pair(self):
        a,b=socket.socketpair()
        connection=ipc_transport.Connection(NativeConnection(a.detach()))
        self.addCleanup(connection.close);self.addCleanup(b.close)
        return connection,b

    def test_partial_header_has_whole_frame_deadline(self):
        connection,peer=self.pair();peer.sendall(b'\0')
        start=time.monotonic()
        with self.assertRaises(TimeoutError):connection.recv_bytes(1024,timeout=.06)
        self.assertLess(time.monotonic()-start,.5)

    def test_partial_body_has_whole_frame_deadline(self):
        connection,peer=self.pair();peer.sendall(struct.pack('!i',100)+b'x')
        with self.assertRaises(TimeoutError):connection.recv_bytes(1024,timeout=.06)

    def test_declared_oversized_or_invalid_frame_refused_before_body(self):
        for header in (struct.pack('!i',1025),struct.pack('!i',-2),struct.pack('!i',-1)+struct.pack('!Q',2**63)):
            connection,peer=self.pair();peer.sendall(header)
            with self.assertRaises(OSError):connection.recv_bytes(1024,timeout=.1)

    def test_compatible_byte_frames_and_empty_message(self):
        connection,peer=self.pair()
        peer.sendall(struct.pack('!i',0)+struct.pack('!i',3)+b'abc')
        self.assertEqual(connection.recv_bytes(),b'');self.assertEqual(connection.recv_bytes(),b'abc')
        connection.send_bytes(b'xxpayloadyy',2,7)
        self.assertEqual(peer.recv(11),struct.pack('!i',7)+b'payload')

    def test_slow_reader_cannot_block_send_forever(self):
        connection,peer=self.pair();connection.sock.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,4096)
        with self.assertRaises(TimeoutError):connection.send_bytes(bytes(2*1024*1024),timeout=.06)

    def test_partial_frame_is_cancelled_without_waiting_for_deadline(self):
        connection,peer=self.pair();peer.sendall(b'\0');connection.cancel=threading.Event()
        timer=threading.Timer(.03,connection.cancel.set);timer.start();began=time.monotonic()
        try:
            with self.assertRaises(InterruptedError):connection.recv_bytes(1024,timeout=3)
        finally:timer.join()
        self.assertLess(time.monotonic()-began,.5)


class PrivateFiles(unittest.TestCase):
    @unittest.skipIf(os.name=='nt','POSIX FIFO')
    def test_fifo_key_is_refused_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as folder:
            key=Path(folder)/'service.key';os.mkfifo(key);result=[]
            def read():
                try:ipc.read_key(folder)
                except ValueError:result.append('refused')
            worker=threading.Thread(target=read,daemon=True);worker.start();worker.join(.2)
            blocked=worker.is_alive()
            if blocked:
                descriptor=os.open(key,os.O_WRONLY|os.O_NONBLOCK);os.close(descriptor);worker.join(1)
            self.assertFalse(blocked);self.assertEqual(result,['refused'])

    @unittest.skipUnless(sys.platform=='darwin','macOS extended ACL')
    def test_profile_removes_extended_allow_acl_in_owned_fixture_only(self):
        with tempfile.TemporaryDirectory() as folder:
            profile=Path(folder)/'profile';profile.mkdir()
            subprocess.run(['/bin/chmod','+a','everyone allow read',str(profile)],check=True)
            self.assertGreater(len(subprocess.check_output(['/bin/ls','-lde',str(profile)]).splitlines()),1)
            service_paths.private_profile(profile)
            self.assertEqual(len(subprocess.check_output(['/bin/ls','-lde',str(profile)]).splitlines()),1)

    @unittest.skipIf(os.name=='nt','POSIX permissions')
    def test_profile_is_private_and_link_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);profile=root/'profile';profile.mkdir(mode=0o755)
            service_paths.private_profile(profile)
            self.assertEqual(stat.S_IMODE(profile.stat().st_mode),0o700)
            link=root/'link';link.symlink_to(profile,target_is_directory=True)
            with self.assertRaises(OSError):service_paths.private_profile(link)

    def test_key_has_fixed_length_and_single_name(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'service.key';path.write_bytes(bytes(32));path.chmod(0o600)
            self.assertEqual(ipc.read_key(folder),bytes(32))
            path.write_bytes(bytes(33))
            with self.assertRaises(ValueError):ipc.read_key(folder)
            path.write_bytes(bytes(32))
            try:os.link(path,Path(folder)/'second-name')
            except OSError:self.skipTest('hard links unavailable')
            with self.assertRaises(ValueError):ipc.read_key(folder)

    @unittest.skipIf(os.name=='nt','POSIX permissions')
    def test_public_or_symlinked_key_is_refused_without_reading_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'service.key';path.write_bytes(bytes(32));path.chmod(0o644)
            with self.assertRaises(ValueError):ipc.read_key(folder)
            path.unlink();victim=root/'victim';victim.write_bytes(bytes(32));path.symlink_to(victim)
            with self.assertRaises(OSError):ipc.read_key(folder)
            self.assertEqual(victim.read_bytes(),bytes(32))

    def test_rtmp_key_in_first_path_segment_never_reaches_journal(self):
        canary='FIRST_SEGMENT_CANARY_83745'
        for scheme in ('rtmp','rtmps'):
            text=error_log.scrub('failed '+scheme+'://user:pass@example.test/'+canary)
            self.assertNotIn(canary,text);self.assertNotIn('user:pass',text)
            self.assertEqual(text,'failed '+scheme+'://example.test')


class UntrustedParsers(unittest.TestCase):
    def test_malformed_graphene_shapes_are_controlled_errors(self):
        bad=[None,[],{}, {'results':[None]}, {'results':[{'series':None}]},
             {'results':[{'series':[0]}]}, {'results':[{'series':[{'columns':[{}]}]}]},
             {'results':[{'series':[{'columns':['time','rx'],'values':[None]}]}]}]
        for data in bad:
            with self.subTest(data=data),self.assertRaises(graphene_port.SourceError):graphene_port.decode(data,0,100)

    def test_graphene_rejects_nonfinite_and_huge_numbers_without_crashing(self):
        points=[[True,1],[10**1000,1],[1000,10**1000],[2000,float('nan')],[3000,1]]
        series=graphene_port.decode({'results':[{'series':[{'columns':['time','rx'],'values':points}]}]},0,10)
        self.assertEqual(series['rx'],[(1.,None),(2.,None),(3.,1)])

    def test_mdns_pointer_cycles_reserved_labels_and_lengths_are_refused(self):
        for raw in (b'\xc0\x00',b'\x40'+b'x'*64+b'\0',b'\x03x',b'\x3f'+b'x'*63+b'\0'):
            if raw.startswith(b'\x3f'):continue
            with self.assertRaises((ValueError,struct.error)):stream_sources._name(raw,0)
        header=struct.pack('!6H',0,0x8400,0,1,0,0)
        malformed=header+b'\0'+struct.pack('!HHIH',12,1,60,1)+b'\x03abc\0'
        with self.assertRaises(ValueError):stream_sources.parse_mdns(malformed)

    def test_xml_dtd_and_entities_refused_before_expansion(self):
        for xml in ('<!DOCTYPE vmix [<!ENTITY a "CANARY">]><vmix><version>&a;</version></vmix>',
                    '<!DOCTYPE vmix SYSTEM "file:///does-not-exist"><vmix/>'):
            with self.assertRaises(stream_sources.SourceError):stream_sources.parse_vmix_xml(xml)
        self.assertEqual(stream_sources.parse_vmix_xml('<vmix><version>26</version></vmix>')['version'],'26')

    def test_invalid_websocket_control_and_fragment_frames_are_refused(self):
        invalid=[b'\x89\x7e\x00\x7e',b'\x09\x00',b'\xc1\x00',b'\x81\x80',b'\x80\x00',b'\x82\x00']
        for raw in invalid:
            ws=stream_sources.WebSocket.__new__(stream_sources.WebSocket);ws.buffer=raw;ws.deadline=time.monotonic()+1
            with self.subTest(raw=raw),self.assertRaises(stream_sources.SourceError):ws.receive()
        ws=stream_sources.WebSocket.__new__(stream_sources.WebSocket);ws.buffer=b'\x01\x01a\x80\x01b';ws.deadline=time.monotonic()+1
        self.assertEqual(ws.receive(),'ab')

    def test_deterministic_binary_mutations_do_not_escape_parsers(self):
        rng=random.Random(20261021)
        for i in range(10000):
            raw=rng.randbytes(i%512)
            self.assertEqual(len(traffic_capture.decode(raw)),4)
            network_info.parse_lldp(raw)
            interface_probes.match_reply(raw,'192.0.2.1',11,12)
            interface_probes.match_reply(raw,'2001:db8::1',11,12)
            try:stream_sources.parse_mdns(raw)
            except (ValueError,struct.error):pass


@unittest.skipIf(os.name=='nt','AF_UNIX attack fixture')
class IPCIntegration(unittest.TestCase):
    def test_partial_unauthenticated_peers_expire_and_json_is_not_pickle(self):
        with tempfile.TemporaryDirectory(prefix='lr-sec-') as folder, \
             patch.object(server.Monitor,'session_workers',return_value=[]), \
             patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'),patch.object(ipc,'AUTH_SECONDS',.15):
            stop=threading.Event();thread=threading.Thread(target=ipc.run,args=(folder,stop));thread.start();peers=[]
            try:
                end=time.monotonic()+3
                while not ipc.connect(folder):
                    if time.monotonic()>end:self.fail('engine startup timeout')
                    time.sleep(.02)
                where,_=ipc.address(folder)
                for _ in range(8):
                    peer=socket.socket(socket.AF_UNIX);peer.settimeout(1);peer.connect(where)
                    header=peer.recv(4)
                    size=struct.unpack('!i',header)[0];challenge=bytearray()
                    while len(challenge)<size:challenge.extend(peer.recv(size-len(challenge)))
                    peer.sendall(b'\0');peers.append(peer)
                time.sleep(.4)
                self.assertTrue(ipc.current(ipc.request(folder,'status')))
                for body in (b'[]',b'null',b'"string"',b'\x80\x04N.',b'['*1500+b']'*1500):
                    with ipc.client(folder) as connection:
                        connection.send_bytes(body)
                        # A malformed request is closed without executing commands.
                        with self.assertRaises((EOFError,OSError)):connection.recv_bytes(timeout=1)
                self.assertTrue(ipc.current(ipc.request(folder,'status')))
                with self.assertRaises(RuntimeError):ipc.request(folder,'execute',path='/does-not-exist')
            finally:
                for peer in peers:peer.close()
                stop.set();thread.join(8);self.assertFalse(thread.is_alive())


class Distribution(unittest.TestCase):
    def test_fixed_certifi_bundle_excludes_compromised_emsign_root(self):
        import certifi
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.load_verify_locations(certifi.where())
        self.assertFalse(any('emSign ECC Root CA - G3' in str(c['subject']) for c in context.get_ca_certs()))

    def test_speed_test_does_not_follow_untrusted_redirects(self):
        import speed_test
        import urllib.request
        response=Mock(status_code=302)
        session=Mock();session.request.return_value=response
        tester=speed_test.SpeedTest();tester.local.session=session
        with self.assertRaises(ValueError):tester.open(urllib.request.Request(speed_test.CONFIG_URL))
        self.assertFalse(session.request.call_args.kwargs['allow_redirects']);response.close.assert_called_once()

    def test_certifi_wheel_record_is_checked_before_replacing_existing_bundle(self):
        import importlib.util,zipfile
        sys.path.insert(0,str(ROOT/'packaging'))
        try:
            spec=importlib.util.spec_from_file_location('security_windows_builder',ROOT/'packaging/build_windows_zip.py')
            builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
        finally:sys.path.pop(0)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);packages=root/'packages';(packages/'certifi').mkdir(parents=True)
            existing=packages/'certifi/cacert.pem';existing.write_text('existing')
            wheel=root/'corrupt.whl'
            with zipfile.ZipFile(wheel,'w') as z:
                info='certifi-2026.10.19.dist-info'
                z.writestr(info+'/METADATA','Name: certifi\nVersion: 2026.10.19\n')
                z.writestr(info+'/WHEEL','Root-Is-Purelib: true\nTag: py3-none-any\n')
                z.writestr(info+'/RECORD','')
                z.writestr('certifi/cacert.pem','unverified')
            with self.assertRaises(ValueError):builder.replace_certifi(packages,wheel)
            self.assertEqual(existing.read_text(encoding='utf-8'),'existing')

    def test_certifi_is_fixed_and_transitive_dependencies_pinned(self):
        text=(ROOT/'requirements-desktop.txt').read_text(encoding='utf-8')
        for requirement in ('certifi==2026.10.19','charset-normalizer==3.5.2','idna==3.20','shiboken6==6.12.0'):
            self.assertIn(requirement,text)

    def test_windows_authorization_freezes_script_and_acl_is_exact(self):
        guard=(ROOT/'packaging/windows-service-safety.ps1').read_text(encoding='utf-8')
        self.assertIn('SetAccessRuleProtection($true,$false)',guard)
        self.assertIn('ReparsePoint',guard)
        for name in ('install-background-windows.ps1','remove-background-windows.ps1'):
            text=(ROOT/'packaging'/name).read_text(encoding='utf-8')
            self.assertIn('-EncodedCommand',text);self.assertNotIn('-File "',text)
            self.assertNotIn('cmd.exe /d /c',text);self.assertNotIn('$env:ProgramFiles',text)
            elevated=text.split('if (-not $Elevated) {',1)[0]+text.split('\n# Elevated payload begins here.',1)[1]
            import gzip,base64
            packed=base64.b64encode(gzip.compress((guard+'\n& {'+elevated+'\n} -Elevated').encode('utf-16le')))
            self.assertLess((len(packed)+1000)*2*4//3,30000)
            self.assertIn('[IO.Compression.GZipStream]',text)
            self.assertIn('$env:PSModulePath=[IO.Path]::Combine([Environment]::SystemDirectory',text)
            self.assertIn('$ast.ParamBlock.Extent.Text',text)
        install=(ROOT/'packaging/install-background-windows.ps1').read_text(encoding='utf-8')
        self.assertIn('Get-PackageDigest $stage',install)
        self.assertIn('Copy-ServiceTree $source (Join-Path $stage $part)',install)
        self.assertIn('[IO.FileMode]::CreateNew',guard)
        self.assertIn('New-PrivateServiceSecurity $false',guard)
        self.assertIn('Protect-ServiceDirectory $stage -Private',install)

    def test_mac_root_configuration_has_no_user_plist_argument_or_user_log_open(self):
        text=(ROOT/'packaging/configure-background-mac.sh').read_text(encoding='utf-8')
        self.assertIn('test "$#" = 2',text);self.assertIn('UserName -string "$task_user"',text)
        self.assertIn('StandardOutPath -string /dev/null',text)
        self.assertIn('StandardErrorPath -string /dev/null',text)
        wrapper=(ROOT/'packaging/install-background-mac.command').read_text(encoding='utf-8')
        self.assertIn('task_payload=$(/bin/cat "$task_configure")',wrapper)
        self.assertNotIn('sudo /bin/sh "$task_configure"',wrapper)
        self.assertNotIn('quoted form of item 1 of argv & " " & quoted form of item 2',wrapper)


class SlowHTTP(unittest.TestCase):
    def exercise(self,headers):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        stop=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                try:
                    if headers:
                        raw=b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}'
                        for byte in raw:
                            self.connection.sendall(bytes([byte]))
                            if stop.wait(.03):break
                    else:
                        self.send_response(200);self.send_header('Content-Length','100');self.end_headers()
                        for _ in range(100):
                            self.wfile.write(b' ');self.wfile.flush()
                            if stop.wait(.03):break
                except OSError:pass
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=http.serve_forever,daemon=True);worker.start()
        began=time.monotonic()
        try:
            with requests.Session() as client:
                client.mount('http://',bound_http.adapter(None))
                if headers:
                    with self.assertRaises((TimeoutError,requests.RequestException)):
                        client.get('http://127.0.0.1:'+str(http.server_port),timeout=(.1,.15),stream=True)
                else:
                    response=client.get('http://127.0.0.1:'+str(http.server_port),timeout=(.1,.15),stream=True)
                    with self.assertRaises(TimeoutError):graphene_port.read_json(response,seconds=.15)
            self.assertLess(time.monotonic()-began,1)
            with bound_http._condition:self.assertFalse(bound_http._active)
        finally:stop.set();http.shutdown();http.server_close();worker.join(2)

    def test_dripping_headers_have_overall_deadline(self):self.exercise(True)
    def test_dripping_body_has_overall_deadline_even_after_http10_detach(self):self.exercise(False)


if __name__=='__main__':unittest.main()

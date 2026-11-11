"""Synthetic discovery regressions; never open a user's network adapter."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import struct
import ctypes as C
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from linkradar.network import network_info as n
from test_network_info import frame


def cdp_tlv(kind,value):
    return struct.pack('!HH',kind,len(value)+4)+value


def cdp_frame(tags=0,ttl=180,extra=b''):
    payload=bytes((2,ttl,0,0))+cdp_tlv(1,b'switch.example.test')+cdp_tlv(3,b'Gi1/0/7')+cdp_tlv(10,b'\x00\x64')+extra
    # Fixtures carry a correct one's-complement checksum, including odd length.
    padded=payload+b'\0'*(len(payload)%2)
    total=sum(struct.unpack('!'+str(len(padded)//2)+'H',padded))
    while total>>16:total=(total&0xffff)+(total>>16)
    payload=payload[:2]+struct.pack('!H',(~total)&0xffff)+payload[4:]
    payload=bytes.fromhex('aaaa0300000c2000')+payload
    ethernet=bytes.fromhex('01000ccccccc001122334455')
    for _ in range(tags):ethernet+=bytes.fromhex('81000064')
    return ethernet+struct.pack('!H',len(payload))+payload


class DiscoveryPackets(unittest.TestCase):
    def test_cdp_and_tagged_cdp_keep_protocol_and_native_vlan(self):
        for tags in range(3):
            with self.subTest(tags=tags):
                neighbor=n.parse_cdp(cdp_frame(tags)+b'\0'*20)
                self.assertEqual((neighbor['switch'],neighbor['port'],neighbor['vlan'],neighbor['protocol']),('switch.example.test','Gi1/0/7',100,'CDP'))
                self.assertEqual(neighbor['ttl'],180)
        self.assertEqual(n.parse_cdp(cdp_frame(ttl=0))['ttl'],0)
        self.assertIsNone(n.parse_cdp(frame()))

    def test_cdp_rejects_truncation_wrong_snap_invalid_tlvs_and_duplicates(self):
        raw=cdp_frame()
        for size in range(len(raw)):self.assertIsNone(n.parse_cdp(raw[:size]),size)
        for extra in (b'\0',struct.pack('!HH',5,0),struct.pack('!HH',5,65535),cdp_tlv(1,b'duplicate'),cdp_tlv(10,b'bad')):
            self.assertIsNone(n.parse_cdp(cdp_frame(extra=extra)),repr(extra))
        broken=bytearray(raw);broken[20]=0;self.assertIsNone(n.parse_cdp(broken))
        self.assertEqual(n.parse_cdp(cdp_frame(extra=cdp_tlv(999,b'future')))['port'],'Gi1/0/7')

    def test_reader_uses_promiscuous_but_filters_discovery_only(self):
        with patch.object(n.PcapCapture,'__init__',return_value=None) as init:
            n.LldpCapture('en7')
        self.assertTrue(init.call_args.kwargs['promiscuous'])
        self.assertIn(b'ether dst 01:00:0c:cc:cc:cc',init.call_args.args[2])

    def test_reader_skips_unrelated_cisco_multicast_before_valid_announcement(self):
        reader=object.__new__(n.LldpCapture)
        packets=[{'raw':b'other'}, {'raw':cdp_frame()},None]
        reader.read_packet=Mock(side_effect=packets)
        self.assertEqual(reader.poll()['protocol'],'CDP')
        self.assertIsNone(reader.poll())
        reader.read_packet=Mock(return_value={'raw':b'other'})
        self.assertIsNone(reader.poll());self.assertEqual(reader.read_packet.call_count,64)

    @unittest.skipUnless(sys.platform=='darwin','macOS libpcap offline filter check')
    def test_real_bpf_filter_accepts_both_protocols_including_vlan_and_excludes_ip(self):
        with patch.object(n.PcapCapture,'__init__',return_value=None) as init:n.LldpCapture('unused')
        lib=C.CDLL('/usr/lib/libpcap.A.dylib')
        lib.pcap_open_dead.argtypes=[C.c_int,C.c_int];lib.pcap_open_dead.restype=C.c_void_p
        lib.pcap_compile.argtypes=[C.c_void_p,C.POINTER(n.BpfProgram),C.c_char_p,C.c_int,C.c_uint]
        lib.pcap_offline_filter.argtypes=[C.POINTER(n.BpfProgram),C.POINTER(n.PacketHeader),C.c_void_p]
        lib.pcap_freecode.argtypes=[C.POINTER(n.BpfProgram)];lib.pcap_close.argtypes=[C.c_void_p]
        handle=lib.pcap_open_dead(1,2048);self.assertTrue(handle)
        program=n.BpfProgram()
        try:
            self.assertEqual(lib.pcap_compile(handle,C.byref(program),init.call_args.args[2],1,0xffffffff),0)
            try:
                for raw,expected in [(frame(tags=tags),True) for tags in range(3)]+[(cdp_frame(tags=tags),True) for tags in range(3)]+[(bytes.fromhex('001122334455aabbccddeeff0800')+bytes(60),False)]:
                    header=n.PacketHeader();header.caplen=len(raw);header.length=len(raw)
                    self.assertEqual(bool(lib.pcap_offline_filter(C.byref(program),C.byref(header),C.create_string_buffer(raw))),expected)
            finally:lib.pcap_freecode(C.byref(program))
        finally:lib.pcap_close(handle)


class FakeStop:
    def __init__(self,clock,limit):self.clock=clock;self.limit=limit
    def is_set(self):return self.clock[0]>=self.limit
    def wait(self,seconds):self.clock[0]+=seconds


class DiscoveryLifecycle(unittest.TestCase):
    def exercise(self,limit,interfaces,publish=None,packet=None):
        clock=[0.];readers=[];states=[];restart=threading.Event()
        def factory(interface):
            reader=Mock();reader.poll.return_value=None
            if packet and not readers:reader.poll.side_effect=[packet,None]+[None]*500
            reader.interface=interface;readers.append(reader);return reader
        def emit(state):
            states.append((clock[0],state))
            if publish:publish(clock[0],state,restart)
        with patch.object(n.time,'monotonic',side_effect=lambda:clock[0]),patch.object(n,'local_interfaces',side_effect=lambda:interfaces(clock[0])),patch.object(n,'LldpCapture',side_effect=factory):
            n.discover(FakeStop(clock,limit),lambda:'en7',emit,restart)
        for reader in readers:reader.close.assert_called_once()
        return readers,states

    @staticmethod
    def row(capture='en7'):return {'id':'en7','capture':capture,'mac':'00:11:22:33:44:55'}

    def test_explicit_refresh_reopens_same_interface_and_discards_cached_neighbor(self):
        def publish(now,state,restart):
            if now==0:restart.set()
        readers,states=self.exercise(2,lambda now:[self.row()],publish,n.parse_cdp(cdp_frame()))
        self.assertEqual(len(readers),2)
        self.assertEqual(states[0][1]['lldp_status'],'CDP получен')
        self.assertIsNone(states[1][1]['neighbor'])

    def test_disconnect_return_and_changed_capture_reopen_without_adapter_fallback(self):
        def interfaces(now):
            wifi={'id':'en0','capture':'en0','default':True}
            return [wifi]+([] if 10<=now<20 else [self.row('usb-new' if now>=30 else 'en7')])
        readers,states=self.exercise(31,interfaces,packet=n.parse_cdp(cdp_frame()))
        self.assertEqual([r.interface for r in readers],['en7','en7','usb-new'])
        absent=[state for now,state in states if 10<=now<20]
        self.assertTrue(absent)
        self.assertTrue(all(not state['local'] and state['neighbor'] is None for state in absent))

    def test_silent_capture_reopens_without_accusing_switch_or_cable(self):
        readers,states=self.exercise(67,lambda now:[self.row()])
        self.assertEqual(len(readers),2)
        self.assertIn('пока не получен',states[-1][1]['lldp_status'])

    def test_announcement_expires_by_its_ttl(self):
        readers,states=self.exercise(3,lambda now:[self.row()],packet=n.parse_cdp(cdp_frame(ttl=1)))
        self.assertIsNotNone(states[0][1]['neighbor'])
        self.assertTrue(all(state['neighbor'] is None for now,state in states if now>=1))


    def test_lldp_preferred_until_expiry_then_cdp_fallback(self):
        clock=[0.];reader=Mock();reader.poll.side_effect=[n.parse_lldp(frame(ttl=1)),n.parse_cdp(cdp_frame()),None]+[None]*20
        states=[]
        with patch.object(n.time,'monotonic',side_effect=lambda:clock[0]),patch.object(n,'local_interfaces',return_value=[self.row()]),patch.object(n,'LldpCapture',return_value=reader):
            n.discover(FakeStop(clock,2),lambda:'en7',lambda state:states.append((clock[0],state)))
        self.assertEqual(states[0][1]['neighbor']['switch'],'Switch-stage')
        self.assertEqual(states[-1][1]['lldp_status'],'CDP получен')
        reader.close.assert_called_once()


class DiscoveryControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_support import production_application
        cls.app=production_application(cls)

    def test_refresh_requests_only_discovery_and_compact_window_fits(self):
        from linkradar.ui import desktop
        from qt_support import managed_window,settle_layout
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),managed_window(desktop.Window(Path(folder),service_folder=False)) as window:
            for width,height in ((900,640),(1100,760)):
                window.resize(width,height);window.show();settle_layout(self.app)
                self.assertEqual(window.width(),width)
                self.assertEqual(window.height(),height)
                self.assertTrue(window.discovery_refresh.isVisible())
            with patch.object(window.discovery,'isRunning',return_value=True),patch.object(window.traffic_panel,'start') as capture_start:
                window.discovery_refresh.click()
                self.assertTrue(window.discovery.restart.is_set())
                capture_start.assert_not_called()
            self.assertIn('LLDP/CDP',window.lldp_status.text())


if __name__=='__main__':unittest.main()

import sys, unittest,struct
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import network_info as n

def tlv(kind,value):return struct.pack('!H',kind<<9|len(value))+value

def frame(extra=b'',tags=0,ttl=120):
    ethernet=bytes.fromhex('0180c200000e001122334455')
    for i in range(tags):ethernet+=b'\x81\x00\x00\x64'
    ethernet+=b'\x88\xcc'
    return ethernet+tlv(1,b'\x04'+bytes.fromhex('001122334455'))+tlv(2,b'\x05Gi1/0/7')+tlv(3,struct.pack('!H',ttl))+tlv(5,b'Switch-stage')+extra+tlv(0,b'')

class NetworkInfoTests(unittest.TestCase):
    def test_redacted_mac_is_not_displayed_as_a_real_device_address(self):
        row={'id':'en0','mac':'02:00:00:00:00:00','dns':[]}
        n.enrich_macos([row],'Hardware Port: Wi-Fi\nDevice: en0\nEthernet Address: 50:11:22:33:44:55\n','')
        self.assertEqual(row['mac'],'50:11:22:33:44:55');self.assertIn('Аппаратный',row['mac_source'])
        row={'id':'en0','mac':'02:00:00:00:00:00','dns':[]}
        n.enrich_macos([row],'','');self.assertIsNone(row['mac'])
    def test_real_tlv_values_and_vlan_without_guessing(self):
        r=n.parse_lldp(frame());self.assertEqual(r['switch'],'Switch-stage');self.assertEqual(r['port'],'Gi1/0/7');self.assertIsNone(r['vlan'])
        extra=tlv(127,b'\x00\x80\xc2\x01\x00\x64')
        for tags in range(3):self.assertEqual(n.parse_lldp(frame(extra,tags))['vlan'],100)
    def test_truncated_and_non_lldp_do_not_produce_neighbor(self):
        raw=frame()
        for size in range(len(raw)):self.assertIsNone(n.parse_lldp(raw[:size]))
        self.assertIsNone(n.parse_lldp(bytes(100)))
    def test_shutdown_and_mandatory_tlvs(self):
        self.assertEqual(n.parse_lldp(frame(ttl=0))['ttl'],0)
        self.assertIsNone(n.parse_lldp(bytes.fromhex('0180c200000e00112233445588cc')+tlv(5,b'fake')+tlv(0,b'')))
    def test_default_interface_effective_mac_mask_and_scoped_dns(self):
        route='gateway: 10.2.3.1\ninterface: en4'
        interfaces='en4: flags=8863<UP,BROADCAST,RUNNING>\n ether aa:bb:cc:dd:ee:ff\n inet 10.2.3.5 netmask 0xffffff00\n status: active\nen0: flags=8863<UP,BROADCAST>\n ether 00:11:22:33:44:55\n status: inactive\n'
        dns='resolver #1\n nameserver[0] : 10.2.3.1\nresolver #2\n domain : corp.example\n nameserver[0] : 10.7.7.7\n'
        rows=n.parse_macos(route,interfaces,dns);self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['mask'],'255.255.255.0');self.assertEqual(rows[0]['dns'],['10.2.3.1']);self.assertEqual(rows[0]['mac'],'aa:bb:cc:dd:ee:ff')

if __name__=='__main__':unittest.main()

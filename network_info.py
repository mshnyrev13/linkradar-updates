"""Local interface configuration and passive LLDP. No Wireshark dependency."""
import ctypes as C
import ipaddress
import json
import os
import platform
import re
import subprocess
import threading
import time
from pathlib import Path
import error_log


def command(args, timeout=6):
    kw = {'creationflags': subprocess.CREATE_NO_WINDOW} if platform.system() == 'Windows' else {}
    p = subprocess.run(args, capture_output=True, timeout=timeout, **kw)
    if p.returncode:
        raise OSError('Не удалось прочитать сетевую конфигурацию')
    return p.stdout.decode('utf-8-sig', errors='replace')


def mask(prefix):
    return str(ipaddress.IPv4Network('0.0.0.0/'+str(prefix)).netmask)


def parse_macos(route, interfaces, dns):
    interface = re.search(r'interface:\s*(\S+)', route)
    gateway = re.search(r'gateway:\s*(\S+)', route)
    default = interface.group(1) if interface else None
    dns_blocks=re.split(r'(?=^resolver #\d+)',dns,flags=re.M)
    default_dns=next((b for b in dns_blocks if 'nameserver[' in b and not re.search(r'^\s*(?:domain\s*:|if_index\s*:)',b,re.M)), '')
    dns_addresses = list(dict.fromkeys(re.findall(r'nameserver\[\d+\]\s*:\s*(\S+)', default_dns)))
    rows = []
    for block in re.split(r'(?=^\S+: flags=)', interfaces, flags=re.M):
        m = re.match(r'(\S+): flags=.*?<([^>]+)>', block)
        if not m or 'UP' not in m.group(2).split(',') or 'LOOPBACK' in m.group(2):
            continue
        name = m.group(1)
        if 'status: inactive' in block:
            continue
        addr = re.search(r'\binet (\S+) netmask (\S+)', block)
        mac = re.search(r'\bether ([0-9a-f:]+)', block, re.I)
        if not addr and not mac:
            continue
        netmask = None
        if addr:
            value = addr.group(2)
            netmask = str(ipaddress.IPv4Address(int(value, 16))) if value.startswith('0x') else value
        rows.append({'id':name, 'name':name, 'capture':name, 'mac':mac.group(1) if mac else None,
                     'ip':addr.group(1) if addr else None, 'mask':netmask,
                     'gateway':gateway.group(1) if gateway and name == default else None,
                     'dns':dns_addresses if name == default else [], 'default':name == default})
    return rows


def enrich_macos(rows, hardware, dns):
    """Use scoped DNS and label a hardware MAC fallback when macOS hides the current one."""
    ports={}
    for block in hardware.split('Hardware Port: ')[1:]:
        dev=re.search(r'^Device:\s*(\S+)',block,re.M)
        addr=re.search(r'^Ethernet Address:\s*([0-9a-f:]+)',block,re.M|re.I)
        if dev:ports[dev.group(1)]=(block.splitlines()[0],addr.group(1) if addr else None)
    for row in rows:
        row['mac_source']='Текущий MAC интерфейса'
        if row['id'] in ports:
            label,hardware_mac=ports[row['id']];row['name']=label+' ('+row['id']+')'
            if row['mac'] in ('02:00:00:00:00:00','00:00:00:00:00:00',None):
                row['mac']=hardware_mac if hardware_mac not in ('02:00:00:00:00:00','00:00:00:00:00:00') else None
                row['mac_source']='Аппаратный MAC адаптера: macOS скрывает текущий адрес' if row['mac'] else 'macOS не разрешила прочитать MAC'
        elif row['mac'] in ('02:00:00:00:00:00','00:00:00:00:00:00'):
            row['mac']=None;row['mac_source']='macOS не разрешила прочитать MAC'
        scoped=[]
        for block in re.split(r'(?=^resolver #\d+)',dns,flags=re.M):
            if re.search(r'if_index\s*:\s*\d+\s*\('+re.escape(row['id'])+r'\)',block) and not re.search(r'^\s*domain\s*:',block,re.M):
                scoped+=re.findall(r'nameserver\[\d+\]\s*:\s*(\S+)',block)
        if scoped:row['dns']=list(dict.fromkeys(scoped))
    return rows


WINDOWS_SCRIPT = r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$routes=@(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | ForEach-Object {
 $r=$_; $i=Get-NetIPInterface -InterfaceIndex $r.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
 [pscustomobject]@{Index=$r.InterfaceIndex; Cost=($r.RouteMetric+$i.InterfaceMetric); Gateway=$r.NextHop}
} | Sort-Object Cost)
$best=$routes | Select-Object -First 1
$rows=@(Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | ForEach-Object {
 $a=$_; $c=Get-NetIPConfiguration -InterfaceIndex $a.ifIndex
 $v4=@($c.IPv4Address); $gateway=@($c.IPv4DefaultGateway)
 [pscustomobject]@{id=[string]$a.ifIndex; name=$a.Name; capture=('\Device\NPF_{'+([string]$a.InterfaceGuid).Trim('{}')+'}');
 mac=$a.MacAddress; ip=if($v4.Count){$v4[0].IPAddress}else{$null};
 prefix=if($v4.Count){$v4[0].PrefixLength}else{$null};
 gateway=if($gateway.Count){$gateway[0].NextHop}else{$null};
 dns=@($c.DNSServer.ServerAddresses); default=($a.ifIndex -eq $best.Index)}
})
ConvertTo-Json -InputObject $rows -Depth 5 -Compress
'''


def local_interfaces():
    if platform.system() == 'Darwin':
        try:
            route = command(['/sbin/route','-n','get','default'])
        except OSError:
            route = ''
        dns=command(['/usr/sbin/scutil','--dns'])
        rows=parse_macos(route,command(['/sbin/ifconfig','-a']),dns)
        try:hardware=command(['/usr/sbin/networksetup','-listallhardwareports'])
        except OSError:hardware=''
        enrich_macos(rows,hardware,dns)
        for row in rows:
            if row['default'] or not row['ip']:continue
            try:
                scoped=command(['/sbin/route','-n','get','-ifscope',row['id'],'default'])
                gateway=re.search(r'gateway:\s*(\S+)',scoped)
                if gateway:
                    try:row['gateway']=str(ipaddress.IPv4Address(gateway.group(1)))
                    except ValueError:pass
            except OSError:pass
        return rows
    if platform.system() == 'Windows':
        shell = str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe')
        result = json.loads(command([shell,'-NoProfile','-NonInteractive','-Command',WINDOWS_SCRIPT]))
        rows = result if isinstance(result,list) else [result]
        for row in rows:
            row['mask'] = mask(row.pop('prefix')) if row.get('prefix') is not None else None
            row['mac'] = (row.get('mac') or '').replace('-',':').lower() or None
        return rows
    raise OSError('Сбор конфигурации поддерживается на macOS и Windows')


def parse_lldp(frame):
    """Parse Ethernet LLDP, including tagged frames and IEEE 802.1 Port VLAN ID."""
    if len(frame) < 14:
        return None
    ether_type = int.from_bytes(frame[12:14],'big'); offset=14
    while ether_type in (0x8100,0x88a8,0x9100):
        if len(frame) < offset+4:
            return None
        ether_type=int.from_bytes(frame[offset+2:offset+4],'big');offset+=4
    if ether_type != 0x88cc:
        return None
    result={'switch':None,'port':None,'vlan':None,'ttl':None,'chassis':None,'port_description':None,'vlan_names':[]}
    types=[]; ended=False
    while offset+2 <= len(frame):
        header=int.from_bytes(frame[offset:offset+2],'big');offset+=2
        kind,size=header>>9,header&511
        if offset+size > len(frame):return None
        value=frame[offset:offset+size];offset+=size
        if kind == 0:
            if size:return None
            ended=True;break
        types.append(kind)
        if kind in (1,2) and len(value)>=2:
            subtype=value[0];raw=value[1:]
            text=':'.join(f'{b:02x}' for b in raw) if (kind==1 and subtype==4 or kind==2 and subtype==3) else raw.decode('utf-8',errors='replace').strip('\x00')
            result['chassis' if kind==1 else 'port']=text
        elif kind==3 and size==2:result['ttl']=int.from_bytes(value,'big')
        elif kind==4:result['port_description']=value.decode('utf-8',errors='replace').strip('\x00')
        elif kind==5:result['switch']=value.decode('utf-8',errors='replace').strip('\x00')
        elif kind==127 and value[:3]==b'\x00\x80\xc2' and size>=6:
            if value[3]==1:
                pvid=int.from_bytes(value[4:6],'big')
                if 1<=pvid<=4094:result['vlan']=pvid
            elif value[3]==3 and size>=7 and 7+value[6]<=size:
                result['vlan_names'].append({'id':int.from_bytes(value[4:6],'big'),'name':value[7:7+value[6]].decode('utf-8',errors='replace')})
    if not ended or types[:3]!=[1,2,3] or result['ttl'] is None or not result['chassis'] or not result['port']:
        return None
    result['switch']=result['switch'] or result['chassis']
    return result


class TimeVal(C.Structure):
    _fields_=[('tv_sec',C.c_long),('tv_usec',C.c_long)]
class PacketHeader(C.Structure):
    _fields_=[('ts',TimeVal),('caplen',C.c_uint),('length',C.c_uint)]
class BpfProgram(C.Structure):
    _fields_=[('length',C.c_uint),('instructions',C.c_void_p)]


class CaptureStats(C.Structure):
    # Extra storage covers Npcap's additional counters as well as libpcap's ABI.
    _fields_=[('received',C.c_uint),('dropped',C.c_uint),('interface_dropped',C.c_uint),('extra',C.c_uint*5)]


class PcapCapture:
    def __init__(self, interface, snaplen=256, capture_filter=None, promiscuous=False):
        if platform.system() == 'Windows':
            root=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/Npcap'
            if not (root/'wpcap.dll').is_file():
                raise OSError('Для захвата нужен установленный Npcap; Python и Wireshark не нужны')
            self.dll_directory=os.add_dll_directory(str(root))
            lib=C.CDLL(str(root/'wpcap.dll'))
        else:
            lib=C.CDLL('/usr/lib/libpcap.A.dylib')
        self.lib=lib;self.handle=None
        lib.pcap_open_live.argtypes=[C.c_char_p,C.c_int,C.c_int,C.c_int,C.c_char_p];lib.pcap_open_live.restype=C.c_void_p
        lib.pcap_close.argtypes=[C.c_void_p]
        lib.pcap_datalink.argtypes=[C.c_void_p];lib.pcap_datalink.restype=C.c_int
        lib.pcap_geterr.argtypes=[C.c_void_p];lib.pcap_geterr.restype=C.c_char_p
        lib.pcap_setnonblock.argtypes=[C.c_void_p,C.c_int,C.c_char_p];lib.pcap_setnonblock.restype=C.c_int
        lib.pcap_compile.argtypes=[C.c_void_p,C.POINTER(BpfProgram),C.c_char_p,C.c_int,C.c_uint];lib.pcap_compile.restype=C.c_int
        lib.pcap_setfilter.argtypes=[C.c_void_p,C.POINTER(BpfProgram)];lib.pcap_setfilter.restype=C.c_int
        lib.pcap_freecode.argtypes=[C.POINTER(BpfProgram)]
        lib.pcap_next_ex.argtypes=[C.c_void_p,C.POINTER(C.POINTER(PacketHeader)),C.POINTER(C.POINTER(C.c_ubyte))];lib.pcap_next_ex.restype=C.c_int
        lib.pcap_stats.argtypes=[C.c_void_p,C.POINTER(CaptureStats)];lib.pcap_stats.restype=C.c_int
        err=C.create_string_buffer(256)
        self.snaplen=snaplen;self.handle=lib.pcap_open_live(interface.encode(),snaplen,int(promiscuous),100,err)
        if not self.handle:
            message=err.value.decode(errors='replace').lower()
            self.close()
            if any(w in message for w in ['permission','permitted','access','denied']):
                raise OSError('Нет прав BPF: откройте приложение из Applications и подтвердите настройку доступа; после установки может требоваться повторный вход в macOS' if platform.system()=='Darwin' else 'Нет прав захвата: проверьте разрешения Npcap')
            raise OSError('Захват недоступен на выбранном интерфейсе')
        try:
            if lib.pcap_datalink(self.handle)!=1:raise OSError('Нужен Ethernet-интерфейс с LLDP; VPN/loopback не поддерживаются')
            if capture_filter:
                program=BpfProgram()
                if lib.pcap_compile(self.handle,C.byref(program),capture_filter,1,0xffffffff)!=0:raise OSError('Не удалось настроить фильтр захвата')
                try:
                    if lib.pcap_setfilter(self.handle,C.byref(program))!=0:raise OSError('Не удалось включить фильтр захвата')
                finally:lib.pcap_freecode(C.byref(program))
            if lib.pcap_setnonblock(self.handle,1,err)!=0:raise OSError('Не удалось включить фоновый захват')
        except Exception:
            self.close();raise
    def read_packet(self):
        header=C.POINTER(PacketHeader)();packet=C.POINTER(C.c_ubyte)()
        rc=self.lib.pcap_next_ex(self.handle,C.byref(header),C.byref(packet))
        if rc<0:raise OSError('Захват прерван')
        if rc==1:
            value=header.contents
            return {'ts':value.ts.tv_sec+value.ts.tv_usec/1e6,'length':value.length,'raw':C.string_at(packet,min(value.caplen,self.snaplen))}
        return None
    def stats(self):
        result=CaptureStats()
        if self.lib.pcap_stats(self.handle,C.byref(result))!=0:return None
        return {'received':result.received,'dropped':result.dropped,'interface_dropped':result.interface_dropped}
    def close(self):
        if self.handle:self.lib.pcap_close(self.handle);self.handle=None
        if getattr(self,'dll_directory',None):self.dll_directory.close();self.dll_directory=None


class LldpCapture(PcapCapture):
    def __init__(self,interface):
        # Only LLDP payloads, including VLAN/QinQ, enter the discovery reader.
        filt=b'ether[12:2] = 0x88cc or ((ether[12:2] = 0x8100 or ether[12:2] = 0x88a8) and ether[16:2] = 0x88cc) or ((ether[12:2] = 0x8100 or ether[12:2] = 0x88a8) and (ether[16:2] = 0x8100 or ether[16:2] = 0x88a8) and ether[20:2] = 0x88cc)'
        super().__init__(interface,2048,filt)
    def poll(self):
        packet=self.read_packet()
        return parse_lldp(packet['raw']) if packet else None


def discover(stop, selected, publish):
    capture=None; interface_id=None; rows=[]; current={}; neighbor=None; expires=0; refresh_at=0;retry_at=0;since=0
    try:
        while not stop.is_set():
            now=time.monotonic()
            if now>=refresh_at:
                refresh_at=now+10
                try:rows=local_interfaces();error_log.resolved('network-config')
                except (OSError,ValueError,subprocess.SubprocessError) as exc:
                    error_log.error('Не удалось прочитать сетевую конфигурацию',exc,key='network-config')
                    rows=[]
                    if capture:capture.close();capture=None
                    interface_id=None;neighbor=None
                    publish({'interfaces':[], 'local':{}, 'neighbor':None,'lldp_status':'Сетевая конфигурация недоступна'})
                    stop.wait(.2);continue
            requested=selected()
            current=next((r for r in rows if r['id']==requested),None) if requested else next((r for r in rows if r.get('default')),rows[0] if rows else None)
            new_id=current['id'] if current else None
            if new_id!=interface_id:
                if capture:capture.close();capture=None
                interface_id=new_id;neighbor=None;retry_at=0;since=now;expires=0
            if current and not capture and now>=retry_at:
                retry_at=now+30
                try:capture=LldpCapture(current['capture']);status='Ожидаем LLDP от коммутатора…';since=now
                except OSError as exc:status=str(exc)
                except AttributeError:status='LLDP недоступен'
                except Exception as exc:status='LLDP недоступен';error_log.error('LLDP-захват не открылся',exc,key='lldp-open')
            if capture:
                try:
                    for _ in range(64):
                        packet=capture.poll()
                        if packet:
                            if packet['ttl']==0:neighbor=None;expires=0
                            else:neighbor=packet;expires=now+packet['ttl']
                        else:break
                    if neighbor and now>=expires:neighbor=None
                    status='LLDP получен' if neighbor else 'LLDP не получен: проверьте LLDP на коммутаторе и кабель' if now-since>=65 else 'Ожидаем LLDP от коммутатора…'
                except OSError as exc:
                    error_log.error('LLDP-захват прерван',exc,key='lldp-read')
                    capture.close();capture=None;neighbor=None;status='LLDP-захват прерван';retry_at=now+5
            if not current:status='Нет активного сетевого интерфейса'
            publish({'interfaces':rows,'local':current or {},'neighbor':neighbor,'lldp_status':status})
            stop.wait(.5)
    finally:
        if capture:capture.close()

"""Explicitly bound ICMP probes. No routing-table changes or default-path fallback."""
import ctypes as C
import ipaddress
import platform
import secrets
import dns_resolver
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path


class InterfaceUnavailable(OSError):
    pass


def validate_interface(value):
    if value is None or value == '':return None
    import re
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}',value):
        raise ValueError('Некорректный сетевой адаптер')
    return value


class Sockaddr(C.Structure):
    _fields_=[('length',C.c_ubyte),('family',C.c_ubyte),('data',C.c_ubyte*26)]

class Ifaddrs(C.Structure):
    pass
Ifaddrs._fields_=[('next',C.POINTER(Ifaddrs)),('name',C.c_char_p),('flags',C.c_uint),
                  ('address',C.POINTER(Sockaddr)),('mask',C.c_void_p),('destination',C.c_void_p),('data',C.c_void_p)]


def darwin_addresses(identity):
    libc=C.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
    libc.getifaddrs.argtypes=[C.POINTER(C.POINTER(Ifaddrs))];libc.getifaddrs.restype=C.c_int
    libc.freeifaddrs.argtypes=[C.POINTER(Ifaddrs)]
    first=C.POINTER(Ifaddrs)()
    if libc.getifaddrs(C.byref(first))!=0:raise InterfaceUnavailable('Не удалось прочитать адреса адаптера')
    result=[]
    try:
        item=first
        while item:
            row=item.contents
            if row.name.decode()==identity and row.flags&1 and row.flags&0x40 and row.address:
                addr=row.address.contents;raw=C.string_at(row.address,addr.length)
                if addr.family==socket.AF_INET and len(raw)>=8:result.append(socket.inet_ntop(socket.AF_INET,raw[4:8]))
                elif addr.family==socket.AF_INET6 and len(raw)>=28:result.append(socket.inet_ntop(socket.AF_INET6,raw[8:24]))
            item=row.next
    finally:libc.freeifaddrs(first)
    return result


class SocketAddress(C.Structure):
    _fields_=[('address',C.c_void_p),('length',C.c_int)]
class Unicast(C.Structure):pass
Unicast._fields_=[('alignment',C.c_uint64),('next',C.POINTER(Unicast)),('address',SocketAddress),('prefix_origin',C.c_int),('suffix_origin',C.c_int),('dad_state',C.c_int),('valid_lifetime',C.c_uint32),('preferred_lifetime',C.c_uint32),('lease_lifetime',C.c_uint32),('prefix_length',C.c_ubyte)]
class Adapter(C.Structure):pass
Adapter._fields_=[('length',C.c_uint32),('index',C.c_uint32),('next',C.POINTER(Adapter)),
 ('name',C.c_char_p),('unicast',C.POINTER(Unicast)),('anycast',C.c_void_p),('multicast',C.c_void_p),
 ('dns',C.c_void_p),('suffix',C.c_wchar_p),('description',C.c_wchar_p),('friendly',C.c_wchar_p),
 ('physical',C.c_ubyte*8),('physical_length',C.c_uint32),('flags',C.c_uint32),('mtu',C.c_uint32),
 ('type',C.c_uint32),('status',C.c_int),('ipv6_index',C.c_uint32)]


def windows_addresses(identity):
    if not identity.isdigit():raise InterfaceUnavailable('Не найден индекс выбранного адаптера Windows')
    api=C.WinDLL('iphlpapi',use_last_error=True)
    api.GetAdaptersAddresses.argtypes=[C.c_uint32,C.c_uint32,C.c_void_p,C.POINTER(Adapter),C.POINTER(C.c_uint32)]
    api.GetAdaptersAddresses.restype=C.c_uint32
    size=C.c_uint32(16384)
    for _ in range(3):
        buffer=C.create_string_buffer(size.value)
        first=C.cast(buffer,C.POINTER(Adapter));error=api.GetAdaptersAddresses(0,0,None,first,C.byref(size))
        if error==111:continue
        if error:raise InterfaceUnavailable('Не удалось прочитать адреса адаптера Windows: '+str(error))
        result=[];selected_index=int(identity);item=first
        while item:
            row=item.contents
            if int(identity) in (row.index,row.ipv6_index) and row.status==1:
                selected_index=row.ipv6_index or row.index
                address=row.unicast
                while address:
                    if address.contents.dad_state!=4:
                        address=address.contents.next;continue
                    a=address.contents.address;raw=C.string_at(a.address,a.length)
                    family=int.from_bytes(raw[:2],'little')
                    if family==2 and len(raw)>=8:result.append(socket.inet_ntop(socket.AF_INET,raw[4:8]))
                    elif family==23 and len(raw)>=28:result.append(socket.inet_ntop(socket.AF_INET6,raw[8:24]))
                    address=address.contents.next
            item=row.next
        return result,selected_index
    raise InterfaceUnavailable('Список адресов адаптера изменился во время чтения')


def binding_for(identity, destination):
    identity=validate_interface(identity)
    if identity is None:return None
    dest=ipaddress.ip_address(destination.split('%')[0]);system=platform.system()
    if system=='Darwin':addresses=darwin_addresses(identity);index=socket.if_nametoindex(identity)
    elif system=='Windows':addresses,index=windows_addresses(identity)
    else:raise InterfaceUnavailable('Привязка адаптера поддерживается в macOS и Windows')
    zone=destination.split('%',1)[1] if '%' in destination else None
    if zone and zone!=identity and (not zone.isdigit() or int(zone)!=index):
        raise InterfaceUnavailable('IPv6 scope цели не совпадает с выбранным адаптером '+identity)
    candidates=[ipaddress.ip_address(a) for a in addresses if ipaddress.ip_address(a).version==dest.version]
    if dest.version==6:
        candidates=[a for a in candidates if a.is_link_local==dest.is_link_local and not a.is_loopback]
    candidates=[a for a in candidates if not a.is_unspecified and not a.is_multicast]
    if dest.is_global:candidates.sort(key=lambda a:not a.is_global)
    if not candidates:raise InterfaceUnavailable('Выбранный адаптер '+identity+' недоступен или не имеет '+('IPv6' if dest.version==6 else 'IPv4'))
    return {'id':identity,'index':index,'source':str(candidates[0])}


def resolve_bound(host, family, identity):
    af={'ipv4':socket.AF_INET,'ipv6':socket.AF_INET6,'auto':socket.AF_UNSPEC}[family or 'ipv4']
    results=dns_resolver.getaddrinfo(host,None,af,socket.SOCK_STREAM)
    failure=None
    for item in results:
        ip=item[4][0]
        if len(item[4])==4 and item[4][3]:ip+='%'+str(item[4][3])
        try:binding_for(identity,ip);return ip
        except InterfaceUnavailable as exc:failure=exc
    raise failure or InterfaceUnavailable('Для выбранного адаптера нет подходящего адреса цели')

def same_ip(a,b):
    """A bound socket's peer need not repeat the destination's IPv6 zone suffix."""
    if not a or not b:return False
    try:return ipaddress.ip_address(a.split('%')[0])==ipaddress.ip_address(b.split('%')[0])
    except ValueError:return False


def checksum(data):
    if len(data)%2:data+=b'\0'
    total=sum(struct.unpack('!'+str(len(data)//2)+'H',data));total=(total>>16)+(total&0xffff);total+=(total>>16)
    return (~total)&0xffff


def match_reply(packet, destination, ident, sequence):
    """Match echo or a quoted echo in TTL/unreachable replies; reject unrelated ICMP."""
    version=ipaddress.ip_address(destination.split('%')[0]).version
    if version==4 and packet and packet[0]>>4==4:
        length=(packet[0]&15)*4
        if length<20 or len(packet)<length+8:return None
        packet=packet[length:]
    if len(packet)<8:return None
    kind,code,_,rid,seq=struct.unpack('!BBHHH',packet[:8])
    if kind==(0 if version==4 else 129) and code==0 and (rid,seq)==(ident,sequence):return 'echo'
    if kind not in ((3,11) if version==4 else (1,3)):return None
    if (kind==11 if version==4 else kind==3) and code!=0:return None
    quoted=packet[8:]
    if version==4:
        if len(quoted)<28 or quoted[0]>>4!=4 or quoted[9]!=socket.IPPROTO_ICMP:return None
        offset=(quoted[0]&15)*4
        if offset<20 or socket.inet_ntop(socket.AF_INET,quoted[16:20])!=destination:return None
    else:
        if len(quoted)<48 or quoted[0]>>4!=6 or quoted[6]!=socket.IPPROTO_ICMPV6:return None
        offset=40
        if socket.inet_ntop(socket.AF_INET6,quoted[24:40])!=str(ipaddress.ip_address(destination.split('%')[0])):return None
    if len(quoted)<offset+8:return None
    qkind,_,_,qid,qseq=struct.unpack('!BBHHH',quoted[offset:offset+8])
    if qkind!=(8 if version==4 else 128) or (qid,qseq)!=(ident,sequence):return None
    return 'ttl' if kind==(11 if version==4 else 3) else 'unreachable'


_access_lock = threading.Lock()
_access_ready = {}


def darwin_access_helper():
    if getattr(sys, 'frozen', False) and sys.platform == 'darwin':
        helper = Path(sys.executable).parent / 'LinkRadarNetworkAccess'
        if not helper.is_file():
            raise InterfaceUnavailable('В Mac-комплекте отсутствует проверка сетевого доступа. Обновите LinkRadar целиком.')
        return helper
    return None


def darwin_network_access(destination, binding, timeout, stop=None):
    """Ask macOS to evaluate local-network access before native ICMP.

    A connected UDP socket triggers the standard Local Network check without
    sending a datagram (Apple TN3179). ICMP sendto alone can silently time out
    in a Finder-launched app before that check has run. Both sockets are bound
    to the selected interface and source; this never changes routing or grants
    permission on the user's behalf. Repeat on the next probe after a denial.
    """
    helper = darwin_access_helper()
    if helper is not None:
        key = (binding['id'], binding['index'], binding['source'], destination)
        while not _access_lock.acquire(timeout=.1):
            if stop and stop.is_set():return False
        try:
            if stop and stop.is_set():return False
            now = time.monotonic()
            for old in list(_access_ready):
                if _access_ready[old] <= now:del _access_ready[old]
            if _access_ready.get(key, 0) > now:return True
            with subprocess.Popen([str(helper), destination, binding['source'], str(binding['index'])],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as child:
                deadline = time.monotonic() + 17
                try:
                    while child.poll() is None:
                        if stop and stop.is_set():return False
                        if time.monotonic() >= deadline:
                            raise InterfaceUnavailable('Проверка сетевого доступа macOS не завершилась; повторите запуск.')
                        time.sleep(.05)
                    state = child.stdout.read(128).strip()
                    if child.returncode != 0 or state != 'ready':
                        reason = ('macOS не разрешила локальную сеть. Проверьте разрешение LinkRadar → Локальная сеть.'
                                  if state == 'local_network_denied' else 'Выбранный адаптер или путь до цели недоступен.')
                        raise InterfaceUnavailable('Адаптер '+binding['id']+' ('+binding['source']+'): '+reason)
                finally:
                    if child.poll() is None:
                        child.terminate()
                        try:child.wait(timeout=1)
                        except subprocess.TimeoutExpired:child.kill();child.wait()
            _access_ready[key] = time.monotonic() + 30
            return True
        finally:
            _access_lock.release()
    # Development/CLI builds have no bundled Swift helper. Still initiate the
    # public UDP access check; packaged Finder builds always use the helper.
    v6 = ':' in destination
    scope = binding['index'] if v6 and ipaddress.ip_address(destination.split('%')[0]).is_link_local else 0
    with socket.socket(socket.AF_INET6 if v6 else socket.AF_INET,
                       socket.SOCK_DGRAM, socket.IPPROTO_UDP) as access:
        access.setsockopt(socket.IPPROTO_IPV6 if v6 else socket.IPPROTO_IP,
                          125 if v6 else 25, binding['index'])
        access.bind((binding['source'], 0, 0, scope) if v6 else (binding['source'], 0))
        access.settimeout(timeout)
        try:
            access.connect((destination.split('%')[0], 9, 0, scope) if v6 else (destination, 9))
        except OSError as exc:
            raise InterfaceUnavailable(
                'Нет сетевого доступа через '+binding['id']+' ('+binding['source']+'). '
                'Проверьте подключение и путь до цели; для локальной сети — '
                'разрешение LinkRadar в настройках macOS → Локальная сеть. '
                'Ошибка ОС: '+str(exc.errno)) from exc
    return True


def darwin_echo(destination,binding,timeout,ttl=64,stop=None):
    if stop and stop.is_set():return {'kind':'cancelled','ip':None,'latency':None}
    if darwin_network_access(destination, binding, timeout, stop) is False:
        return {'kind':'cancelled','ip':None,'latency':None}
    v6=':' in destination;family=socket.AF_INET6 if v6 else socket.AF_INET
    proto=socket.IPPROTO_ICMPV6 if v6 else socket.IPPROTO_ICMP
    ident=secrets.randbelow(65536);sequence=secrets.randbelow(65536)
    payload=secrets.token_bytes(32)
    packet=struct.pack('!BBHHH',128 if v6 else 8,0,0,ident,sequence)+payload
    if not v6:packet=packet[:2]+struct.pack('!H',checksum(packet))+packet[4:]
    with socket.socket(family,socket.SOCK_DGRAM,proto) as sock:
        # Darwin's datagram ICMP sockets need no root; BOUND_IF prevents fallback.
        sock.setsockopt(socket.IPPROTO_IPV6 if v6 else socket.IPPROTO_IP,125 if v6 else 25,binding['index'])
        sock.setsockopt(socket.IPPROTO_IPV6 if v6 else socket.IPPROTO_IP,socket.IPV6_UNICAST_HOPS if v6 else socket.IP_TTL,ttl)
        scope=binding['index'] if v6 and ipaddress.ip_address(destination.split('%')[0]).is_link_local else 0
        sock.bind((binding['source'],0,0,scope) if v6 else (binding['source'],0))
        endpoint=(destination.split('%')[0],0,0,scope) if v6 else (destination,0)
        if stop and stop.is_set():return {'kind':'cancelled','ip':None,'latency':None}
        tick=time.perf_counter();sock.sendto(packet,endpoint);deadline=tick+timeout
        while time.perf_counter()<deadline:
            if stop and stop.is_set():return {'kind':'cancelled','ip':None,'latency':None}
            sock.settimeout(min(.1,max(.001,deadline-time.perf_counter())))
            try:raw,peer=sock.recvfrom(65535)
            except socket.timeout:continue
            latency=(time.perf_counter()-tick)*1000
            kind=match_reply(raw,destination,ident,sequence)
            if kind and (kind!='echo' or peer[0].split('%')[0]==destination.split('%')[0]):
                return {'kind':kind,'ip':peer[0],'latency':round(latency,3)}
    return {'kind':'timeout','ip':None,'latency':None}


class IpOptions(C.Structure):
    _fields_=[('ttl',C.c_ubyte),('tos',C.c_ubyte),('flags',C.c_ubyte),('size',C.c_ubyte),('data',C.c_uint32)]
class EchoReply(C.Structure):
    _fields_=[('address',C.c_uint32),('status',C.c_uint32),('rtt',C.c_uint32)]
class Sockaddr6(C.Structure):
    _fields_=[('family',C.c_uint16),('port',C.c_uint16),('flow',C.c_uint32),('address',C.c_ubyte*16),('scope',C.c_uint32)]
class Address6(C.Structure):
    _fields_=[('port',C.c_uint16),('flow',C.c_uint32),('address',C.c_ubyte*16),('scope',C.c_uint32)]
class EchoReply6(C.Structure):
    _fields_=[('address',Address6),('status',C.c_uint32),('rtt',C.c_uint32)]


def windows_echo(destination,binding,timeout,ttl=64,stop=None):
    if stop and stop.is_set():return {'kind':'cancelled','ip':None,'latency':None}
    api=C.WinDLL('iphlpapi',use_last_error=True);v6=':' in destination
    create=api.Icmp6CreateFile if v6 else api.IcmpCreateFile;create.argtypes=[];create.restype=C.c_void_p
    api.IcmpCloseHandle.argtypes=[C.c_void_p];api.IcmpCloseHandle.restype=C.c_int
    handle=create()
    if handle in (None,C.c_void_p(-1).value):raise OSError('Не удалось открыть Windows ICMP API')
    payload=C.create_string_buffer(secrets.token_bytes(32));reply=C.create_string_buffer(4096);options=IpOptions(ttl,0,0,0,0)
    try:
        if v6:
            def addr(value):
                out=Sockaddr6();out.family=23;out.address[:]=socket.inet_pton(socket.AF_INET6,value.split('%')[0]);out.scope=binding['index'] if ipaddress.ip_address(value.split('%')[0]).is_link_local else 0;return out
            source=addr(binding['source']);target=addr(destination)
            fn=api.Icmp6SendEcho2;fn.restype=C.c_uint32
            fn.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p,C.POINTER(Sockaddr6),C.POINTER(Sockaddr6),C.c_void_p,C.c_uint16,C.POINTER(IpOptions),C.c_void_p,C.c_uint32,C.c_uint32]
            count=fn(handle,None,None,None,C.byref(source),C.byref(target),payload,32,C.byref(options),reply,len(reply),max(1,int(timeout*1000)))
            row=C.cast(reply,C.POINTER(EchoReply6)).contents
            address=socket.inet_ntop(socket.AF_INET6,bytes(row.address.address))
        else:
            fn=api.IcmpSendEcho2Ex;fn.restype=C.c_uint32
            fn.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p,C.c_uint32,C.c_uint32,C.c_void_p,C.c_uint16,C.POINTER(IpOptions),C.c_void_p,C.c_uint32,C.c_uint32]
            source=int.from_bytes(socket.inet_aton(binding['source']),'little');target=int.from_bytes(socket.inet_aton(destination),'little')
            count=fn(handle,None,None,None,source,target,payload,32,C.byref(options),reply,len(reply),max(1,int(timeout*1000)))
            row=C.cast(reply,C.POINTER(EchoReply)).contents
            address=socket.inet_ntoa(row.address.to_bytes(4,'little'))
        if stop and stop.is_set():return {'kind':'cancelled','ip':None,'latency':None}
        if not count:
            error=C.get_last_error()
            if error==11010:return {'kind':'timeout','ip':None,'latency':None}
            raise OSError('Windows ICMP: '+str(error))
        kind='echo' if row.status==0 else 'ttl' if row.status==11013 else 'timeout' if row.status==11010 else 'unreachable'
        return {'kind':kind,'ip':address if kind!='timeout' else None,'latency':float(row.rtt) if kind in ('echo','ttl') else None}
    finally:api.IcmpCloseHandle(handle)


def echo(destination,binding,timeout,ttl=64,stop=None):
    if platform.system()=='Darwin':return darwin_echo(destination,binding,timeout,ttl,stop)
    if platform.system()=='Windows':return windows_echo(destination,binding,timeout,ttl,stop)
    raise InterfaceUnavailable('Привязка адаптера поддерживается в macOS и Windows')


def detail(binding,text=''):
    return 'Адаптер '+binding['id']+' ('+binding['source']+') · '+text


def path_key(sample):
    detail=sample.get('detail','')
    return detail.split(' · ',1)[0] if detail.startswith('Адаптер ') else ''


def same_path(sample,before):
    # Most adjacent probes have identical detail. Avoid repeatedly splitting
    # their adapter/source label while keeping source changes as real breaks.
    return sample.get('detail','')==before.get('detail','') or path_key(sample)==path_key(before)

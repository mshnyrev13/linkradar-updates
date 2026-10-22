"""Explicit local Ethernet capture. Bounded, volatile 120-second header history."""
from collections import deque
from dataclasses import dataclass
import ctypes
import ipaddress
import struct
import sys
import threading
import time

import error_log
from network_info import PcapCapture

HISTORY_SECONDS=120
SNAPLEN=256
MAX_PACKETS=50000
MAX_MEMORY=32*1024*1024

# mach_absolute_time (Python monotonic on macOS) excludes sleep. History must
# expire across sleep too, while remaining immune to wall-clock corrections.
_continuous=None
_timebase=1.
if sys.platform=='darwin':
    class _Timebase(ctypes.Structure):_fields_=[('numer',ctypes.c_uint32),('denom',ctypes.c_uint32)]
    _system=ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    _system.mach_timebase_info.argtypes=[ctypes.POINTER(_Timebase)];_system.mach_timebase_info.restype=ctypes.c_int
    _basis=_Timebase()
    if _system.mach_timebase_info(ctypes.byref(_basis))!=0 or not _basis.denom:raise OSError('Системный таймер macOS недоступен')
    _continuous=_system.mach_continuous_time;_continuous.argtypes=[];_continuous.restype=ctypes.c_uint64
    _timebase=_basis.numer/_basis.denom/1e9

def history_clock():
    if _continuous is not None:return _continuous()*_timebase
    # Windows monotonic (QueryPerformanceCounter) includes suspension.
    return time.monotonic()


def mac(raw):return ':'.join(f'{part:02x}' for part in raw)
def endpoint(ip,port):return f'[{ip}]:{port}' if ':' in ip else f'{ip}:{port}'


def decode(raw):
    """Read captured bytes only; no DNS lookups or inferred TCP loss analysis."""
    if len(raw)<14:return '—','—','Ethernet','Усечённый Ethernet-заголовок'
    src,dst=mac(raw[6:12]),mac(raw[:6]);kind=int.from_bytes(raw[12:14],'big');offset=14;vlans=[]
    while kind in (0x8100,0x88a8,0x9100) and len(vlans)<4:
        if len(raw)<offset+4:return src,dst,'VLAN','Усечённый VLAN-заголовок'
        vlans.append(int.from_bytes(raw[offset:offset+2],'big')&4095)
        kind=int.from_bytes(raw[offset+2:offset+4],'big');offset+=4
    prefix=('VLAN '+','.join(map(str,vlans))+' · ') if vlans else ''
    if kind==0x0806:
        if len(raw)>=offset+28 and raw[offset+4:offset+6]==b'\x06\x04':
            src=str(ipaddress.IPv4Address(raw[offset+14:offset+18]));dst=str(ipaddress.IPv4Address(raw[offset+24:offset+28]))
            operation=int.from_bytes(raw[offset+6:offset+8],'big')
            return src,dst,'ARP',prefix+('Who has '+dst+'?' if operation==1 else src+' is at '+mac(raw[offset+8:offset+14]) if operation==2 else f'Operation {operation}')
        return src,dst,'ARP',prefix+'ARP'
    if kind==0x88cc:return src,dst,'LLDP',prefix+'Объявление коммутатора'
    if kind==0x888e:return src,dst,'EAPOL',prefix+'802.1X'
    if kind==0x0800:
        if len(raw)<offset+20:return src,dst,'IPv4',prefix+'Усечённый IPv4-заголовок'
        header=(raw[offset]&15)*4
        if raw[offset]>>4!=4 or header<20 or len(raw)<offset+header:return src,dst,'IPv4',prefix+'Некорректный/усечённый IPv4-заголовок'
        src=str(ipaddress.IPv4Address(raw[offset+12:offset+16]));dst=str(ipaddress.IPv4Address(raw[offset+16:offset+20]))
        proto=raw[offset+9];fragment=int.from_bytes(raw[offset+6:offset+8],'big')
        if fragment&8191:return src,dst,'IPv4',prefix+'Фрагмент IP · offset '+str((fragment&8191)*8)
        offset+=header
    elif kind==0x86dd:
        if len(raw)<offset+40 or raw[offset]>>4!=6:return src,dst,'IPv6',prefix+'Некорректный/усечённый IPv6-заголовок'
        src=str(ipaddress.IPv6Address(raw[offset+8:offset+24]));dst=str(ipaddress.IPv6Address(raw[offset+24:offset+40]))
        proto=raw[offset+6];offset+=40
        for _ in range(12):
            if proto not in (0,43,44,51,60):break
            if len(raw)<offset+8:return src,dst,'IPv6',prefix+'Усечённый extension header'
            following=raw[offset]
            if proto==44:
                if int.from_bytes(raw[offset+2:offset+4],'big')&0xfff8:return src,dst,'IPv6',prefix+'Фрагмент IPv6'
                length=8
            else:length=(raw[offset+1]+2)*4 if proto==51 else (raw[offset+1]+1)*8
            if len(raw)<offset+length:return src,dst,'IPv6',prefix+'Усечённый extension header'
            proto=following;offset+=length
    else:return src,dst,'802.3' if kind<1536 else f'0x{kind:04X}',prefix+'Ethernet'
    if proto==6:
        if len(raw)<offset+20:return src,dst,'TCP',prefix+'Усечённый TCP-заголовок'
        a,b,seq,ack=struct.unpack_from('!HHII',raw,offset);flags=raw[offset+13]
        names=[name for bit,name in ((1,'FIN'),(2,'SYN'),(4,'RST'),(8,'PSH'),(16,'ACK'),(32,'URG'),(64,'ECE'),(128,'CWR')) if flags&bit]
        return endpoint(src,a),endpoint(dst,b),'TCP',prefix+'['+', '.join(names)+f'] Seq={seq} Ack={ack} Win='+str(int.from_bytes(raw[offset+14:offset+16],'big'))
    if proto==17:
        if len(raw)<offset+8:return src,dst,'UDP',prefix+'Усечённый UDP-заголовок'
        a,b,size=struct.unpack_from('!HHH',raw,offset)
        return endpoint(src,a),endpoint(dst,b),'UDP',prefix+f'{a} → {b} · Length={size}'
    if proto in (1,58):
        label='ICMP' if proto==1 else 'ICMPv6'
        return src,dst,label,prefix+(f'Type={raw[offset]} Code={raw[offset+1]}' if len(raw)>=offset+2 else 'Усечённый ICMP-заголовок')
    return src,dst,'ESP' if proto==50 else f'IP/{proto}',prefix+'IP protocol '+str(proto)


@dataclass(slots=True)
class Frame:
    number:int
    ts:float
    captured_at:float
    length:int
    raw:bytes
    src:str
    dst:str
    protocol:str
    info:str
    @property
    def memory(self):return len(self.raw)+320+2*(len(self.src)+len(self.dst)+len(self.info))


class TrafficCapture:
    def __init__(self,clock=history_clock,capture_factory=PcapCapture):
        self.clock=clock;self.capture_factory=capture_factory;self.lock=threading.RLock();self.frames=deque();self.buckets={}
        self.memory=0;self.number=0;self.evicted=0;self.state='idle';self.error='';self.source=None;self.worker=None;self.stop_event=threading.Event();self.capture_stats=None;self.paused=False;self.epoch=threading.Event()
    def expire(self,now):
        cutoff=now-HISTORY_SECONDS
        while self.frames and self.frames[0].captured_at<=cutoff:self.memory-=self.frames.popleft().memory
        while self.buckets:
            first=next(iter(self.buckets))
            if first>cutoff:break
            del self.buckets[first]
    def add(self,packet):
        raw=bytes(packet['raw'][:SNAPLEN]);parts=decode(raw);now=self.clock()
        with self.lock:
            self.expire(now);self.number+=1
            frame=Frame(self.number,packet['ts'],now,packet['length'],raw,*parts);self.frames.append(frame);self.memory+=frame.memory
            bucket=self.buckets.setdefault(int(now),[0,0]);bucket[0]+=packet['length'];bucket[1]+=1
            while len(self.frames)>MAX_PACKETS or self.memory>MAX_MEMORY:
                self.memory-=self.frames.popleft().memory;self.evicted+=1
    def start(self,interface,switch=None,port=None):
        if not interface:raise ValueError('Выберите доступный сетевой адаптер')
        with self.lock:
            if self.worker and self.worker.is_alive():raise ValueError('Предыдущий захват ещё завершается')
            self.frames.clear();self.buckets.clear();self.memory=0;self.evicted=0;self.error='';self.capture_stats=None
            self.source=(interface,switch,port);self.state='starting';self.stop_event=threading.Event();self.paused=False;self.epoch=threading.Event()
            self.worker=threading.Thread(target=self.run,args=(interface,self.stop_event),daemon=True);self.worker.start()
    def run(self,interface,stop):
        capture=None;epoch=None;bases={'received':0,'dropped':0,'interface_dropped':0}
        def update_stats(stats):
            if isinstance(stats,dict):
                with self.lock:self.capture_stats={key:bases[key]+stats.get(key,0) for key in bases}
        def close_reader():
            nonlocal capture
            if capture:
                reader=capture;capture=None
                try:
                    try:stats=reader.stats()
                    except (OSError,ValueError,AttributeError):stats=None
                    update_stats(stats)
                    if isinstance(stats,dict):
                        for key in bases:bases[key]+=stats.get(key,0)
                finally:reader.close()
        try:
            stats_at=self.clock()
            while not stop.is_set():
                with self.lock:paused=self.paused;requested_epoch=self.epoch
                if capture and (paused or epoch is not requested_epoch):close_reader()
                if paused:stop.wait(.01);continue
                if capture is None:
                    epoch=requested_epoch;capture=self.capture_factory(interface,snaplen=SNAPLEN,promiscuous=True)
                    with self.lock:self.state='stopping' if stop.is_set() else 'paused' if self.paused else 'starting' if epoch is not self.epoch else 'running'
                    continue
                received=False
                for _ in range(512):
                    if stop.is_set() or epoch.is_set():break
                    packet=capture.read_packet()
                    if packet is None:break
                    with self.lock:
                        if stop.is_set() or epoch.is_set() or self.paused:break
                        self.add(packet);received=True
                if self.clock()-stats_at>=1:
                    update_stats(capture.stats())
                    stats_at=self.clock()
                if not received:stop.wait(.01)
        except (OSError,ValueError,AttributeError) as exc:
            error_log.error('Захват пакетов прерван',exc)
            with self.lock:self.state='error';self.error='Не удалось захватить пакеты. Проверьте доступ к BPF на macOS или установленный Npcap и права захвата на Windows.'
        finally:
            try:close_reader()
            except (OSError,ValueError,AttributeError):
                with self.lock:self.state='error';self.error='Не удалось закрыть захват пакетов.'
            with self.lock:
                if self.state!='error':self.state='stopped'
    def pause(self):
        with self.lock:
            if self.state not in ('starting','running','paused'):raise ValueError('Захват не выполняется')
            self.paused=not self.paused
            if self.paused:self.epoch.set();self.state='paused'
            else:self.epoch=threading.Event();self.state='starting'
            return self.paused
    def stop(self):
        with self.lock:
            self.stop_event.set();self.epoch.set()
            # A thread can still be alive just after its finally set 'stopped'.
            # Do not resurrect 'stopping' after cleanup has already finished.
            if self.state in ('starting','running','paused'):
                self.state='stopping' if self.worker and self.worker.is_alive() else 'stopped'
    def snapshot(self):
        with self.lock:
            now=self.clock();self.expire(now);rate=self.buckets.get(int(now)-1,[0,0]) if self.state=='running' else [0,0]
            return {'frames':list(self.frames),'bytes':sum(v[0] for v in self.buckets.values()),'packets':sum(v[1] for v in self.buckets.values()),
                    'mbps':rate[0]*8/1e6,'pps':rate[1],'memory':self.memory,'evicted':self.evicted,'state':self.state,'error':self.error,'source':self.source,'capture_stats':self.capture_stats,'worker_active':bool(self.worker and self.worker.is_alive())}
    def close(self):
        self.stop()
        if self.worker:self.worker.join(timeout=3)
        with self.lock:self.frames.clear();self.buckets.clear();self.memory=0

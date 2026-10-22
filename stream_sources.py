"""Read-only collectors of live-stream health for the Stream tab.

Sources: OBS (obs-websocket 5), vMix (XML API and the open Statistics window),
encoder TCP connections (Windows TCP table and ESTATS) and NDI sources (mDNS).
Nothing is written to OBS or vMix. Stream keys, SRT parameters and passwords are
never returned, stored or exported.
"""
import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
import error_log
import dns_resolver
import bound_http
import requests
from urllib3.exceptions import HTTPError as TransportError

OBS_PORT=4455
VMIX_API='http://127.0.0.1:8088/api'
# Processes whose TCP connections are treated as encoder traffic.
ENCODERS={'obs64.exe':'OBS','obs32.exe':'OBS','obs.exe':'OBS','vmix64.exe':'vMix','vmix.exe':'vMix','ndinode.exe':'vMix','ffmpeg.exe':'FFmpeg'}
STREAM_OUTPUTS={'simple_stream','adv_stream'}
RECORD_OUTPUTS={'simple_file_output','adv_file_output','adv_ffmpeg_output'}

class SourceError(Exception):pass

def is_loopback(host):
    if (host or '').lower()=='localhost':return True
    try:return ipaddress.ip_address(host).is_loopback
    except ValueError:return False

def safe_url(url):
    """Server address without credentials, stream key, query or SRT parameters."""
    url=(url or '').strip()
    if not url:return ''
    try:parts=urllib.parse.urlsplit(url)
    except ValueError:return ''
    if not parts.scheme or not parts.hostname:return ''
    host=parts.hostname if ':' not in parts.hostname else '['+parts.hostname+']'
    try:port=parts.port
    except ValueError:port=None
    address=parts.scheme.lower()+'://'+host+(':'+str(port) if port else '')
    # A key can occupy the FIRST path segment too. Preserve only the endpoint.
    return address

def safe_text(value):
    text=re.sub(r'\b[a-z][a-z0-9+.-]*://[^\s<>\"\']+',lambda m:safe_url(m.group()),str(value or ''),flags=re.I)
    return error_log.scrub(text)

def safe_name(value):
    text=safe_text(value)
    # Preserve distinct source identities when their secret portions differ.
    return text+' ['+hashlib.sha256(str(value).encode()).hexdigest()[:8]+']' if text!=value else text

def protocol_of(url,default='RTMP'):
    scheme=urllib.parse.urlsplit(url or '').scheme.lower()
    return {'rtmp':'RTMP','rtmps':'RTMPS','srt':'SRT','rist':'RIST','http':'HTTP','https':'HTTPS','whip':'WHIP'}.get(scheme,default)

# --- OBS -------------------------------------------------------------------------

class WebSocket:
    """Minimal RFC 6455 text client; enough for obs-websocket requests."""
    def __init__(self,host,port,timeout=3,protocol='obswebsocket.json'):
        self.buffer=b'';self.deadline=time.monotonic()+timeout
        self.sock=dns_resolver.create_connection((host,port),timeout=timeout)
        try:
            key=base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall((f'GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                               f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: {protocol}\r\n\r\n').encode())
            head=b''
            while b'\r\n\r\n' not in head:
                remaining=self.deadline-time.monotonic()
                if remaining<=0:raise SourceError('OBS: превышено время приветствия')
                self.sock.settimeout(remaining)
                chunk=self.sock.recv(4096)
                if not chunk:raise SourceError('obs-websocket закрыл соединение')
                head+=chunk
                if len(head)>65536:raise SourceError('Некорректный ответ obs-websocket')
            head,self.buffer=head.split(b'\r\n\r\n',1)
            lines=head.decode('latin-1').split('\r\n')
            if len(lines[0].split())<2 or lines[0].split()[1]!='101':raise SourceError('Порт не принимает WebSocket: '+lines[0][:80])
            headers={line.split(':',1)[0].strip().lower():line.split(':',1)[1].strip() for line in lines[1:] if ':' in line}
            # RFC 6455 handshake checksum, not password hashing or authentication.
            accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode(),usedforsecurity=False).digest()).decode()
            if headers.get('sec-websocket-accept')!=accept:raise SourceError('Неверное подтверждение WebSocket')
        except BaseException:
            self.close();raise
    def _read(self,count):
        while len(self.buffer)<count:
            remaining=self.deadline-time.monotonic()
            if remaining<=0:raise SourceError('OBS: превышено время чтения')
            self.sock.settimeout(remaining)
            chunk=self.sock.recv(65536)
            if not chunk:raise SourceError('Соединение с OBS закрыто')
            self.buffer+=chunk
        data,self.buffer=self.buffer[:count],self.buffer[count:];return data
    def send(self,data,opcode=1):
        payload=data.encode() if isinstance(data,str) else bytes(data);size=len(payload)
        header=bytes([0x80|opcode])+(bytes([0x80|size]) if size<126 else bytes([0x80|126])+struct.pack('!H',size) if size<65536 else bytes([0x80|127])+struct.pack('!Q',size))
        mask=os.urandom(4);self.sock.sendall(header+mask+bytes(b^mask[i%4] for i,b in enumerate(payload)))
    def receive(self):
        message=b'';fragmented=False;frames=0
        while True:
            frames+=1
            if frames>1024:raise SourceError('Слишком много фрагментов OBS')
            first,second=self._read(2);opcode=first&15;size=second&127
            if first&0x70 or second&0x80 or opcode not in (0,1,8,9,10):raise SourceError('Некорректный WebSocket-кадр OBS')
            if size==126:size=struct.unpack('!H',self._read(2))[0]
            elif size==127:size=struct.unpack('!Q',self._read(8))[0]
            if size>16*1024*1024:raise SourceError('Слишком большое сообщение OBS')
            if opcode>=8 and (not first&128 or size>125):raise SourceError('Некорректный управляющий кадр OBS')
            if opcode<8 and ((opcode==0)!=fragmented):raise SourceError('Некорректная последовательность фрагментов OBS')
            payload=self._read(size)
            if opcode==8:
                code=struct.unpack('!H',payload[:2])[0] if len(payload)>=2 else None
                raise SourceError('Неверный пароль obs-websocket' if code==4009 else 'OBS закрыл соединение'+(f' (код {code})' if code else ''))
            if opcode==9:self.send(payload,10);continue
            if opcode==10:continue
            if len(message)+len(payload)>16*1024*1024:raise SourceError('Слишком большое сообщение OBS')
            message+=payload
            if first&128:return message.decode('utf-8')
            fragmented=True
    def close(self):
        try:self.sock.close()
        except OSError:pass

def obs_auth(password,salt,challenge):
    secret=base64.b64encode(hashlib.sha256((password+salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret+challenge).encode()).digest()).decode()

def obs_config_path():
    if sys.platform=='win32':base=Path(os.environ.get('APPDATA',str(Path.home()/'AppData/Roaming')))
    elif sys.platform=='darwin':base=Path.home()/'Library/Application Support'
    else:base=Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))
    return base/'obs-studio/plugin_config/obs-websocket/config.json'

def local_obs_settings(path=None):
    """Port/password of obs-websocket on this computer; read only, never stored."""
    try:data=json.loads(Path(path or obs_config_path()).read_text(encoding='utf-8'))
    except (OSError,ValueError,TypeError):return {}
    if not isinstance(data,dict):return {}
    return {'port':data.get('server_port'),'password':(data.get('server_password') or '') if data.get('auth_required') else '','enabled':data.get('server_enabled')}

class ObsClient:
    def __init__(self,host='127.0.0.1',port=None,password='',timeout=3,config_path=None):
        self.host=host or '127.0.0.1';self.port=port;self.password=password or '';self.timeout=timeout;self.config_path=config_path
        self.ws=None;self.ids=0;self.version={}
    def connect(self):
        local=local_obs_settings(self.config_path) if is_loopback(self.host) else {}
        if local.get('enabled') is False and not self.port:raise SourceError('В OBS выключен WebSocket-сервер (Инструменты → Настройки сервера WebSocket)')
        port=int(self.port or local.get('port') or OBS_PORT);password=self.password or local.get('password') or ''
        try:ws=WebSocket(self.host,port,self.timeout)
        except OSError as exc:raise SourceError(f'OBS не отвечает на {self.host}:{port}') from exc
        try:
            hello=json.loads(ws.receive())
            if hello.get('op')!=0:raise SourceError('Нет приветствия obs-websocket 5')
            data=hello.get('d') or {};identify={'rpcVersion':1,'eventSubscriptions':0}
            if data.get('authentication'):
                if not password:raise SourceError('obs-websocket требует пароль')
                identify['authentication']=obs_auth(password,data['authentication']['salt'],data['authentication']['challenge'])
            ws.send(json.dumps({'op':1,'d':identify}))
            if json.loads(ws.receive()).get('op')!=2:raise SourceError('obs-websocket не подтвердил подключение')
        except (OSError,ValueError,KeyError) as exc:
            ws.close();raise SourceError('Ошибка протокола obs-websocket') from exc
        except BaseException:
            ws.close();raise
        self.ws=ws;self.port_used=port
        version=self.request('GetVersion');self.version={k:version.get(k) for k in ('obsVersion','obsWebSocketVersion','platformDescription')}
    def request(self,kind,data=None):
        self.ids+=1;rid=str(self.ids)
        self.ws.deadline=min(getattr(self,'poll_deadline',time.monotonic()+self.timeout),time.monotonic()+self.timeout)
        self.ws.send(json.dumps({'op':6,'d':{'requestType':kind,'requestId':rid,**({'requestData':data} if data else {})}}))
        while True:
            message=json.loads(self.ws.receive())
            body=message.get('d') or {}
            if message.get('op')==7 and body.get('requestId')==rid:
                status=body.get('requestStatus') or {}
                if not status.get('result'):raise SourceError(f'{kind}: '+safe_text(status.get('comment') or status.get('code')))
                return body.get('responseData') or {}
    def poll(self):
        self.poll_deadline=time.monotonic()+6
        if not self.ws:self.connect()
        try:
            result={'version':self.version,'stats':self.request('GetStats'),'video':self.request('GetVideoSettings'),
                    'stream':self.request('GetStreamStatus'),'record':self.request('GetRecordStatus'),'outputs':[]}
            try:
                service=self.request('GetStreamServiceSettings');settings=service.get('streamServiceSettings') or {}
                server=settings.get('server') or settings.get('service') or ''
                result['service']={'type':service.get('streamServiceType'),'server':safe_url(server) or (server if '://' not in server else ''),
                                   'protocol':'WHIP' if 'whip' in str(service.get('streamServiceType')) else protocol_of(server)}
            except SourceError:result['service']={}
            for output in self.request('GetOutputList').get('outputs',[]):
                name=output.get('outputName')
                if not output.get('outputActive') or name in STREAM_OUTPUTS|RECORD_OUTPUTS:continue
                try:status=self.request('GetOutputStatus',{'outputName':name})
                except SourceError:status={}
                result['outputs'].append({'name':safe_name(name),'kind':safe_text(output.get('outputKind')),**status})
            return result
        except (OSError,ValueError) as exc:
            self.close();raise SourceError('Связь с OBS потеряна') from exc
        except SourceError:
            self.close();raise
    def close(self):
        if self.ws:self.ws.close()
        self.ws=None

# --- vMix ------------------------------------------------------------------------

def parse_vmix_xml(text):
    if len(text)>4*1024*1024 or re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b',text,re.I):
        raise SourceError('vMix XML содержит запрещённые объявления или слишком большой ответ')
    root=ET.fromstring(text)
    def flag(tag):
        node=root.find(tag)
        return node is not None and (node.text or '').strip().lower()=='true'
    def channels(tag):
        node=root.find(tag)
        return [] if node is None else [key for key,value in node.attrib.items() if value.strip().lower()=='true']
    inputs=[]
    for node in root.iter('input'):
        if node.get('type') is None:continue
        inputs.append({'number':node.get('number'),'type':safe_text(node.get('type')),'title':safe_name(node.get('title') or node.get('shortTitle') or ''),'state':safe_text(node.get('state'))})
    return {'version':(root.findtext('version') or '').strip(),'edition':(root.findtext('edition') or '').strip(),
            'streaming':flag('streaming'),'streaming_channels':channels('streaming'),'recording':flag('recording'),
            'external':flag('external'),'fullscreen':flag('fullscreen'),'multicorder':flag('multiCorder'),'inputs':inputs}

STREAM_INPUT=re.compile(r'srt|ndi|stream|rtmp|rtsp|hls|udp|zoom|teams|call|web',re.I)

def vmix_api(url=VMIX_API,timeout=2):
    try:parts=urllib.parse.urlsplit(url)
    except ValueError:raise SourceError('Некорректный адрес vMix API') from None
    if parts.scheme.lower() not in ('http','https') or not parts.hostname:raise SourceError('Адрес vMix API должен начинаться с http://')
    if parts.query or parts.fragment or parts.path.rstrip('/').lower()!='/api':raise SourceError('Укажите адрес /api без команд, параметров и фрагмента')
    try:
        with requests.Session() as client:
            client.mount('http://',bound_http.adapter(None));client.mount('https://',bound_http.adapter(None))
            with client.get(url,timeout=(timeout,timeout),stream=True,allow_redirects=False) as response:
                if response.status_code!=200:raise SourceError('vMix API: HTTP '+str(response.status_code)+'; укажите конечный адрес /api')
                raw=bytearray();deadline=time.monotonic()+timeout
                with bound_http.Deadline(timeout,response):
                    while True:
                        if time.monotonic()>=deadline:raise SourceError('vMix API: превышено время чтения')
                        chunk=response.raw.read1(64*1024,decode_content=True)
                        if not chunk:break
                        if len(raw)+len(chunk)>4*1024*1024:raise SourceError('Слишком большой ответ vMix API')
                        raw.extend(chunk)
                text=raw.decode('utf-8','replace')
    except (OSError,requests.RequestException,TransportError) as exc:raise SourceError('vMix API не отвечает: '+safe_url(url)) from exc
    try:return parse_vmix_xml(text)
    except ET.ParseError as exc:raise SourceError('vMix API вернул не XML') from exc

AUDIO=re.compile(r'(?P<buf_a>\d+)\s*/\s*(?P<buf_b>\d+)\s*ms\s+Dropped:\s*(?P<drop_a>\d+)\s*/\s*(?P<drop_b>\d+)\s+Resampled:\s*(?P<res_a>\d+)\s*/\s*(?P<res_b>\d+)',re.I)
FULLSCREEN=re.compile(r'Fullscreen\s+Dropped:\s*(?P<a>\d+)\s+(?P<b>\d+)\s+(?P<c>\d+)\s*\((?P<latency>\d+)\s*ms\)',re.I)
CONNECTED=re.compile(r'Connected\s+(?P<endpoint>\S+)',re.I)
LATENCY=re.compile(r'\((?P<value>\d+)\s*ms\)',re.I)
COLUMN_KEYS=[('source','source_dropped'),('render','renderer_dropped'),('resync','resync'),('queue','queue'),('video','video'),('audio','audio')]
DEFAULT_COLUMNS=['name','source_dropped','renderer_dropped','resync','queue','video','audio']

def vmix_kind(name):
    lower=name.lower()
    if 'srt' in lower:return 'srt_output' if 'out' in lower else 'srt_input'
    if 'ndi' in lower:return 'ndi'
    if lower.startswith('output') or lower in ('stream','record','recording','external','fullscreen'):return 'output'
    return 'input'

def integer(text):
    match=re.search(r'-?\d+',text or '')
    return int(match.group()) if match else None

def parse_vmix_rows(headers,rows):
    """Rows of the vMix Statistics list as dictionaries with parsed counters."""
    columns=[]
    for i,header in enumerate(headers):
        lower=(header or '').lower();key=next((k for word,k in COLUMN_KEYS if word in lower),None)
        columns.append(key or ('name' if i==0 else 'col'+str(i)))
    if not headers or len(set(columns)&set(DEFAULT_COLUMNS))<3:columns=DEFAULT_COLUMNS+['col'+str(i) for i in range(len(DEFAULT_COLUMNS),max(map(len,rows),default=0))]
    result=[]
    for raw in rows:
        raw=[safe_name(text) if i==0 else safe_text(text) for i,text in enumerate(raw)]
        values={columns[i]:(text or '').strip() for i,text in enumerate(raw) if i<len(columns)}
        name=values.get('name','');video=values.get('video','');audio=values.get('audio','')
        row={'name':name,'kind':vmix_kind(name),'video':video,'audio':audio,'raw':list(raw)}
        for key in ('source_dropped','renderer_dropped','resync','queue'):row[key]=integer(values.get(key))
        parsed={}
        if match:=FULLSCREEN.search(video):parsed.update(fullscreen_dropped=sum(int(match.group(k)) for k in 'abc'),latency_ms=int(match.group('latency')))
        elif match:=LATENCY.search(video):parsed['latency_ms']=int(match.group('value'))
        if match:=CONNECTED.search(video):parsed['endpoint']=match.group('endpoint')
        if match:=AUDIO.search(audio):
            parsed.update(audio_buffer_ms=int(match.group('buf_a')),audio_dropped=int(match.group('drop_a'))+int(match.group('drop_b')),audio_resampled=int(match.group('res_a'))+int(match.group('res_b')))
        row.update(parsed);result.append(row)
    return result

# vMix 26 Statistics has a second tab, SRT, with its own list:
# Connection | Duration | Bandwidth (mbps) | Packet Loss (%) | Total Data | RTT (ms).
# Headers are real; cell formats are not observed yet, so numbers are read leniently.
SRT_COLUMN_KEYS=[('connection','name'),('duration','duration'),('bandwidth','bandwidth'),('loss','packet_loss_pct'),('total','total_data'),('rtt','rtt_ms')]

def is_srt_headers(headers):
    text=' '.join(headers or []).lower()
    return 'bandwidth' in text and 'rtt' in text

def number(text):
    match=re.search(r'-?\d+(?:[.,]\d+)?',text or '')
    return float(match.group().replace(',','.')) if match else None

def parse_vmix_srt_rows(headers,rows):
    columns=[next((k for word,k in SRT_COLUMN_KEYS if word in (h or '').lower()),'col'+str(i)) for i,h in enumerate(headers)]
    result=[]
    for raw in rows:
        raw=[safe_name(text) if i==0 else safe_text(text) for i,text in enumerate(raw)]
        values={columns[i]:(text or '').strip() for i,text in enumerate(raw) if i<len(columns)}
        bandwidth=number(values.get('bandwidth'))
        summary=' · '.join(f'{h}: {(t or "").strip()}' for h,t in zip(headers[1:],raw[1:]) if (t or '').strip())
        result.append({'name':values.get('name',''),'kind':'srt','video':summary,'audio':'','raw':list(raw),'duration':values.get('duration',''),'total_data':values.get('total_data',''),
                       'bandwidth_kbps':bandwidth*1000 if bandwidth is not None else None,'packet_loss_pct':number(values.get('packet_loss_pct')),'rtt_ms':number(values.get('rtt_ms')),
                       'source_dropped':None,'renderer_dropped':None,'resync':None,'queue':None})
    return result

def parse_vmix_list(headers,rows):
    return parse_vmix_srt_rows(headers,rows) if is_srt_headers(headers) else parse_vmix_rows(headers,rows)

COUNTERS=('source_dropped','renderer_dropped','fullscreen_dropped','audio_dropped','audio_resampled')

def vmix_deltas(rows,previous):
    """Per-row counter growth since the previous read; a reset is not counted as loss."""
    before={(row['kind'],row['name']):row for row in previous or []}
    for row in rows:
        old=before.get((row['kind'],row['name']));row['delta']={}
        for key in COUNTERS:
            if old and row.get(key) is not None and old.get(key) is not None and row[key]>=old[key]:row['delta'][key]=row[key]-old[key]
    return rows

# --- Windows ---------------------------------------------------------------------

if sys.platform=='win32':
    import ctypes
    from ctypes import wintypes
    user32=ctypes.WinDLL('user32',use_last_error=True);kernel32=ctypes.WinDLL('kernel32',use_last_error=True);iphlpapi=ctypes.WinDLL('iphlpapi')
    WNDENUMPROC=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    user32.EnumWindows.argtypes=[WNDENUMPROC,wintypes.LPARAM];user32.EnumChildWindows.argtypes=[wintypes.HWND,WNDENUMPROC,wintypes.LPARAM]
    user32.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int];user32.GetClassNameW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)];user32.IsWindowVisible.argtypes=[wintypes.HWND]
    user32.SendMessageTimeoutW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM,wintypes.UINT,wintypes.UINT,ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype=ctypes.c_size_t
    kernel32.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel32.OpenProcess.restype=wintypes.HANDLE
    kernel32.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    kernel32.VirtualAllocEx.argtypes=[wintypes.HANDLE,ctypes.c_void_p,ctypes.c_size_t,wintypes.DWORD,wintypes.DWORD];kernel32.VirtualAllocEx.restype=ctypes.c_void_p
    kernel32.VirtualFreeEx.argtypes=[wintypes.HANDLE,ctypes.c_void_p,ctypes.c_size_t,wintypes.DWORD]
    kernel32.WriteProcessMemory.argtypes=[wintypes.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_size_t)]
    kernel32.ReadProcessMemory.argtypes=[wintypes.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_size_t)]
    kernel32.IsWow64Process.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]

    class LVITEMW(ctypes.Structure):
        _fields_=[('mask',wintypes.UINT),('iItem',ctypes.c_int),('iSubItem',ctypes.c_int),('state',wintypes.UINT),('stateMask',wintypes.UINT),
                  ('pszText',ctypes.c_void_p),('cchTextMax',ctypes.c_int),('iImage',ctypes.c_int),('lParam',ctypes.c_ssize_t),('iIndent',ctypes.c_int),
                  ('iGroupId',ctypes.c_int),('cColumns',wintypes.UINT),('puColumns',ctypes.c_void_p),('piColFmt',ctypes.c_void_p),('iGroup',ctypes.c_int)]
    class HDITEMW(ctypes.Structure):
        _fields_=[('mask',wintypes.UINT),('cxy',ctypes.c_int),('pszText',ctypes.c_void_p),('hbm',ctypes.c_void_p),('cchTextMax',ctypes.c_int),
                  ('fmt',ctypes.c_int),('lParam',ctypes.c_ssize_t),('iImage',ctypes.c_int),('iOrder',ctypes.c_int),('type',wintypes.UINT),
                  ('pvFilter',ctypes.c_void_p),('state',wintypes.UINT)]

def process_name(pid):
    if sys.platform!='win32' or not pid:return ''
    handle=kernel32.OpenProcess(0x1000,False,pid)
    if not handle:return ''
    try:
        size=wintypes.DWORD(1024);buffer=ctypes.create_unicode_buffer(1024)
        return os.path.basename(buffer.value).lower() if kernel32.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)) else ''
    finally:kernel32.CloseHandle(handle)

def _windows(parent=None):
    found=[]
    callback=WNDENUMPROC(lambda hwnd,_:found.append(hwnd) or True)
    if parent is None:user32.EnumWindows(callback,0)
    else:user32.EnumChildWindows(parent,callback,0)
    return found

def _text(hwnd,getter):
    buffer=ctypes.create_unicode_buffer(256);getter(hwnd,buffer,256);return buffer.value

def _send(hwnd,message,wparam=0,lparam=0):
    result=ctypes.c_size_t()
    if not user32.SendMessageTimeoutW(hwnd,message,wparam,lparam,0x2|0x1,700,ctypes.byref(result)):raise SourceError('Окно vMix Statistics не отвечает')
    return result.value

class RemoteBuffer:
    """Memory in the vMix process for list-view messages; freed on exit."""
    def __init__(self,pid,size):
        self.process=kernel32.OpenProcess(0x8|0x10|0x20|0x400,False,pid)
        if not self.process:raise SourceError('Нет доступа к процессу vMix (запущен от администратора?)')
        wow=wintypes.BOOL()
        if kernel32.IsWow64Process(self.process,ctypes.byref(wow)) and wow.value!=(ctypes.sizeof(ctypes.c_void_p)==4):
            kernel32.CloseHandle(self.process);raise SourceError('Разрядность vMix не совпадает с приложением')
        self.address=kernel32.VirtualAllocEx(self.process,None,size,0x3000,0x04);self.size=size
        if not self.address:kernel32.CloseHandle(self.process);raise SourceError('Не удалось выделить буфер в процессе vMix')
    def write(self,offset,data):
        kernel32.WriteProcessMemory(self.process,self.address+offset,ctypes.byref(data),ctypes.sizeof(data),None)
    def read_text(self,offset,chars):
        buffer=ctypes.create_unicode_buffer(chars)
        if not kernel32.ReadProcessMemory(self.process,self.address+offset,buffer,chars*2,None):return ''
        return buffer.value
    def __enter__(self):return self
    def __exit__(self,*args):kernel32.VirtualFreeEx(self.process,self.address,0,0x8000);kernel32.CloseHandle(self.process)

def find_statistics_window(processes=('vmix64.exe','vmix.exe'),title='Statistics'):
    """(pid, list views) of the Statistics window, the visible list first.

    vMix keeps one list per tab (Overview, SRT); the hidden one stays a child window.
    """
    for hwnd in _windows():
        if not user32.IsWindowVisible(hwnd) or title.lower() not in _text(hwnd,user32.GetWindowTextW).lower():continue
        pid=wintypes.DWORD();user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        if process_name(pid.value) not in processes:continue
        lists=[child for child in _windows(hwnd) if 'syslistview32' in _text(child,user32.GetClassNameW).lower()]
        if lists:return pid.value,sorted(lists,key=lambda child:not user32.IsWindowVisible(child))
        raise SourceError('Окно Statistics найдено, но таблица в нём не найдена')
    return None

def read_list_view(pid,hwnd,max_rows=200,max_columns=24,chars=512):
    LVM_GETITEMCOUNT,LVM_GETHEADER,LVM_GETITEMTEXTW=0x1004,0x101F,0x1073;HDM_GETITEMCOUNT,HDM_GETITEMW=0x1200,0x120B
    header=_send(hwnd,LVM_GETHEADER);columns=min(max_columns,_send(header,HDM_GETITEMCOUNT)) if header else 0
    count=min(max_rows,_send(hwnd,LVM_GETITEMCOUNT));item_size=max(ctypes.sizeof(LVITEMW),ctypes.sizeof(HDITEMW))
    with RemoteBuffer(pid,item_size+chars*2) as remote:
        text=remote.address+item_size;headers=[]
        for column in range(columns):
            item=HDITEMW(mask=0x2,pszText=text,cchTextMax=chars);remote.write(0,item)
            headers.append(remote.read_text(item_size,chars) if _send(header,HDM_GETITEMW,column,remote.address) else '')
        rows=[]
        for index in range(count):
            row=[]
            for column in range(max(1,columns)):
                item=LVITEMW(iSubItem=column,pszText=text,cchTextMax=chars);remote.write(0,item)
                length=_send(hwnd,LVM_GETITEMTEXTW,index,remote.address)
                row.append(remote.read_text(item_size,min(chars,length+1)) if length else '')
            rows.append(row)
    return headers,rows

def vmix_statistics(processes=('vmix64.exe','vmix.exe'),title='Statistics'):
    """None when the window is closed; otherwise parsed rows of the vMix Statistics list."""
    if sys.platform!='win32':raise SourceError('Окно Statistics читается только в Windows')
    found=find_statistics_window(processes,title)
    if not found:return None
    pid,lists=found;rows=[]
    for hwnd in lists:rows+=parse_vmix_list(*read_list_view(pid,hwnd))
    return rows

# Encoder TCP connections. ESTATS bytes/RTT need collection enabled, which Windows
# allows only to administrators; without it the connection list is still shown.

if sys.platform=='win32':
    class MIB_TCPROW(ctypes.Structure):
        _fields_=[('dwState',wintypes.DWORD),('dwLocalAddr',wintypes.DWORD),('dwLocalPort',wintypes.DWORD),('dwRemoteAddr',wintypes.DWORD),('dwRemotePort',wintypes.DWORD)]
    class MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_=MIB_TCPROW._fields_+[('dwOwningPid',wintypes.DWORD)]
    class MIB_TCP6ROW(ctypes.Structure):
        _fields_=[('State',wintypes.DWORD),('LocalAddr',ctypes.c_ubyte*16),('dwLocalScopeId',wintypes.DWORD),('dwLocalPort',wintypes.DWORD),
                  ('RemoteAddr',ctypes.c_ubyte*16),('dwRemoteScopeId',wintypes.DWORD),('dwRemotePort',wintypes.DWORD)]
    class MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
        _fields_=[('ucLocalAddr',ctypes.c_ubyte*16),('dwLocalScopeId',wintypes.DWORD),('dwLocalPort',wintypes.DWORD),('ucRemoteAddr',ctypes.c_ubyte*16),
                  ('dwRemoteScopeId',wintypes.DWORD),('dwRemotePort',wintypes.DWORD),('dwState',wintypes.DWORD),('dwOwningPid',wintypes.DWORD)]
    class ESTATS_RW(ctypes.Structure):_fields_=[('EnableCollection',ctypes.c_ubyte)]
    class ESTATS_DATA_ROD(ctypes.Structure):
        _fields_=[('DataBytesOut',ctypes.c_uint64),('DataSegsOut',ctypes.c_uint64),('DataBytesIn',ctypes.c_uint64),('DataSegsIn',ctypes.c_uint64),
                  ('SegsOut',ctypes.c_uint64),('SegsIn',ctypes.c_uint64),('SoftErrors',wintypes.ULONG),('SoftErrorReason',wintypes.ULONG),
                  ('SndUna',wintypes.ULONG),('SndNxt',wintypes.ULONG),('SndMax',wintypes.ULONG),('ThruBytesAcked',ctypes.c_uint64),
                  ('RcvNxt',wintypes.ULONG),('ThruBytesReceived',ctypes.c_uint64)]
    PATH_FIELDS=('FastRetran Timeouts SubsequentTimeouts CurTimeoutCount AbruptTimeouts PktsRetrans BytesRetrans DupAcksIn SacksRcvd SackBlocksRcvd '
                 'CongSignals PreCongSumCwnd PreCongSumRtt PostCongSumRtt PostCongCountRtt EcnSignals EceRcvd SendStall QuenchRcvd RetranThresh '
                 'SndDupAckEpisodes SumBytesReordered NonRecovDa NonRecovDaEpisodes AckAfterFr DsackDups SampleRtt SmoothedRtt RttVar MaxRtt MinRtt '
                 'SumRtt CountRtt CurRto MaxRto MinRto CurMss MaxMss MinMss SpuriousRtoDetections').split()
    class ESTATS_PATH_ROD(ctypes.Structure):_fields_=[(name,wintypes.ULONG) for name in PATH_FIELDS]
    TCP_DATA,TCP_PATH=1,3

def _port(value):return socket.ntohs(value&0xffff)

def tcp_table():
    """Established TCP connections with owner PID, IPv4 and IPv6."""
    if sys.platform!='win32':raise SourceError('Соединения процессов читаются только в Windows')
    result=[]
    for family,row_type in ((socket.AF_INET,MIB_TCPROW_OWNER_PID),(socket.AF_INET6,MIB_TCP6ROW_OWNER_PID)):
        size=wintypes.DWORD(0);iphlpapi.GetExtendedTcpTable(None,ctypes.byref(size),False,family,5,0)
        for _ in range(3):
            buffer=ctypes.create_string_buffer(size.value+4096);size=wintypes.DWORD(len(buffer))
            status=iphlpapi.GetExtendedTcpTable(buffer,ctypes.byref(size),False,family,5,0)
            if status!=122:break
        if status!=0:continue
        count=ctypes.cast(buffer,ctypes.POINTER(wintypes.DWORD))[0]
        # dwNumEntries is followed by 4-byte aligned rows in both tables.
        rows=ctypes.cast(ctypes.addressof(buffer)+4,ctypes.POINTER(row_type*count)).contents
        for row in rows:
            if family==socket.AF_INET:
                if row.dwState!=5:continue
                local=socket.inet_ntoa(struct.pack('<I',row.dwLocalAddr));remote=socket.inet_ntoa(struct.pack('<I',row.dwRemoteAddr))
            else:
                if row.dwState!=5:continue
                local=socket.inet_ntop(socket.AF_INET6,bytes(row.ucLocalAddr));remote=socket.inet_ntop(socket.AF_INET6,bytes(row.ucRemoteAddr))
            result.append({'family':family,'local':local,'local_port':_port(row.dwLocalPort),'remote':remote,'remote_port':_port(row.dwRemotePort),
                           'pid':row.dwOwningPid,'row':row})
    return result

def _estats_row(conn):
    row=conn['row']
    if conn['family']==socket.AF_INET:
        return MIB_TCPROW(5,row.dwLocalAddr,row.dwLocalPort,row.dwRemoteAddr,row.dwRemotePort),iphlpapi.GetPerTcpConnectionEStats,iphlpapi.SetPerTcpConnectionEStats
    six=MIB_TCP6ROW(5,row.ucLocalAddr,row.dwLocalScopeId,row.dwLocalPort,row.ucRemoteAddr,row.dwRemoteScopeId,row.dwRemotePort)
    return six,iphlpapi.GetPerTcp6ConnectionEStats,iphlpapi.SetPerTcp6ConnectionEStats

def tcp_estats(conn,enable=True):
    """Bytes, retransmissions and RTT of one connection, or None without rights."""
    row,get,set_=_estats_row(conn);values={}
    for kind,rod in ((TCP_DATA,ESTATS_DATA_ROD),(TCP_PATH,ESTATS_PATH_ROD)):
        for attempt in (0,1):
            data=rod();rw=ESTATS_RW()
            status=get(ctypes.byref(row),kind,ctypes.byref(rw),0,ctypes.sizeof(rw),None,0,0,ctypes.byref(data),0,ctypes.sizeof(data))
            if status==0 and rw.EnableCollection:break
            # Enabling is attempted once; it succeeds only with administrator rights.
            if attempt or not enable or set_(ctypes.byref(row),kind,ctypes.byref(ESTATS_RW(1)),0,1,0)!=0:return None
        values.update({name:getattr(data,name) for name,_ in rod._fields_})
    return estats_values(values)

def estats_values(values):
    # Before the first RTT sample Windows reports 0xFFFFFFFF; 0 ms is a real loopback value.
    sampled=values['CountRtt']>0
    def ms(name):return values[name] if sampled and values[name]<0xffffffff else None
    return {'bytes_out':values['DataBytesOut'],'bytes_in':values['DataBytesIn'],'segments_out':values['DataSegsOut'],
            'retrans_segments':values['PktsRetrans'],'retrans_bytes':values['BytesRetrans'],'rtt':ms('SmoothedRtt'),
            'rtt_min':ms('MinRtt'),'rtt_max':ms('MaxRtt'),'timeouts':values['Timeouts'],'congestion_signals':values['CongSignals']}

NDI_PORTS=range(5960,6000)

def classify_connection(conn,ndi_endpoints=()):
    port=conn['remote_port']
    if (conn['remote'],port) in ndi_endpoints or port in NDI_PORTS or conn['local_port'] in NDI_PORTS:return 'NDI'
    return {1935:'RTMP',443:'RTMPS/HTTPS',80:'HTTP',554:'RTSP'}.get(port,'TCP')

def encoder_connections(ndi_endpoints=(),processes=None,exclude_pids=(),estats=True):
    processes=processes or ENCODERS;names={};result=[]
    for conn in tcp_table():
        if conn['pid'] in exclude_pids:continue
        name=names.get(conn['pid'])
        if name is None:name=names[conn['pid']]=process_name(conn['pid'])
        if name not in processes:continue
        protocol=classify_connection(conn,ndi_endpoints)
        # Loopback is the control plane (WebSocket/API) unless it carries NDI.
        if is_loopback(conn['remote']) and protocol!='NDI':continue
        conn={**conn,'process':name,'app':processes[name],'protocol':protocol}
        conn['estats']=tcp_estats(conn) if estats else None
        conn.pop('row');result.append(conn)
    return result

# --- NDI discovery ---------------------------------------------------------------

def mdns_query(name='_ndi._tcp.local',unicast=True):
    return struct.pack('!6H',0,0,1,0,0,0)+b''.join(bytes([len(part)])+part.encode() for part in name.split('.'))+b'\0'+struct.pack('!HH',12,0x8001 if unicast else 1)

def _name(data,offset,depth=0):
    labels=[];visited=set();end=None;wire_size=0;jumps=depth
    while True:
        if offset>=len(data) or offset in visited or jumps>20:raise ValueError('bad name')
        visited.add(offset)
        size=data[offset]
        if size&0xc0==0xc0:
            if offset+2>len(data):raise ValueError('bad pointer')
            pointer=struct.unpack('!H',data[offset:offset+2])[0]&0x3fff
            if end is None:end=offset+2
            offset=pointer;jumps+=1;continue
        offset+=1
        if not size:return '.'.join(labels),end if end is not None else offset
        if size>63 or offset+size>len(data):raise ValueError('bad label')
        wire_size+=size+1
        if wire_size>254:raise ValueError('DNS name too long')
        labels.append(data[offset:offset+size].decode('utf-8','replace'));offset+=size

def parse_mdns(data):
    """Resource records of an mDNS response: (name, type, value)."""
    records=[]
    _,flags,questions,answers,authority,additional=struct.unpack('!6H',data[:12]);offset=12
    if len(data)>9000 or questions>256 or answers+authority+additional>2048:raise ValueError('mDNS limit')
    for _ in range(questions):offset=_name(data,offset)[1]+4
    for _ in range(answers+authority+additional):
        name,offset=_name(data,offset);kind,_,_,size=struct.unpack('!HHIH',data[offset:offset+10]);offset+=10;body=offset;offset+=size
        if offset>len(data):raise ValueError('truncated mDNS record')
        if kind==12:
            value,name_end=_name(data,body)
            if name_end>offset:raise ValueError('bad PTR length')
        elif kind==33:
            if size<7:raise ValueError('bad SRV length')
            priority,weight,port=struct.unpack('!3H',data[body:body+6]);target,name_end=_name(data,body+6)
            if name_end>offset:raise ValueError('bad SRV name length')
            value=(port,target)
        elif kind==1 and size==4:value=socket.inet_ntoa(data[body:body+4])
        elif kind==28 and size==16:value=socket.inet_ntop(socket.AF_INET6,data[body:body+16])
        else:continue
        records.append((name,kind,value))
    return records

def ndi_sources(records):
    ptr=[value for name,kind,value in records if kind==12 and name.lower()=='_ndi._tcp.local']
    srv={name:value for name,kind,value in records if kind==33}
    address={}
    for name,kind,value in records:
        if kind in (1,28):address.setdefault(name.lower(),[]).append(value)
    result=[]
    for instance in dict.fromkeys(ptr):
        port,target=srv.get(instance,(None,''))
        ips=address.get(target.lower(),[])
        result.append({'name':instance[:-len('._ndi._tcp.local')] if instance.lower().endswith('._ndi._tcp.local') else instance,
                       'host':target.rstrip('.'),'ip':next((ip for ip in ips if ':' not in ip),ips[0] if ips else None),'port':port})
    return result

def discover_ndi(timeout=1.5):
    """NDI senders announced by mDNS. NDI Discovery Server setups are not covered."""
    sockets=[];records=[]
    try:
        query=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);query.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_TTL,255);query.bind(('',0));sockets.append(query)
        try:
            listen=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);listen.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);listen.bind(('',5353))
            listen.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,socket.inet_aton('224.0.0.251')+socket.inet_aton('0.0.0.0'));sockets.append(listen)
        except OSError:pass
        query.sendto(mdns_query(),('224.0.0.251',5353));end=time.monotonic()+timeout
        import select
        packets=0
        while (left:=end-time.monotonic())>0 and packets<256 and len(records)<2048:
            ready,_,_=select.select(sockets,[],[],left)
            for sock in ready:
                try:
                    packets+=1
                    records+=parse_mdns(sock.recv(9000))[:2048-len(records)]
                except (OSError,ValueError,struct.error):pass
    finally:
        for sock in sockets:sock.close()
    return ndi_sources(records)

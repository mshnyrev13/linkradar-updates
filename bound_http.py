"""Strict per-socket HTTP interface binding; no system routing mutations."""
import ipaddress
import platform
import socket
import struct
import time
import threading
import requests
from urllib3.connection import HTTPSConnection,HTTPConnection
from urllib3.connectionpool import HTTPSConnectionPool,HTTPConnectionPool
from urllib3.exceptions import NewConnectionError
import dns_resolver
import interface_probes

# Requests read timeouts are per socket read: a peer can drip bytes forever
# while BufferedReader waits for a whole header/body chunk. One shared daemon
# bounds those operations and interrupts the socket without adding a thread
# for each chunk in a speed test.
_condition=threading.Condition(threading.RLock())
_active=set()
_watcher=None
_local=threading.local()

class Deadline:
    def __init__(self,seconds,response=None,stop=None):
        self.until=time.monotonic()+max(.001,seconds);self.stop=stop
        self.connections=[];self.response=response;self.expired=False;self.previous=None
    def add(self,connection):
        with _condition:
            if connection not in self.connections:self.connections.append(connection)
    def __enter__(self):
        global _watcher
        self.previous=getattr(_local,'deadline',None);_local.deadline=self
        if self.response is not None:
            raw=getattr(self.response,'raw',None)
            connection=getattr(raw,'_connection',None)
            if connection is not None:self.add(connection)
            # HTTP/1.0 and Connection: close detach conn.sock while the body is
            # still read through SocketIO. Keep that actual socket interruptible.
            fp=getattr(raw,'_fp',None);fp=getattr(fp,'fp',None)
            sock=getattr(getattr(fp,'raw',None),'_sock',None)
            if isinstance(sock,socket.socket):self.add(sock)
        with _condition:
            _active.add(self)
            if _watcher is None:
                _watcher=threading.Thread(target=_watch,name='http-deadlines',daemon=True);_watcher.start()
            _condition.notify()
        return self
    def __exit__(self,*args):
        with _condition:_active.discard(self)
        _local.deadline=self.previous
        if self.expired:
            if self.stop and self.stop.is_set():raise InterruptedError('HTTP-запрос отменён') from None
            raise TimeoutError('Превышено общее время HTTP-запроса') from None

def _watch():
    while True:
        with _condition:
            if not _active:_condition.wait();continue
            for guard in list(_active):
                if time.monotonic()>=guard.until or (guard.stop and guard.stop.is_set()):
                    guard.expired=True
                    for connection in guard.connections:
                        sock=connection if isinstance(connection,socket.socket) else getattr(connection,'sock',None)
                        if sock is not None:
                            try:sock.shutdown(socket.SHUT_RDWR)
                            except OSError:pass
            _condition.wait(.05)

def _register(connection):
    guard=getattr(_local,'deadline',None)
    if guard is not None:guard.add(connection)

def connect(host,port,identity,timeout):
    timeout=timeout if isinstance(timeout,(int,float)) and timeout>0 else dns_resolver.TIMEOUT
    if identity is None:return dns_resolver.create_connection((host,port),timeout)
    deadline=time.monotonic()+timeout
    failure=None
    for family,kind,proto,_,endpoint in dns_resolver.getaddrinfo(host,port,socket.AF_UNSPEC,socket.SOCK_STREAM,timeout=min(dns_resolver.TIMEOUT,timeout)):
        sock=None
        try:
            binding=interface_probes.binding_for(identity,endpoint[0])
            sock=socket.socket(family,kind,proto)
            v6=family==socket.AF_INET6
            if platform.system()=='Darwin':sock.setsockopt(socket.IPPROTO_IPV6 if v6 else socket.IPPROTO_IP,125 if v6 else 25,binding['index'])
            elif platform.system()=='Windows':sock.setsockopt(socket.IPPROTO_IPV6 if v6 else socket.IPPROTO_IP,31,binding['index'] if v6 else struct.pack('!I',binding['index']))
            else:raise interface_probes.InterfaceUnavailable('HTTP-привязка поддерживается на macOS и Windows')
            scope=binding['index'] if v6 and ipaddress.ip_address(endpoint[0].split('%')[0]).is_link_local else 0
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Истекло время подключения')
            sock.settimeout(remaining);sock.bind((binding['source'],0,0,scope) if v6 else (binding['source'],0))
            if scope:endpoint=(endpoint[0].split('%')[0],endpoint[1],endpoint[2],scope)
            sock.connect(endpoint);return sock
        except OSError as exc:
            failure=exc
            if sock:sock.close()
    raise failure or OSError('Нет подходящего адреса для выбранного адаптера')

def adapter(identity,ssl_context=None):
    class Connection(HTTPSConnection):
        def request(self,*args,**kwargs):
            _register(self);return super().request(*args,**kwargs)
        def _new_conn(self):
            try:return connect(self._dns_host,self.port,identity,self.timeout)
            except OSError as exc:raise NewConnectionError(self,str(exc)) from exc
    class Pool(HTTPSConnectionPool):ConnectionCls=Connection
    class PlainConnection(HTTPConnection):
        def request(self,*args,**kwargs):
            _register(self);return super().request(*args,**kwargs)
        def _new_conn(self):
            try:return connect(self._dns_host,self.port,identity,self.timeout)
            except OSError as exc:raise NewConnectionError(self,str(exc)) from exc
    class PlainPool(HTTPConnectionPool):ConnectionCls=PlainConnection
    class Adapter(requests.adapters.HTTPAdapter):
        def send(self,*args,**kwargs):
            timeout=kwargs.get('timeout')
            parts=timeout if isinstance(timeout,tuple) else (timeout,)
            budget=sum(p for p in parts if isinstance(p,(int,float)) and p>0) or 15
            with Deadline(budget):return super().send(*args,**kwargs)
        def init_poolmanager(self,*args,**kwargs):
            if ssl_context is not None:kwargs['ssl_context']=ssl_context
            super().init_poolmanager(*args,**kwargs)
            self.poolmanager.pool_classes_by_scheme=dict(self.poolmanager.pool_classes_by_scheme,https=Pool,http=PlainPool)
        def proxy_manager_for(self,*args,**kwargs):
            if ssl_context is not None:kwargs['ssl_context']=ssl_context
            manager=super().proxy_manager_for(*args,**kwargs)
            manager.pool_classes_by_scheme=dict(manager.pool_classes_by_scheme,https=Pool,http=PlainPool)
            return manager
    return Adapter()

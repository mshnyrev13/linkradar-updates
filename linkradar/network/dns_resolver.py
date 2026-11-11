"""Bound OS DNS latency and in-flight lookups; never choose another interface.

The OS resolver cannot be killed portably. At most four daemon threads can
remain in it. Repeated requests share an unfinished lookup instead of leaking
one thread on every measurement tick.
"""
import socket
import ipaddress
import threading
import time

_lock=threading.Lock()
_jobs={}
LIMIT=4
TIMEOUT=3

def getaddrinfo(host,port,family,kind,timeout=None):
    try:ipaddress.ip_address(host.split('%')[0])
    except ValueError:pass
    else:
        # Numeric probes must remain usable even if every DNS worker is busy.
        return socket.getaddrinfo(host,port,family,kind,0,socket.AI_NUMERICHOST)
    key=(host,port,family,kind)
    with _lock:
        for old,job in list(_jobs.items()):
            if job['done'].is_set():del _jobs[old]
        job=_jobs.get(key)
        if job is None:
            if len(_jobs)>=LIMIT:raise socket.gaierror('DNS занят: предыдущие запросы ещё не завершены')
            job={'done':threading.Event()};_jobs[key]=job
            def lookup():
                try:job['result']=socket.getaddrinfo(host,port,family,kind)
                except Exception as exc:job['error']=exc
                finally:job['done'].set()
            threading.Thread(target=lookup,name='network-dns',daemon=True).start()
    if not job['done'].wait(TIMEOUT if timeout is None else timeout):raise socket.gaierror('DNS не ответил за отведённое время')
    if 'error' in job:raise job['error']
    return job['result']

def create_connection(address,timeout=3):
    timeout=timeout if isinstance(timeout,(int,float)) and timeout>0 else TIMEOUT
    host,port=address;deadline=time.monotonic()+timeout;failure=None
    answers=getaddrinfo(host,port,socket.AF_UNSPEC,socket.SOCK_STREAM,timeout=min(TIMEOUT,timeout))
    for family,kind,proto,_,endpoint in answers:
        sock=None
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Истекло время подключения')
            sock=socket.socket(family,kind,proto);sock.settimeout(remaining);sock.connect(endpoint);return sock
        except OSError as exc:
            failure=exc
            if sock:sock.close()
    raise failure or OSError('Нет адресов для подключения')

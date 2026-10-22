"""HTTP goodput to Yandex Internetometer CDN, using its public probe configuration.
Synthetic data only. No cookies, local files, telemetry reports or private credentials.
"""
import concurrent.futures
from collections import Counter
import json
import os
import threading
import time
import statistics
import math
import requests
from urllib3.exceptions import HTTPError as TransportError
import urllib.error
import urllib.parse
import urllib.request
import certifi
import bound_http

CONFIG_URL='https://yandex.ru/internet/api/v0/get-probes'
MAX_BYTES=8*1024*1024*1024  # safety limit; short capped measurements are labelled


def valid_url(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme!='https' or not p.hostname or not p.hostname.endswith('.cdn.yandex.net') or p.username or p.password or p.port not in (None,443):
        raise ValueError('Сервис вернул неподдерживаемый сервер измерения')
    return url


def probes(config,direction):
    rows=config.get(direction,{}).get('probes',[])
    urls=[]
    for row in rows:
        if row.get('timeout'):continue  # tiny probes deliberately aborted by the reference site
        for key in ['url','fallbackUrl']:
            if row.get(key):
                url=valid_url(row[key])
                if url not in urls:urls.append(url)
    if not urls:raise ValueError('Нет серверов для измерения '+direction)
    return urls


class HttpResponse:
    def __init__(self,response,timeout=4,stop=None):self.response=response;self.status=response.status_code;self.headers=response.headers;self.timeout=timeout;self.stop=stop
    def read(self,n):
        with bound_http.Deadline(self.timeout,self.response,self.stop):return self.response.raw.read(n)
    def read1(self,n):
        with bound_http.Deadline(self.timeout,self.response,self.stop):return self.response.raw.read1(n)
    def __enter__(self):return self
    def __exit__(self,*args):self.response.close()


def summarize_rates(rates):
    if not rates:return {'p10':None,'p90':None,'variation':None,'stable':False}
    ordered=sorted(rates);mean=statistics.mean(rates)
    variation=statistics.pstdev(rates)/mean if mean else 0
    return {'p10':round(ordered[int((len(ordered)-1)*.1)],2), 'p90':round(ordered[math.ceil((len(ordered)-1)*.9)],2),
            'variation':round(variation,3), 'stable':variation<=.15}


class Cancelled(Exception):pass


class UploadBody:
    """Known-length multipart stream; never allocate a whole 16 MiB probe.

    Requests uses __len__ for Content-Length. The CDN receives the same multipart
    format as before; confirmed payload bytes exclude this framing.
    """
    def __init__(self,size,boundary):
        self.size=size
        self.prefix=('--'+boundary+'\r\nContent-Disposition: form-data; name="data"; filename="probe.bin"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        self.suffix=('\r\n--'+boundary+'--\r\n').encode()
        self.chunk=os.urandom(min(size,65536))
    def __len__(self):return len(self.prefix)+self.size+len(self.suffix)
    def __iter__(self):
        yield self.prefix
        remaining=self.size
        while remaining:
            data=self.chunk[:min(remaining,len(self.chunk))]
            yield data;remaining-=len(data)
        yield self.suffix


class SpeedTest:
    def __init__(self, stop=None, progress=None, seconds=15, parallel=4, interface=None):
        self.stop=stop or threading.Event();self.progress=progress or (lambda x:None)
        self.seconds=seconds;self.parallel=parallel;self.local=threading.local();self.interface=interface
    def open(self, request, timeout=4):
        if self.stop.is_set():raise Cancelled()
        session=getattr(self.local,'session',None)
        if session is None:
            self.local.session=session=requests.Session()
            session.mount('https://',bound_http.adapter(self.interface))
            session.mount('http://',bound_http.adapter(self.interface))
        response=session.request(request.get_method(),request.full_url,data=request.data,headers=dict(request.header_items()),timeout=timeout,stream=True,verify=certifi.where(),allow_redirects=False)
        if 300<=response.status_code<400:
            response.close();raise ValueError('Сервер измерения вернул перенаправление; тест остановлен')
        try:response.raise_for_status()
        except BaseException:response.close();raise
        return HttpResponse(response,timeout,self.stop)
    def config(self):
        request=urllib.request.Request(CONFIG_URL,headers={'User-Agent':'LinkRadar','Accept':'application/json','Cache-Control':'no-cache'})
        with self.open(request,6) as r:
            raw=r.read(1024*1024+1)
            if len(raw)>1024*1024:raise ValueError('Некорректная конфигурация сервиса')
            return json.loads(raw)
    def phase(self, direction, urls):
        lock=threading.Lock();start=time.perf_counter();warm=start+min(3,self.seconds/5);deadline=warm+self.seconds
        counted=0;transferred=0;errors=[];limit=False;finished=threading.Event();buckets=[0.0]*math.ceil(self.seconds)
        def count(size,begin=None):
            nonlocal counted,transferred,limit
            now=time.perf_counter()
            with lock:
                transferred+=size
                if begin is None:
                    if warm<=now<=deadline:
                        counted+=size;buckets[min(len(buckets)-1,int(now-warm))]+=size
                else:
                    left=max(warm,begin);right=min(deadline,now)
                    if right>left:
                        rate=size/max(.000001,now-begin);counted+=rate*(right-left)
                        for bucket in range(max(0,int(left-warm)),min(len(buckets),math.ceil(right-warm))):
                            overlap=max(0,min(right,warm+bucket+1)-max(left,warm+bucket))
                            buckets[bucket]+=rate*overlap
                if transferred>=MAX_BYTES:limit=True;finished.set()
        def transfer_body(index):
            # A 1 MiB first block on each of four connections needs ~34 s at
            # 1 Mbit/s. It times out before the first ACK, so adaptation never
            # starts. Begin small, then grow from confirmed transaction times.
            size=16*1024;attempt=0;failures=0
            while not self.stop.is_set() and not finished.is_set() and time.perf_counter()<deadline:
                url=urls[(index+attempt)%len(urls)];attempt+=1
                try:
                    headers={'User-Agent':'LinkRadar','Cache-Control':'no-cache','Accept-Encoding':'identity'}
                    begin=time.perf_counter()
                    if direction=='download':
                        with self.open(urllib.request.Request(url,headers=headers)) as response:
                            mime=response.headers.get('Content-Type','').lower()
                            if response.status!=200 or 'text/html' in mime or 'application/json' in mime:raise ValueError('Сервер не вернул тестовые данные')
                            while not self.stop.is_set() and not finished.is_set() and time.perf_counter()<deadline:
                                data=response.read1(65536)
                                if not data:break
                                count(len(data))
                    else:
                        boundary='NetworkWatchProbe'
                        body=UploadBody(size,boundary)
                        headers['Content-Type']='multipart/form-data; boundary='+boundary
                        # Meter only successfully acknowledged complete payloads, never socket-buffer writes.
                        with self.open(urllib.request.Request(url,data=body,headers=headers,method='POST')) as response:
                            ack=response.read(1024)
                            if response.status!=200 or ack.strip()!=b'OK':raise ValueError('Сервер не подтвердил приём тестового блока')
                        count(size,begin)
                        duration=time.perf_counter()-begin
                        size=max(8*1024,min(16*1024*1024,int(size*1.2/max(.05,duration))))
                except Cancelled:return
                except (OSError,ValueError,urllib.error.URLError,requests.RequestException,TransportError) as exc:
                    failures+=1
                    with lock:errors.append(type(exc).__name__)
                    if failures>=len(urls)+2:return
        def transfer(index):
            try:transfer_body(index)
            finally:
                session=getattr(self.local,'session',None)
                if session:session.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.parallel) as pool:
            tasks=[pool.submit(transfer,i) for i in range(self.parallel)]
            while not all(t.done() for t in tasks):
                if self.stop.is_set():finished.set()
                if time.perf_counter()>=deadline:finished.set()
                with lock:
                    elapsed=max(0,min(time.perf_counter(),deadline)-warm);rate=counted*8/elapsed/1e6 if elapsed else 0
                self.progress({'phase':direction,'mbps':rate,'progress':min(1,max(0,(time.perf_counter()-warm)/self.seconds)), 'warmup':time.perf_counter()<warm, 'total_progress':((0 if direction=='download' else 1)+min(1,max(0,(time.perf_counter()-start)/(deadline-start))))/2})
                if self.stop.wait(.2):finished.set()
            for task in tasks:task.result()
        if self.stop.is_set():raise Cancelled()
        duration=max(0,min(time.perf_counter(),deadline)-warm)
        if not counted or duration<.1:raise ValueError('Измерение '+('загрузки' if direction=='download' else 'отдачи')+' не удалось: сервер недоступен или блокирует запросы')
        rates=[b*8/1e6 for b in buckets[:int(duration)]]
        summary=summarize_rates(rates)
        return {'mbps':round(counted*8/duration/1e6,2),'bytes':transferred,
                'measurement_bytes':round(counted,3),'seconds':round(duration,2),
                'warmup_seconds':round(warm-start,3),'wall_seconds':round(time.perf_counter()-start,3),
                'limited':limit,'partial':bool(errors),'request_errors':dict(Counter(errors)),**summary}
    def run(self):
        self.progress({'phase':'config','progress':0})
        try:config=self.config()
        except Cancelled:raise
        except (OSError,ValueError,urllib.error.URLError,requests.RequestException,TransportError):raise ValueError('Не удалось получить серверы интернетометра Яндекса') from None
        finally:
            session=getattr(self.local,'session',None)
            if session:session.close();self.local.session=None
        result={'interface':self.interface,'dns':'OS resolver','provider':'Яндекс CDN',
                'parallel':self.parallel,'method':'HTTP payload; upload confirmed by server',
                'download':None,'upload':None,'errors':{},'ts':time.time()}
        for direction in ['download','upload']:
            try:result[direction]=self.phase(direction,probes(config,direction))
            except Cancelled:raise
            except ValueError as exc:result['errors'][direction]=str(exc)
        return result

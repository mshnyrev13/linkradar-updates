"""Read a public, data-only update channel. Never download or execute software."""
from dataclasses import dataclass
import json
import re
import ssl
import threading
import time
from urllib.parse import urlsplit
from urllib.request import build_opener,HTTPSHandler,HTTPRedirectHandler,Request
import certifi
import truststore
from PySide6.QtCore import QObject,QThread,QTimer,Signal

FEED_URL='https://raw.githubusercontent.com/mshnyrev13/linkradar-updates/main/latest.json'
MAX_BYTES=16*1024
CHECK_INTERVAL_MS=6*60*60*1000


def version_key(value):
    """Stable release versions only; compare numbers, never strings or floats."""
    if not isinstance(value,str) or not re.fullmatch(r'v?\d{1,6}\.\d{1,6}\.\d{1,6}',value):
        raise ValueError('Некорректный номер версии в канале обновлений')
    return tuple(int(part) for part in value.lstrip('v').split('.'))


def download_link(value):
    """Allow only the owner's release pages or a public Disk share."""
    if not isinstance(value,str) or len(value)>512 or any(ord(c)<=32 for c in value) or '\\' in value:
        raise ValueError('Некорректная ссылка на обновление')
    url=urlsplit(value)
    if url.scheme!='https' or url.username or url.password or url.port not in (None,443) or url.query or url.fragment:
        raise ValueError('Недопустимая ссылка на обновление')
    if url.hostname=='github.com' and re.fullmatch(r'/mshnyrev13/(?:vcs-network-watch|linkradar-updates)/releases/(?:latest|tag/v?\d+\.\d+\.\d+)/?',url.path):
        return value
    if url.hostname=='disk.yandex.ru' and re.fullmatch(r'/d/[A-Za-z0-9_-]+/?',url.path):
        return value
    raise ValueError('Неизвестный источник обновления')


@dataclass(frozen=True)
class Release:
    version:str
    url:str


def parse_feed(payload,current):
    if not isinstance(payload,bytes) or len(payload)>MAX_BYTES:
        raise ValueError('Слишком большой ответ канала обновлений')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('Повторяющееся поле в канале обновлений')
            result[key]=value
        return result
    try:data=json.loads(payload,object_pairs_hook=unique)
    except (ValueError,RecursionError) as exc:raise ValueError('Не удалось прочитать канал обновлений') from exc
    if not isinstance(data,dict):raise ValueError('Некорректный канал обновлений')
    version=data.get('version');remote=version_key(version);local=version_key(current)
    url=download_link(data.get('download_url'))
    return Release(version.lstrip('v'),url) if remote>local else None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('Канал обновлений перенаправил запрос')


def fetch_feed(stop):
    context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(certifi.where())
    opener=build_opener(HTTPSHandler(context=context),NoRedirect())
    request=Request(FEED_URL,headers={'Accept':'application/json','User-Agent':'LinkRadar-update-check'})
    if stop.is_set():return b''
    deadline=time.monotonic()+15
    with opener.open(request,timeout=5) as response:
        if response.status!=200:raise ValueError('Канал обновлений недоступен')
        chunks=[];size=0
        while not stop.is_set():
            if time.monotonic()>deadline:raise TimeoutError('Проверка обновлений заняла слишком много времени')
            chunk=response.read1(min(4096,MAX_BYTES+1-size))
            if not chunk:return b''.join(chunks)
            size+=len(chunk)
            if size>MAX_BYTES:raise ValueError('Слишком большой ответ канала обновлений')
            chunks.append(chunk)
    return b''


class CheckWorker(QThread):
    def __init__(self,current,fetcher=fetch_feed):
        super().__init__();self.current=current;self.fetcher=fetcher;self.stop=threading.Event();self.result=None;self.error=None
    def run(self):
        try:
            payload=self.fetcher(self.stop)
            if not self.stop.is_set():self.result=parse_feed(payload,self.current)
        except Exception:
            # Do not expose proxy URLs, credentials, or raw server text in the UI.
            self.error='Не удалось проверить обновления. Проверьте доступ к интернету и попробуйте ещё раз.'


class UpdateChecker(QObject):
    available=Signal(object)
    checked=Signal(bool)
    failed=Signal(str,bool)
    busyChanged=Signal(bool)
    def __init__(self,current,parent=None,fetcher=fetch_feed):
        super().__init__(parent);self.current=current;self.fetcher=fetcher;self.worker=None;self.closed=False;self.manual=False;self.announced=set()
        self.timer=QTimer(self);self.timer.setInterval(CHECK_INTERVAL_MS);self.timer.timeout.connect(self.check)
        self.initial=QTimer(self);self.initial.setSingleShot(True);self.initial.timeout.connect(self.check)
    def start(self):
        self.initial.start(2500);self.timer.start()
    def check(self,manual=False):
        if self.closed:return
        if self.worker:
            self.manual=self.manual or manual;return
        self.manual=manual;self.busyChanged.emit(True)
        self.worker=CheckWorker(self.current,self.fetcher);self.worker.finished.connect(self.finished);self.worker.start()
    def finished(self):
        worker=self.worker
        if worker is None:return
        manual=self.manual;self.worker=None;worker.deleteLater();self.busyChanged.emit(False)
        if self.closed:return
        if worker.error:self.failed.emit(worker.error,manual)
        elif worker.result:
            if manual or worker.result.version not in self.announced:
                self.announced.add(worker.result.version);self.available.emit(worker.result)
        else:self.checked.emit(manual)
    def shutdown(self):
        self.closed=True;self.initial.stop();self.timer.stop()
        if self.worker:self.worker.stop.set()
    def busy(self):return bool(self.worker and self.worker.isRunning())

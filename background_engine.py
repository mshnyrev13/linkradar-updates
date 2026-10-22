"""Headless engine for launchd/Task Scheduler; authenticated local IPC, no web UI."""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
import stat
from multiprocessing.connection import Client,Listener,deliver_challenge,answer_challenge
from multiprocessing.context import AuthenticationError
import error_log
import server
import service_paths
import session_export
import export_transfer
import ipc_transport

# Session data the GUI may add through IPC; engine_state stays internal to the engine.
CLIENT_KINDS = {'network','speed','graphene_source','graphene'}
RECORD_BATCH = 2000
GUI_CAPABILITIES = {'clear', 'retention_12h', 'bound_interface', 'session_data', 'resume_session', 'stream_export', 'compact_snapshot', 'bounded_frames'}
AUTH_SECONDS = 5


def current(status):
    """Protocol compatibility alone does not mean the running probe code is current."""
    return (status.get('version') == server.VERSION
            and GUI_CAPABILITIES.issubset(status.get('capabilities', [])))


def version_error(status):
    return ('Версия фонового движка '+str(status.get('version') or 'неизвестна')+
            ' не совпадает с LinkRadar '+server.VERSION+'. '
            'На Mac откройте обновлённый LinkRadar из Applications для обновления службы; '
            'на Windows повторите установку фонового режима из нового комплекта. '
            'Измерения старым движком не запускаются.')

def address(folder):
    if os.name=='nt':return r'\\.\pipe\VCSNetworkWatch-'+hashlib.sha256(str(folder).lower().encode()).hexdigest()[:20],'AF_PIPE'
    original=str(Path(folder).resolve()/'service.sock')
    if len(original.encode())<100:return original,'AF_UNIX'
    # macOS sockaddr_un holds only ~104 bytes; long profile/user names must
    # still work. The alternate socket lives in an owner-only, non-link folder.
    root=Path('/private/tmp' if sys.platform=='darwin' else '/tmp')/('linkradar-ipc-'+str(os.getuid()))
    root.mkdir(mode=0o700,exist_ok=True)
    info=root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise OSError('Небезопасная папка IPC; запуск остановлен')
    name=hashlib.sha256(str(Path(folder).resolve()).encode()).hexdigest()[:32]
    return str(root/(name+'.sock')),'AF_UNIX'

class AuthDeadline:
    def __init__(self,connection):self.connection=connection
    def __getattr__(self,name):return getattr(self.connection,name)
    def recv_bytes(self,*args,**kwargs):
        deadline=time.monotonic()+AUTH_SECONDS
        if not self.connection.poll(AUTH_SECONDS):raise TimeoutError('Таймаут авторизации IPC')
        return self.connection.recv_bytes(*args,timeout=max(.001,deadline-time.monotonic()),**kwargs)
    def send_bytes(self,*args,**kwargs):
        return self.connection.send_bytes(*args,timeout=AUTH_SECONDS,**kwargs)

def client(folder):
    key=read_key(folder);where,family=address(folder)
    deadline=time.monotonic()+1
    while True:
        connection=None
        try:
            connection=ipc_transport.Connection(Client(where,family=family,authkey=None))
            bounded=AuthDeadline(connection);answer_challenge(bounded,key);deliver_challenge(bounded,key)
            return connection
        except (ConnectionRefusedError,ConnectionResetError,BrokenPipeError,EOFError):
            if connection:connection.close()
            # No RPC has been sent, so retrying cannot repeat a mutation.
            if time.monotonic()>=deadline:raise
            time.sleep(.05)
        except BaseException:
            if connection:connection.close()
            raise

def request(folder,command,_stop=None,**args):
    with client(folder) as connection:
        connection.cancel=_stop
        connection.send_bytes(json.dumps({'command':command,**args}).encode())
        export_transfer.wait(connection,15,_stop)
        frame=connection.recv_bytes(64*1024*1024)
        if frame==b'S':
            output=io.BytesIO();export_transfer.receive(connection,output);return output.getvalue().decode('utf-8')
        response=json.loads(frame)
        if not isinstance(response,dict) or not ('result' in response or isinstance(response.get('error'),str)):
            raise ValueError('Некорректный ответ IPC')
    if 'error' in response:raise RuntimeError(response['error'])
    return response['result']

class Proxy:
    def __init__(self,folder,status):
        self.folder=folder;self.sid=status['active_id'];self.status=status;self.stop_event=threading.Event()
    def start(self,config):
        self.sid=request(self.folder,'start',config=config);self.stop_event.clear();return self.sid
    def stop(self):request(self.folder,'stop');self.stop_event.set()
    def clear(self):request(self.folder,'clear');self.sid=None;self.stop_event.set()
    def pause(self,sid):return request(self.folder,'pause',sid=sid)
    def set_interface(self,identity):return request(self.folder,'interface',identity=identity)
    def set_interval(self,seconds):return request(self.folder,'interval',seconds=seconds)
    def resume(self,sid,paused=False):
        self.sid=request(self.folder,'resume',sid=sid,paused=paused);return self.sid
    def snapshot(self,sid,window,**kwargs):
        cancel=kwargs.pop('cancel',None)
        result=request(self.folder,'snapshot',sid=sid,window=window,_stop=cancel,**kwargs)
        if not kwargs.get('compact'):self.sid=result['active_id']
        return result
    # The GUI never opens the engine database: on Windows it is readable only by SYSTEM.
    def record(self,rows):
        for i in range(0,len(rows),RECORD_BATCH):
            for attempt in range(3):
                try:request(self.folder,'record',rows=rows[i:i+RECORD_BATCH]);break
                except (OSError,EOFError):
                    # INSERT OR REPLACE uses the same session/kind/key/ts on
                    # retry, including the case where only the reply was lost.
                    if attempt==2:raise
                    time.sleep(.1)
    def event(self,sid,message):return request(self.folder,'event',sid=sid,message=message)
    def export(self,sid,names=None):return request(self.folder,'export',sid=sid,names=names or {})
    def export_to(self,sid,path,names=None,stop=None,prefix=''):
        def write(output):
            extra=prefix.encode('utf-8');output.write(extra)
            with client(self.folder) as connection:
                connection.cancel=stop
                connection.send_bytes(json.dumps({'command':'export_stream','sid':sid,'names':names or {}}).encode())
                export_transfer.wait(connection,15,stop)
                frame=connection.recv_bytes(export_transfer.CHUNK+1)
                if frame!=b'S':raise RuntimeError(json.loads(frame).get('error','Ошибка экспорта'))
                return export_transfer.receive(connection,output,stop)+len(extra)
        return export_transfer.atomic_write(path,write)


def connect(folder):
    if not (Path(folder)/'service.key').exists():return None
    try:return Proxy(folder,request(folder,'status'))
    except (OSError,EOFError,ValueError,RuntimeError,AuthenticationError):return None

def read_key(folder):
    descriptor=os.open(Path(folder)/'service.key',os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
    try:
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size!=32:
            raise ValueError('Некорректный ключ IPC')
        if os.name!='nt' and (info.st_uid!=os.getuid() or info.st_mode&0o077):
            raise ValueError('Небезопасные права ключа IPC')
        return os.read(descriptor,33)
    finally:os.close(descriptor)

def session_rows(store,rows):
    """Validate GUI rows [sid,kind,key,ts,data] for existing sessions; return SQL tuples."""
    if not isinstance(rows,list) or len(rows)>RECORD_BATCH:raise ValueError('Некорректные данные сессии')
    clean=[];known=set()
    for row in rows:
        if not isinstance(row,list) or len(row)!=5:raise ValueError('Некорректные данные сессии')
        sid,kind,key,ts,data=row
        if (not isinstance(sid,str) or not isinstance(kind,str) or kind not in CLIENT_KINDS or not isinstance(key,str) or len(key)>512
                or isinstance(ts,bool) or not isinstance(ts,(int,float)) or not math.isfinite(ts)):
            raise ValueError('Некорректные данные сессии')
        if sid not in known:
            if not store.rows('SELECT 1 FROM sessions WHERE id=?',(sid,)):raise ValueError('Сессия не найдена')
            known.add(sid)
        clean.append((sid,kind,key,float(ts),json.dumps(data,ensure_ascii=False)))
    return clean

def run(folder,stop=None,user_sid=None):
    """Serve the engine; with user_sid (Windows SYSTEM service) the folder is locked first."""
    folder=Path(folder);stop=stop or threading.Event();removed=[]
    if user_sid:removed=service_paths.prepare(folder,user_sid)
    else:service_paths.private_profile(folder)
    # In-process runs (tests) close the journal they opened; a service process just exits.
    journal=error_log.path() is None;error_log.setup(folder,'service')
    if removed:error_log.logger.warning('В папке службы удалены небезопасные записи: %s',', '.join(removed))
    lock=os.fdopen(os.open(folder/'service.lock',os.O_RDWR|os.O_CREAT,0o600),'r+b')
    if os.name=='nt':
        import msvcrt
        lock.write(b'0');lock.flush();lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    store=server.Store(folder/'network.sqlite3',recover=False,retention_seconds=server.RETENTION_SECONDS);monitor=server.Monitor(store)
    active=store.rows("SELECT id FROM sessions WHERE status='running' ORDER BY started DESC LIMIT 1")
    if active:
        try:monitor.resume(active[0]['id'])
        except ValueError:store.execute("UPDATE sessions SET status='interrupted',ended=? WHERE id=?",(time.time(),active[0]['id']))
    keyfile=folder/'service.key'
    if user_sid:service_paths.write_key(folder,user_sid)
    elif not keyfile.exists():
        fd=os.open(keyfile,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as f:f.write(os.urandom(32))
    where,family=address(folder)
    if family=='AF_PIPE' and user_sid:
        import windows_pipe
        windows_pipe.configure(user_sid)
    if family=='AF_UNIX' and Path(where).exists():Path(where).unlink()
    authkey=read_key(folder)
    # Authentication belongs to a bounded worker. One client that connects and
    # never answers must not block the listener for every other GUI.
    listener=Listener(where,family=family,authkey=None,backlog=32)
    if family=='AF_UNIX':os.chmod(where,0o600)
    slots=threading.BoundedSemaphore(8)
    active={};active_lock=threading.Lock()
    def handle(connection):
        try:
            with connection:
                connection.cancel=stop
                bounded=AuthDeadline(connection);deliver_challenge(bounded,authkey);answer_challenge(bounded,authkey)
                if not connection.poll(10):return
                req=json.loads(connection.recv_bytes(4*1024*1024,timeout=10))
                if not isinstance(req,dict) or not isinstance(req.get('command'),str):
                    raise ValueError('Ожидается команда IPC в объекте JSON')
                command=req['command']
                try:
                    if command=='status':
                        sessions=store.rows('SELECT config FROM sessions WHERE id=?',(monitor.sid,))
                        result={'active_id':monitor.sid,'config':json.loads(sessions[0]['config']) if sessions else None,'paused':monitor.paused,'version':server.VERSION,'capabilities':['pause','interval',*sorted(GUI_CAPABILITIES)],'pid':os.getpid(),'executable':sys.executable}
                    elif command=='start':result=monitor.start(req['config'])
                    elif command=='resume':
                        sid,paused=req.get('sid'),req.get('paused',False)
                        if not isinstance(sid,str) or not isinstance(paused,bool):raise ValueError('Некорректная сессия восстановления')
                        monitor.resume(sid,paused);result=monitor.sid
                    elif command=='stop':monitor.stop();result=True
                    elif command=='clear':monitor.clear();result=True
                    elif command=='pause':result=monitor.pause(req['sid'])
                    elif command=='interface':monitor.set_interface(req.get('identity'));result=True
                    elif command=='interval':monitor.set_interval(req['seconds']);result=True
                    elif command=='snapshot':result=monitor.snapshot(req['sid'],req['window'],event_limit=None,compact=bool(req.get('compact')),selected_node=req.get('selected_node'),event_after=max(0,int(req.get('event_after',0))),cancel=stop)
                    elif command in ('export','export_stream'):
                        names=req.get('names') or {}
                        if not isinstance(names,dict) or len(names)>10000 or not all(isinstance(k,str) and (v is None or isinstance(v,str)) for k,v in names.items()):raise ValueError('Некорректные имена узлов')
                        connection.send_bytes(b'S')
                        sender=export_transfer.Sender(connection)
                        try:
                            session_export.write_txt(store,req['sid'],sender,{**names,**monitor.ptr_names},stop)
                            sender.finish()
                        except (OSError,EOFError):raise
                        except Exception as exc:
                            connection.send_bytes(b'X'+error_log.scrub(str(exc))[:1000].encode())
                        return
                    elif command=='record':
                        rows=session_rows(store,req.get('rows'))
                        with store.lock:
                            store.db.executemany('INSERT OR REPLACE INTO session_data VALUES (?,?,?,?,?)',rows);store.db.commit();store.prune(force=True)
                        result=len(rows)
                    elif command=='event':
                        sid,message=req.get('sid'),req.get('message')
                        if not isinstance(sid,str) or not isinstance(message,str) or not message or len(message)>500:raise ValueError('Некорректное событие')
                        if not store.rows('SELECT 1 FROM sessions WHERE id=?',(sid,)):raise ValueError('Сессия не найдена')
                        store.event(sid,'','info',message);result=True
                    else:raise ValueError('Неизвестная команда службы')
                    response={'result':result}
                except Exception as exc:
                    # ValueError carries a user-facing refusal; anything else is a defect.
                    if not isinstance(exc,ValueError):error_log.error('Команда службы '+str(command)+' завершилась ошибкой',exc)
                    response={'error':error_log.scrub(str(exc))}
                connection.send_bytes(json.dumps(response,ensure_ascii=False).encode())
        except (BrokenPipeError,ConnectionResetError,EOFError):pass  # a client may cancel a read/export
        except (OSError,ValueError,RecursionError,AuthenticationError) as exc:error_log.error('Ошибка IPC службы',exc,key='ipc')
        finally:
            with active_lock:active.pop(connection,None)
            slots.release()
    def accept():
        while not stop.is_set():
            try:
                connection=ipc_transport.Connection(listener.accept())
                if not slots.acquire(blocking=False):connection.close();continue
                worker=threading.Thread(target=handle,args=(connection,),daemon=True)
                with active_lock:active[connection]=worker
                worker.start()
            except (OSError,EOFError,AuthenticationError):
                if stop.is_set():break
                time.sleep(.1)
    threading.Thread(target=accept,daemon=True).start()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    try:
        while not stop.wait(.5):store.prune()
    finally:
        listener.close();monitor.stop();monitor.power.stop()
        with active_lock:pending=list(active.items())
        for connection,_ in pending:connection.shutdown()
        for _,worker in pending:worker.join(5)
        deadline=time.monotonic()+6
        while monitor.sid and time.monotonic()<deadline:time.sleep(.05)
        if family=='AF_UNIX' and Path(where).exists():Path(where).unlink()
        # Windows keeps an open SQLite file locked; close it before releasing the
        # service lock so a new process may reopen the database.
        with store.lock:store.db.close()
        lock.close()
        if journal:error_log.shutdown()

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',required=True);args=parser.parse_args();run(args.data_dir)

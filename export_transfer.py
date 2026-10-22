"""Bounded IPC frames and atomic local files; service never accepts a client path."""
import hashlib
import json
import os
import time
from pathlib import Path
import tempfile

CHUNK=256*1024

class Sender:
    def __init__(self,connection):
        self.connection=connection;self.buffer=bytearray();self.hash=hashlib.sha256();self.size=0
    def write(self,text):
        data=text.encode('utf-8');self.buffer.extend(data)
        while len(self.buffer)>=CHUNK:self.flush(CHUNK)
    def flush(self,n=None):
        if not self.buffer:return
        n=n or len(self.buffer);part=bytes(self.buffer[:n]);del self.buffer[:n]
        self.connection.send_bytes(b'D'+part);self.hash.update(part);self.size+=len(part)
    def finish(self):
        self.flush();self.connection.send_bytes(b'E'+json.dumps({'bytes':self.size,'sha256':self.hash.hexdigest()}).encode())

def wait(connection,seconds,stop=None):
    deadline=time.monotonic()+seconds
    while True:
        if stop and stop.is_set():raise InterruptedError('Операция отменена')
        if connection.poll(min(.1,max(0,deadline-time.monotonic()))):return
        if time.monotonic()>=deadline:raise TimeoutError('Служба не ответила')

def receive(connection,output,stop=None):
    digest=hashlib.sha256();size=0
    while True:
        if stop and stop.is_set():raise InterruptedError('Экспорт отменён')
        wait(connection,30,stop)
        frame=connection.recv_bytes(CHUNK+1)
        if not frame:raise ValueError('Оборванный экспорт')
        if frame[:1]==b'D':
            output.write(frame[1:]);digest.update(frame[1:]);size+=len(frame)-1
        elif frame[:1]==b'E':
            end=json.loads(frame[1:])
            if end!={'bytes':size,'sha256':digest.hexdigest()}:raise ValueError('Контрольная сумма экспорта не совпала')
            return size
        elif frame[:1]==b'X':raise RuntimeError(frame[1:].decode())
        else:raise ValueError('Неизвестный блок экспорта')

def atomic_write(path,writer):
    target=Path(path);temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,prefix='.'+target.name+'-',suffix='.tmp',delete=False) as output:
            temporary=Path(output.name);result=writer(output);output.flush();os.fsync(output.fileno())
        os.replace(temporary,target);return result
    finally:
        if temporary:temporary.unlink(missing_ok=True)

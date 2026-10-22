"""Ordered background I/O for Qt. Callbacks are delivered in the GUI thread."""
import queue
import threading
from PySide6.QtCore import QThread,Signal

class IOQueue(QThread):
    completed=Signal(object)
    def __init__(self):
        super().__init__();self.jobs=queue.Queue(maxsize=64);self.stopping=threading.Event();self.data_error=None
    def submit(self,function,callback=None,data=False):
        if self.stopping.is_set():raise RuntimeError('Приложение завершается')
        self.jobs.put_nowait((function,callback,data))
    def barrier(self):
        event=threading.Event();self.submit(event.set);return event
    def shutdown(self):
        self.stopping.set()
        while True:
            try:self.jobs.get_nowait()
            except queue.Empty:break
        self.jobs.put_nowait(None)
    def run(self):
        while not self.stopping.is_set():
            job=self.jobs.get()
            if job is None:return
            function,callback,data=job;result=None;error=None
            try:result=function()
            except Exception as exc:
                error=exc
                if data:self.data_error=exc
            self.completed.emit((callback,result,error))

class Call(QThread):
    def __init__(self,function):super().__init__();self.function=function;self.error=None;self.result=None
    def run(self):
        try:self.result=self.function()
        except Exception as exc:self.error=exc

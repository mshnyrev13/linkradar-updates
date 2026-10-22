"""Prevent idle system sleep during measurement, without keeping the display on."""
import os
import platform
import subprocess
import threading

class PowerGuard:
    def __init__(self):self.process=None;self.thread=None;self.stop_event=threading.Event()
    def start(self):
        if self.process and self.process.poll() is None:return
        if self.thread and self.thread.is_alive():return
        self.stop_event=threading.Event()
        stop=self.stop_event
        if platform.system()=='Darwin':
            self.process=subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        elif platform.system()=='Windows':
            ready=threading.Event();errors=[]
            def hold():
                import ctypes
                api=ctypes.windll.kernel32.SetThreadExecutionState
                if not api(0x80000001):errors.append('Windows не разрешила удержание от сна')
                ready.set();stop.wait();api(0x80000000)
            self.thread=threading.Thread(target=hold,daemon=True);self.thread.start()
            if not ready.wait(3):
                stop.set();raise OSError('Windows не завершила проверку удержания от сна')
            if errors:raise OSError(errors[0])
    def stop(self):
        self.stop_event.set()
        process,self.process=self.process,None
        thread,self.thread=self.thread,None
        if process:
            try:
                if process.poll() is None:process.terminate()
                process.wait(timeout=3)
            except ProcessLookupError:pass
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=3)
        if thread:thread.join(timeout=3)

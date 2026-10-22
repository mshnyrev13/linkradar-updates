"""Multiprocessing-compatible byte frames with whole-frame deadlines; no pickle."""
import os
import socket
import struct
import threading
import time

MAX_FRAME = 64 * 1024 * 1024


class Connection:
    def __init__(self, connection):
        self.connection = connection
        self.sock = None
        self.cancel = None
        if os.name != 'nt':
            self.sock = socket.fromfd(connection.fileno(), socket.AF_UNIX, socket.SOCK_STREAM)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.sock:
            self.sock.close()
        self.connection.close()

    def shutdown(self):
        if self.sock:
            try:self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        elif os.name=='nt':
            import ctypes
            cancel=ctypes.WinDLL('kernel32',use_last_error=True).CancelIoEx
            cancel.argtypes=[ctypes.c_void_p,ctypes.c_void_p];cancel.restype=ctypes.c_int
            cancel(self.connection.fileno(),None)

    def poll(self, timeout=0):
        return self.connection.poll(timeout)

    def _read(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            if self.cancel and self.cancel.is_set():raise InterruptedError('Обмен IPC отменён')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Таймаут чтения IPC')
            self.sock.settimeout(min(.1,remaining))
            try:chunk = self.sock.recv(min(size - len(data), 256 * 1024))
            except socket.timeout:continue
            if not chunk:
                raise EOFError('Оборванный блок IPC')
            data.extend(chunk)
        return data

    def _windows_io(self, function, timeout):
        # PipeConnection waits in overlapped I/O after poll(). CancelIoEx also
        # cancels an incomplete message and is callable from another thread.
        import ctypes
        cancel = ctypes.WinDLL('kernel32', use_last_error=True).CancelIoEx
        cancel.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cancel.restype = ctypes.c_int
        expired = threading.Event();finished=threading.Event();deadline=time.monotonic()+timeout
        def interrupt():
            while not finished.wait(.05):
                if time.monotonic()>=deadline or (self.cancel and self.cancel.is_set()):
                    expired.set();cancel(self.connection.fileno(), None)
        watcher=threading.Thread(target=interrupt,daemon=True);watcher.start()
        try:
            return function()
        except OSError:
            if expired.is_set():
                if self.cancel and self.cancel.is_set():raise InterruptedError('Обмен IPC отменён') from None
                raise TimeoutError('Таймаут обмена IPC') from None
            raise
        finally:
            finished.set();watcher.join()

    def recv_bytes(self, maxlength=None, *, timeout=30):
        limit = min(MAX_FRAME, MAX_FRAME if maxlength is None else maxlength)
        if limit < 0:
            raise ValueError('Некорректный размер блока IPC')
        if self.sock is None:
            return self._windows_io(lambda: self.connection.recv_bytes(limit), timeout)
        deadline = time.monotonic() + timeout
        size = struct.unpack('!i', self._read(4, deadline))[0]
        if size == -1:
            size = struct.unpack('!Q', self._read(8, deadline))[0]
        if not 0 <= size <= limit:
            raise OSError('Превышен размер блока IPC')
        return bytes(self._read(size, deadline))

    def send_bytes(self, data, offset=0, size=None, *, timeout=15):
        payload = memoryview(data).cast('B')
        size = len(payload) - offset if size is None else size
        if offset < 0 or size < 0 or offset + size > len(payload) or size > MAX_FRAME:
            raise ValueError('Некорректный размер блока IPC')
        if self.sock is None:
            return self._windows_io(lambda: self.connection.send_bytes(payload, offset, size), timeout)
        deadline = time.monotonic() + timeout
        for part in (memoryview(struct.pack('!i', size)), payload[offset:offset + size]):
            while part:
                if self.cancel and self.cancel.is_set():raise InterruptedError('Обмен IPC отменён')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Таймаут записи IPC')
                self.sock.settimeout(min(.1,remaining))
                try:count = self.sock.send(part)
                except socket.timeout:continue
                if count == 0:
                    raise EOFError('Канал IPC закрыт')
                part = part[count:]

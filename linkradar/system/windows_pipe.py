"""Windows service pipe ACL: SYSTEM, administrators and the configured local user."""
import re

def configure(user_sid):
    if not re.fullmatch(r'S-1-\d+(?:-\d+)+',user_sid):raise ValueError('Некорректный SID пользователя службы')
    import ctypes
    from ctypes import wintypes
    import _winapi
    import multiprocessing.connection as connection
    class SecurityAttributes(ctypes.Structure):
        _fields_=[('nLength',wintypes.DWORD),('lpSecurityDescriptor',ctypes.c_void_p),('bInheritHandle',wintypes.BOOL)]
    convert=ctypes.WinDLL('advapi32',use_last_error=True).ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(wintypes.DWORD)];convert.restype=wintypes.BOOL
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateNamedPipeW
    create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(SecurityAttributes)];create.restype=wintypes.HANDLE
    free=kernel.LocalFree;free.argtypes=[ctypes.c_void_p];free.restype=ctypes.c_void_p
    class UserPipeListener(connection.PipeListener):
        def _new_handle(self,first=False):
            descriptor=ctypes.c_void_p()
            sddl='D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;'+user_sid+')'
            if not convert(sddl,1,ctypes.byref(descriptor),None):raise ctypes.WinError(ctypes.get_last_error())
            try:
                security=SecurityAttributes(ctypes.sizeof(SecurityAttributes),descriptor,False)
                flags=_winapi.PIPE_ACCESS_DUPLEX|_winapi.FILE_FLAG_OVERLAPPED
                if first:flags|=_winapi.FILE_FLAG_FIRST_PIPE_INSTANCE
                handle=create(self._address,flags,0x4|0x2|0x8,255,connection.BUFSIZE,connection.BUFSIZE,0,ctypes.byref(security))
                if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
                return handle
            finally:free(descriptor)
    connection.PipeListener=UserPipeListener

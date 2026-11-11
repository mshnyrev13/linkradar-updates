"""Windows background engine data: a SYSTEM-only folder and link checks before SYSTEM writes.

The engine runs as SYSTEM. Its data lives in %ProgramData%\\VCS Network Watch\\<id>, where <id>
is derived from the user's SID. Only SYSTEM and administrators may write there; the user may
list the folder and read service.key (IPC authentication) and nothing else. Measurements reach
the GUI only through the authenticated named pipe, so a user cannot redirect a SYSTEM write with
a junction, a symbolic link or a hard link.

Folders are opened without following reparse points and secured through that handle, so the
check and the new owner/DACL apply to the same object. Files the engine opens are refused when
they are a reparse point, have more than one name or (in the service folder) a foreign owner.
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import uuid

ROOT_NAME = 'VCS Network Watch'
SID = re.compile(r'S-1-\d+(?:-\d+)+')
SYSTEM = 'S-1-5-18'
ADMINISTRATORS = 'S-1-5-32-544'
TRUSTED_OWNERS = (SYSTEM, ADMINISTRATORS)


def private_profile(folder):
    """POSIX profiles contain network history and an IPC key: owner access only."""
    folder=Path(folder)
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    if os.name!='nt':
        descriptor=os.open(folder,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0))
        try:
            info=os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid():
                raise OSError('Папка профиля должна принадлежать текущему пользователю и не быть ссылкой')
            os.fchmod(descriptor,0o700)
            if sys.platform=='darwin':
                # Extended allow ACLs may grant access despite POSIX mode 0700.
                # Change only this owned profile, through the checked handle.
                import ctypes
                libc=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
                libc.acl_init.argtypes=[ctypes.c_int];libc.acl_init.restype=ctypes.c_void_p
                libc.acl_set_fd_np.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.c_int];libc.acl_set_fd_np.restype=ctypes.c_int
                libc.acl_free.argtypes=[ctypes.c_void_p]
                acl=libc.acl_init(0)
                if not acl:raise OSError(ctypes.get_errno(),'Не удалось подготовить ACL профиля')
                try:
                    if libc.acl_set_fd_np(descriptor,acl,0x100):raise OSError(ctypes.get_errno(),'Не удалось закрыть ACL профиля')
                finally:libc.acl_free(acl)
        finally:os.close(descriptor)
    return folder
# Everything the engine creates, truncates or rotates in its folder.
SERVICE_FILES = ('network.sqlite3', 'network.sqlite3-wal', 'network.sqlite3-shm', 'network.sqlite3-journal',
                 'service.lock', 'service.key', 'errors-service.log', 'errors-service.log.1',
                 'crash-service.log', 'service-out.log', 'service-error.log')
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
DIRECTORY = getattr(stat, 'FILE_ATTRIBUTE_DIRECTORY', 0x10)
READ_EXECUTE = '0x1200a9'  # FILE_GENERIC_READ | FILE_GENERIC_EXECUTE, this folder only
PROGRAM_DATA = uuid.UUID('62AB5D82-FDC1-4DC3-A9DD-070D1D495D97')


def valid_sid(sid):
    if not isinstance(sid, str) or not SID.fullmatch(sid):
        raise ValueError('Некорректный SID пользователя службы')
    return sid


def folder_id(sid):
    """Stable per-user name; the installer scripts compute the same value (Get-ServiceId)."""
    return hashlib.sha256(valid_sid(sid).upper().encode('ascii')).hexdigest()[:16]


def service_folder(sid, base=None):
    return Path(base or program_data()) / ROOT_NAME / folder_id(sid)


def root_sddl():
    # Users may only list the root; every per-user folder below has its own protected DACL.
    return 'O:SYD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;;' + READ_EXECUTE + ';;;BU)'


def folder_sddl(sid):
    return 'O:SYD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;;' + READ_EXECUTE + ';;;' + valid_sid(sid) + ')'


def key_sddl(sid):
    return 'O:SYD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;' + valid_sid(sid) + ')'


def is_link(path):
    """True for a symbolic link, junction or any other reparse point; the target is never followed."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & REPARSE)


def plain_file(path):
    """An existing regular file with exactly one name: safe to truncate in place."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and not is_link(path) and info.st_nlink == 1


def remove_entry(path):
    """Remove a name only: a link or junction disappears, its target stays untouched."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if getattr(info, 'st_file_attributes', 0) & DIRECTORY or stat.S_ISDIR(info.st_mode):
        if not is_link(path):
            raise RuntimeError('В папке службы неожиданный каталог: ' + path.name)
        os.rmdir(path)
    else:
        os.unlink(path)


def remove_unsafe(folder, names=SERVICE_FILES, owners=None):
    """Delete engine file names that are links, have extra hard links or a foreign owner."""
    removed = []
    for name in names:
        path = Path(folder) / name
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        unsafe = not plain_file(path)
        if not unsafe and owners is not None and os.name == 'nt':
            unsafe = owner(path) not in owners
        if unsafe:
            remove_entry(path)
            removed.append(name)
    return removed


# --- Windows API -----------------------------------------------------------------------------

READ_CONTROL = 0x20000
WRITE_DAC = 0x40000
WRITE_OWNER = 0x80000
GENERIC_WRITE = 0x40000000
SHARE_ALL = 0x7
OPEN_EXISTING = 3
CREATE_NEW = 1
BACKUP_SEMANTICS = 0x02000000
OPEN_REPARSE_POINT = 0x00200000
NORMAL = 0x80
SE_FILE_OBJECT = 1
OWNER_INFO = 1
DACL_INFO = 4
PROTECTED_DACL = 0x80000000
api = {}


def windows():
    if api:
        return api
    if os.name != 'nt':
        raise OSError('Только Windows')
    import ctypes
    from ctypes import wintypes

    class Info(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('created', wintypes.FILETIME), ('accessed', wintypes.FILETIME),
                    ('written', wintypes.FILETIME), ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                    ('size_low', wintypes.DWORD), ('links', wintypes.DWORD), ('index_high', wintypes.DWORD),
                    ('index_low', wintypes.DWORD)]

    class Attributes(ctypes.Structure):
        _fields_ = [('nLength', wintypes.DWORD), ('lpSecurityDescriptor', ctypes.c_void_p), ('bInheritHandle', wintypes.BOOL)]

    class Luid(ctypes.Structure):
        _fields_ = [('low', wintypes.DWORD), ('high', wintypes.LONG)]

    class Privileges(ctypes.Structure):
        _fields_ = [('count', wintypes.DWORD), ('luid', Luid), ('attributes', wintypes.DWORD)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    ole = ctypes.WinDLL('ole32')
    void, handle, dword, boolean = ctypes.c_void_p, wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    ref = ctypes.POINTER

    def bind(dll, name, restype, *argtypes):
        function = getattr(dll, name)
        function.restype = restype
        function.argtypes = list(argtypes)
        return function

    api.update(
        ctypes=ctypes, Info=Info, Attributes=Attributes, Luid=Luid, Privileges=Privileges,
        create=bind(kernel, 'CreateFileW', handle, wintypes.LPCWSTR, dword, dword, void, dword, dword, handle),
        info=bind(kernel, 'GetFileInformationByHandle', boolean, handle, ref(Info)),
        close=bind(kernel, 'CloseHandle', boolean, handle),
        write=bind(kernel, 'WriteFile', boolean, handle, void, dword, ref(dword), void),
        local_free=bind(kernel, 'LocalFree', void, void),
        process=bind(kernel, 'GetCurrentProcess', handle),
        parse=bind(advapi, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', boolean, wintypes.LPCWSTR, dword, ref(void), ref(dword)),
        format=bind(advapi, 'ConvertSecurityDescriptorToStringSecurityDescriptorW', boolean, void, dword, dword, ref(void), ref(dword)),
        sd_owner=bind(advapi, 'GetSecurityDescriptorOwner', boolean, void, ref(void), ref(boolean)),
        sd_dacl=bind(advapi, 'GetSecurityDescriptorDacl', boolean, void, ref(boolean), ref(void), ref(boolean)),
        set_security=bind(advapi, 'SetSecurityInfo', dword, handle, ctypes.c_int, dword, void, void, void, void),
        get_security=bind(advapi, 'GetSecurityInfo', dword, handle, ctypes.c_int, dword, ref(void), ref(void), ref(void), ref(void), ref(void)),
        sid_text=bind(advapi, 'ConvertSidToStringSidW', boolean, void, ref(void)),
        open_token=bind(advapi, 'OpenProcessToken', boolean, handle, dword, ref(handle)),
        token_info=bind(advapi, 'GetTokenInformation', boolean, handle, ctypes.c_int, void, dword, ref(dword)),
        lookup=bind(advapi, 'LookupPrivilegeValueW', boolean, wintypes.LPCWSTR, wintypes.LPCWSTR, ref(Luid)),
        adjust=bind(advapi, 'AdjustTokenPrivileges', boolean, handle, boolean, ref(Privileges), dword, void, void),
        known_folder=bind(shell, 'SHGetKnownFolderPath', ctypes.c_long, void, dword, handle, ref(void)),
        task_free=bind(ole, 'CoTaskMemFree', None, void),
    )
    return api


def fail(name):
    w = windows()
    raise w['ctypes'].WinError(w['ctypes'].get_last_error(), name)


def program_data():
    if os.name != 'nt':
        return os.environ.get('ProgramData', '/tmp')
    w = windows()
    ctypes = w['ctypes']
    guid = ctypes.create_string_buffer(PROGRAM_DATA.bytes_le, 16)
    text = ctypes.c_void_p()
    if w['known_folder'](ctypes.addressof(guid), 0, None, ctypes.byref(text)) != 0:
        return os.environ.get('ProgramData', r'C:\ProgramData')
    try:
        return ctypes.wstring_at(text.value)
    finally:
        w['task_free'](text)


def sid_string(sid):
    w = windows()
    text = w['ctypes'].c_void_p()
    if not w['sid_text'](sid, w['ctypes'].byref(text)):
        fail('ConvertSidToStringSidW')
    try:
        return w['ctypes'].wstring_at(text.value)
    finally:
        w['local_free'](text)


def current_sid():
    """SID of the user running this process (the same for an elevated token of that user)."""
    w = windows()
    ctypes = w['ctypes']
    token = ctypes.c_void_p()
    if not w['open_token'](w['process'](), 0x8, ctypes.byref(token)):
        fail('OpenProcessToken')
    try:
        size = ctypes.c_ulong()
        w['token_info'](token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not w['token_info'](token, 1, buffer, size, ctypes.byref(size)):
            fail('GetTokenInformation')
        return sid_string(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])
    finally:
        w['close'](token)


def enable_privileges(names=('SeRestorePrivilege', 'SeTakeOwnershipPrivilege')):
    """Best effort: SYSTEM holds these, a normal user does not (tests then use their own ACL)."""
    w = windows()
    ctypes = w['ctypes']
    token = ctypes.c_void_p()
    if not w['open_token'](w['process'](), 0x28, ctypes.byref(token)):
        return
    try:
        for name in names:
            luid = w['Luid']()
            if w['lookup'](None, name, ctypes.byref(luid)):
                w['adjust'](token, False, ctypes.byref(w['Privileges'](1, luid, 2)), 0, None, None)
    finally:
        w['close'](token)


def open_entry(path, access):
    """Handle to the entry itself, never to the target of a link or junction."""
    w = windows()
    handle = w['create'](str(path), access, SHARE_ALL, None, OPEN_EXISTING, BACKUP_SEMANTICS | OPEN_REPARSE_POINT, None)
    if handle in (None, w['ctypes'].c_void_p(-1).value):
        fail(str(path))
    return handle


def entry_info(handle):
    w = windows()
    info = w['Info']()
    if not w['info'](handle, w['ctypes'].byref(info)):
        fail('GetFileInformationByHandle')
    return info


def handle_owner(handle):
    w = windows()
    ctypes = w['ctypes']
    sid, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    code = w['get_security'](handle, SE_FILE_OBJECT, OWNER_INFO, ctypes.byref(sid), None, None, None, ctypes.byref(descriptor))
    if code:
        raise ctypes.WinError(code)
    try:
        return sid_string(sid)
    finally:
        w['local_free'](descriptor)


def owner(path):
    w = windows()
    handle = open_entry(path, READ_CONTROL)
    try:
        return handle_owner(handle)
    finally:
        w['close'](handle)


def describe(path):
    """Owner and DACL as SDDL (diagnostics and tests)."""
    w = windows()
    ctypes = w['ctypes']
    handle = open_entry(path, READ_CONTROL)
    try:
        sid, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        code = w['get_security'](handle, SE_FILE_OBJECT, OWNER_INFO | DACL_INFO, ctypes.byref(sid), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if code:
            raise ctypes.WinError(code)
        try:
            text = ctypes.c_void_p()
            if not w['format'](descriptor, 1, OWNER_INFO | DACL_INFO, ctypes.byref(text), None):
                fail('ConvertSecurityDescriptorToStringSecurityDescriptorW')
            try:
                return ctypes.wstring_at(text.value)
            finally:
                w['local_free'](text)
        finally:
            w['local_free'](descriptor)
    finally:
        w['close'](handle)


class Descriptor:
    """Self-relative security descriptor parsed from SDDL; freed on exit."""
    def __init__(self, sddl):
        self.sddl = sddl

    def __enter__(self):
        w = windows()
        ctypes = w['ctypes']
        self.pointer = ctypes.c_void_p()
        if not w['parse'](self.sddl, 1, ctypes.byref(self.pointer), None):
            fail('ConvertStringSecurityDescriptorToSecurityDescriptorW')
        return self

    def __exit__(self, *args):
        windows()['local_free'](self.pointer)

    def parts(self):
        w = windows()
        ctypes = w['ctypes']
        owner_sid, defaulted, present, dacl = ctypes.c_void_p(), ctypes.c_int(), ctypes.c_int(), ctypes.c_void_p()
        if not w['sd_owner'](self.pointer, ctypes.byref(owner_sid), ctypes.byref(defaulted)):
            fail('GetSecurityDescriptorOwner')
        if not w['sd_dacl'](self.pointer, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            fail('GetSecurityDescriptorDacl')
        return owner_sid.value, dacl.value


def apply(handle, sddl):
    """Set owner (if the SDDL names one) and a protected DACL through an already checked handle."""
    w = windows()
    with Descriptor(sddl) as descriptor:
        owner_sid, dacl = descriptor.parts()
        flags = DACL_INFO | PROTECTED_DACL | (OWNER_INFO if owner_sid else 0)
        code = w['set_security'](handle, SE_FILE_OBJECT, flags, owner_sid, None, dacl, None)
    if code:
        raise w['ctypes'].WinError(code)


def secure_directory(path, sddl):
    """Create the folder if needed and lock it; return its previous owner.

    A link or junction planted under the folder's name is removed (only the name) and the
    folder is created again; a second link in a row is refused.
    """
    w = windows()
    for _attempt in range(2):
        try:
            os.mkdir(path)
        except FileExistsError:
            pass
        handle = open_entry(path, READ_CONTROL | WRITE_DAC | WRITE_OWNER)
        try:
            attributes = entry_info(handle).attributes
            if not attributes & REPARSE:
                if not attributes & DIRECTORY:
                    raise RuntimeError('На месте папки службы находится файл: ' + str(path))
                previous = handle_owner(handle)
                apply(handle, sddl)
                return previous
        finally:
            w['close'](handle)
        remove_entry(path)
    raise RuntimeError('Папку службы повторно подменили ссылкой: ' + str(path))


def create_private_file(path, sddl, data):
    """Create a new file that already carries its DACL; an existing name is never reused."""
    w = windows()
    ctypes = w['ctypes']
    with Descriptor(sddl) as descriptor:
        attributes = w['Attributes'](ctypes.sizeof(w['Attributes']), descriptor.pointer, False)
        handle = w['create'](str(path), GENERIC_WRITE, 0, ctypes.addressof(attributes), CREATE_NEW, NORMAL, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        fail(str(path))
    try:
        buffer = ctypes.create_string_buffer(bytes(data), len(data))
        written = ctypes.c_ulong()
        if not w['write'](handle, buffer, len(data), ctypes.byref(written), None) or written.value != len(data):
            fail('WriteFile')
    finally:
        w['close'](handle)


def write_key(folder, user_sid, sddl=None):
    """A fresh IPC key on every engine start; only SYSTEM, administrators and the user can read it."""
    path = Path(folder) / 'service.key'
    remove_entry(path)
    create_private_file(path, sddl or key_sddl(user_sid), os.urandom(32))
    return path


def prepare(folder, user_sid):
    """Lock the engine folder before anything is opened in it (runs as SYSTEM).

    Returns the names removed because they were links, had extra hard links or a foreign owner.
    """
    valid_sid(user_sid)
    folder = Path(folder)
    enable_privileges()
    secure_directory(folder.parent, root_sddl())
    previous = secure_directory(folder, folder_sddl(user_sid))
    if previous in TRUSTED_OWNERS:
        return remove_unsafe(folder, SERVICE_FILES, TRUSTED_OWNERS)
    # Someone else owned the folder: nothing under an engine name can be trusted.
    removed = []
    for name in SERVICE_FILES:
        if os.path.lexists(folder / name):
            remove_entry(folder / name)
            removed.append(name)
    return removed

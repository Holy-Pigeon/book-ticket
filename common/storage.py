"""Windows current-user DPAPI and atomic private JSON files."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile
from .errors import LocalError


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def protect(raw, decrypt=False):
    if os.name != 'nt':
        raise LocalError('凭证存储需要 Windows 当前用户的 DPAPI。')
    buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    src, out = Blob(len(raw), buf), Blob()
    fn = getattr(ctypes.WinDLL('crypt32', use_last_error=True),
                 'CryptUnprotectData' if decrypt else 'CryptProtectData')
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                  ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out)):
        raise LocalError('Windows 本机凭证加密或解密失败，请在原 Windows 账户重新导入。')
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        free = ctypes.WinDLL('kernel32').LocalFree
        free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
        free(out.pbData)


def project_data_dir(project):
    if not project or project in {'.', '..'} or any(c in project for c in '\\/:'):
        raise ValueError('Use one project directory name')
    override = os.environ.get('BOOK_TICKET_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve() / project
    root = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share'))
    return root / 'book-ticket' / project


def write_json(path, value):
    path = Path(path)
    raw = protect(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False) as f:
            temporary = Path(f.name)
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(protect(Path(path).read_bytes(), True))
    except Exception:
        raise LocalError('本机配置或预约记录无法读取；请先初始化或在原 Windows 账户核对。') from None

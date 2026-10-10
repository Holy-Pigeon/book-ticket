"""Atomic encrypted JSON: legacy Windows DPAPI or macOS Keychain-backed Fernet."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile
from .errors import LocalError
from .paths import project_data_dir  # Re-export for existing callers.
from .platforms import system
from .concurrency import SingleInstance

MAC_PREFIX = b'BOOK-TICKET:KEYCHAIN:1\n'
KEYCHAIN_SERVICE = 'book-ticket'
KEYCHAIN_ACCOUNT = 'local-encryption-v1'


def _mac_keyring():
    # Select the native backend explicitly: no environment-selected plaintext fallback.
    from keyring.backends.macOS import Keyring
    return Keyring()


def _mac_protect(raw, decrypt):
    from cryptography.fernet import Fernet
    try:
        backend = _mac_keyring()
        if decrypt:
            if not raw.startswith(MAC_PREFIX):
                raise LocalError('此凭证不是 macOS 本机格式，请在当前电脑重新采集登录态。')
            key = backend.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
            if key is None:
                raise LocalError('钥匙串中的本机加密密钥缺失，请恢复原钥匙串；请勿删除预约记录后重试。')
        else:
            # Serialize initial key creation across both projects and all data directories.
            with SingleInstance('keychain-initialization'):
                key = backend.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
                if key is None:
                    key = Fernet.generate_key().decode('ascii')
                    backend.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, key)
                    if backend.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT) != key:
                        raise LocalError('本机密钥未能可靠保存到钥匙串，未保存凭证。')
        cipher = Fernet(key.encode('ascii'))
        return cipher.decrypt(raw[len(MAC_PREFIX):]) if decrypt else MAC_PREFIX + cipher.encrypt(raw)
    except LocalError:
        raise
    except Exception:
        raise LocalError('macOS 钥匙串加密或解密失败。请解锁登录钥匙串并允许当前 Python 访问，未使用明文存储。') from None


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def protect(raw, decrypt=False):
    if system() == 'macos':
        return _mac_protect(raw, decrypt)
    if decrypt and raw.startswith(MAC_PREFIX):
        raise LocalError('此凭证来自 macOS，请在当前电脑重新采集登录态。')
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


def write_json(path, value):
    raw = protect(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    _write_bytes(path, raw)


def write_plain_json(path, value):
    """Atomic UTF-8 JSON for already sanitized diagnostics, never credentials."""
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
    _write_bytes(path, raw)


def _write_bytes(path, raw):
    path = Path(path)
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
        raise LocalError('本机配置或预约记录无法读取；请在原系统账户核对凭证和加密密钥，勿删除预约记录后重试。') from None

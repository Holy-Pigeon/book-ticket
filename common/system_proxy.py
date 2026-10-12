"""Temporary user proxy with encrypted recovery state; never changes TLS trust."""
import ctypes
from ctypes import wintypes
import contextlib
import subprocess

from .errors import LocalError
from .platforms import system
from .platforms import proxy_instructions
from .storage import read_json, write_json


class WindowsProxy:
    def _options(self, state=None):
        class Value(ctypes.Union):
            _fields_ = [('number', wintypes.DWORD), ('pointer', ctypes.c_void_p), ('time', wintypes.FILETIME)]
        class Option(ctypes.Structure):
            _fields_ = [('option', wintypes.DWORD), ('value', Value)]
        class Options(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('connection', ctypes.c_wchar_p),
                        ('count', wintypes.DWORD), ('error', wintypes.DWORD), ('options', ctypes.POINTER(Option))]
        options = (Option * 4)()
        # FLAGS_UI preserves the user's auto-detect preference; restore via FLAGS.
        for item, index in zip(options, (1 if state else 10, 2, 3, 4)):
            item.option = index
        strings = []
        if state:
            options[0].value.number = state['flags']
            for item, name in zip(options[1:], ('server', 'bypass', 'pac')):
                text = ctypes.create_unicode_buffer(state[name])
                strings.append(text)
                item.value.pointer = ctypes.cast(text, ctypes.c_void_p).value
        value = Options(ctypes.sizeof(Options), None, 4, 0, options)
        api = ctypes.WinDLL('wininet', use_last_error=True)
        query, change = api.InternetQueryOptionW, api.InternetSetOptionW
        query.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        change.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        query.restype = change.restype = wintypes.BOOL
        if state:
            if not change(None, 75, ctypes.byref(value), ctypes.sizeof(value)):
                raise LocalError('无法设置当前用户的网络代理。')
            if not change(None, 39, None, 0) or not change(None, 37, None, 0):
                raise LocalError('代理设置刷新失败，需要恢复原设置。')
            return
        length = wintypes.DWORD(ctypes.sizeof(value))
        if not query(None, 75, ctypes.byref(value), ctypes.byref(length)):
            raise LocalError('无法读取原网络代理，未修改设置。')
        free = ctypes.WinDLL('kernel32').GlobalFree
        free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
        try:
            return dict(flags=int(options[0].value.number), **{
                name: ctypes.wstring_at(item.value.pointer) if item.value.pointer else ''
                for item, name in zip(options[1:], ('server', 'bypass', 'pac'))})
        finally:
            for item in options[1:]:
                if item.value.pointer:
                    free(item.value.pointer)

    snapshot = _options

    def apply(self, state):
        self._options(state)

    def local(self, port):
        # Only the venue's HTTP page uses this listener. Payment HTTPS stays direct.
        return dict(flags=3, server=f'http=127.0.0.1:{port}', bypass='<local>;localhost;127.*', pac='')


class MacProxy:
    def __init__(self, service):
        if not service or service.startswith('-'):
            raise LocalError('macOS 自动代理需指定当前网络服务，例如 --network-service Wi-Fi；也可使用 --manual-proxy。')
        self.service = service

    def _run(self, option, *args):
        try:
            result = subprocess.run(['/usr/sbin/networksetup', option, self.service, *map(str, args)],
                                    capture_output=True, text=True, check=True, timeout=15)
            if 'Error:' in result.stdout:
                raise OSError()
            return result.stdout
        except (OSError, subprocess.SubprocessError):
            raise LocalError('macOS 网络代理读取或设置失败，请核对网络服务名和系统权限；也可使用手动代理。') from None

    def snapshot(self):
        result = {}
        for name, option in [('http', '-getwebproxy'), ('https', '-getsecurewebproxy'),
                             ('pac', '-getautoproxyurl'), ('discovery', '-getproxyautodiscovery')]:
            fields = dict((line.partition(':')[0].strip(), line.partition(':')[2].strip())
                          for line in self._run(option).splitlines() if ':' in line)
            key = 'Auto Proxy Discovery' if name == 'discovery' else 'Enabled'
            if fields.get(key) not in ('Yes', 'No', 'On', 'Off'):
                raise LocalError('macOS 代理设置格式不明确，未修改设置。')
            if name == 'http' and fields.get('Authenticated Proxy Enabled') == '1':
                raise LocalError('原代理要求认证，无法完整自动恢复，请使用手动代理。')
            result[name] = fields
        if not result['http'].get('Server') or result['http'].get('Port') in ('0', None):
            raise LocalError('原 HTTP 代理地址为空，无法保证完整自动恢复；macOS 请使用 --manual-proxy。')
        return result

    def apply(self, state):
        data = state['http']
        self._run('-setwebproxy', data['Server'], data['Port'])
        self._run('-setwebproxystate', 'on' if data['Enabled'] == 'Yes' else 'off')
        # HTTPS and the PAC URL are never changed, so do not rewrite their values.
        pac = state['pac']
        self._run('-setautoproxystate', 'on' if pac['Enabled'] == 'Yes' else 'off')
        self._run('-setproxyautodiscovery', 'on' if state['discovery']['Auto Proxy Discovery'] == 'On' else 'off')

    def local(self, port):
        proxy = {'Enabled': 'Yes', 'Server': '127.0.0.1', 'Port': str(port), 'Authenticated Proxy Enabled': '0'}
        state = self.snapshot()
        state['http'] = proxy
        state['pac']['Enabled'] = 'No'
        state['discovery']['Auto Proxy Discovery'] = 'Off'
        return state


class TemporaryProxy:
    def __init__(self, recovery, port=8766, service=None, backend=None):
        self.path, self.port, self.service = recovery, port, service
        self.backend = backend or (WindowsProxy() if system() == 'windows' else MacProxy(service))

    def __enter__(self):
        if self.path.exists():
            raise LocalError('存在未处理的代理恢复记录，请先运行 recover-proxy，未覆盖原记录。')
        before = self.backend.snapshot()
        requested = self.backend.local(self.port)
        record = {'system': system(), 'service': self.service, 'before': before, 'requested': requested}
        write_json(self.path, record)
        try:
            self.backend.apply(requested)
            record['applied'] = self.backend.snapshot()
            write_json(self.path, record)
        except BaseException:
            self.backend.apply(before)
            self.path.unlink(missing_ok=True)
            raise
        return self

    def restore(self):
        record = read_json(self.path)
        if record['system'] != system():
            raise LocalError('代理恢复记录属于其他系统，未修改设置。')
        if self.backend.snapshot() != record.get('applied', record['requested']):
            raise LocalError('代理已被其他操作修改，未覆盖新设置。请手动恢复原代理，再处理恢复记录。')
        self.backend.apply(record['before'])
        self.path.unlink(missing_ok=True)

    def __exit__(self, *args):
        self.restore()


def recover_proxy(path):
    if not path.exists():
        return False
    record = read_json(path)
    guard = TemporaryProxy(path, service=record.get('service'))
    guard.restore()
    return True


@contextlib.contextmanager
def proxy_session(path, manual=False, service=None, show=print, restored=None):
    """Caller must keep its listener alive until this context has restored settings."""
    if not manual:
        guard = TemporaryProxy(path, service=service)
        try:
            with guard:
                yield
        finally:
            if not path.exists():
                show('系统代理已退出临时设置，没有待恢复记录。')
            else:
                show('代理自动恢复未完成：请运行 recover-proxy，或按原设置手动恢复。')
        return
    if restored is None:
        restored = lambda: input('请先恢复原代理设置，恢复后按回车，再停止监听：')
    show(proxy_instructions(8766))
    try:
        yield
    finally:
        restored()

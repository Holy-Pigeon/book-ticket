"""Crash-safe exclusion: Windows named mutex or macOS advisory file lock."""
import ctypes
from .paths import application_data_dir
from .platforms import system
from .errors import LocalError


class SingleInstance:
    def __init__(self, name):
        if not name or any(c in name for c in '\\/:'):
            raise ValueError('Use a nonempty mutex name without separators')
        self.name = 'Local\\BookTicket-' + name
        self.filename = name + '.lock'

    def __enter__(self):
        if system() == 'macos':
            import fcntl
            directory = application_data_dir() / 'locks'
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.file = (directory / self.filename).open('a+b')
            try:
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.file.close()
                raise LocalError('另一预约进程正在运行或无法取得进程锁，已停止本次提交。') from None
            self.close = lambda _: self.file.close()
            self.handle = None
            return self
        dll = ctypes.WinDLL('kernel32', use_last_error=True)
        fn = dll.CreateMutexW
        fn.argtypes, fn.restype = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p], ctypes.c_void_p
        ctypes.set_last_error(0)
        self.handle = fn(None, False, self.name)
        error = ctypes.get_last_error()
        self.close = dll.CloseHandle
        self.close.argtypes, self.close.restype = [ctypes.c_void_p], ctypes.c_bool
        if not self.handle or error == 183:
            if self.handle:
                self.close(self.handle)
            raise LocalError('另一预约进程正在运行，已停止本次提交。')
        return self

    def __exit__(self, *args):
        self.close(self.handle)

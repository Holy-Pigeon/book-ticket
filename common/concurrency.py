"""Windows process-wide exclusion for one booking submission workflow."""
import ctypes
import os
from .errors import LocalError


class SingleInstance:
    def __init__(self, name):
        if not name or any(c in name for c in '\\/:'):
            raise ValueError('Use a nonempty mutex name without separators')
        self.name = 'Local\\BookTicket-' + name

    def __enter__(self):
        if os.name != 'nt':
            raise LocalError('此进程互斥实现需要 Windows。')
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

"""Tk helpers loaded only by desktop modes; command-line queries need no Tk."""
from .errors import LocalError
from .platforms import open_proxy_settings


def show_proxy_settings():
    from tkinter import messagebox
    try:
        open_proxy_settings()
    except LocalError as error:
        messagebox.showerror('代理设置', str(error))


def require_tk():
    try:
        import tkinter
    except ImportError:
        raise LocalError('图形界面需要 Tkinter。请使用含 Tcl/Tk 的 Python 安装版本；macOS 可安装 python.org 的 Python 后重建虚拟环境。') from None

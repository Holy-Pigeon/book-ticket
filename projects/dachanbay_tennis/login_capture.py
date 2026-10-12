"""Bounded own-session capture; restore the system proxy before stopping the listener."""
import contextlib
import queue
import time

from common.concurrency import SingleInstance
from common.system_proxy import proxy_session
from . import core
from .capture_proxy import Proxy


def capture_login(timeout=180, manual=False, service=None, show=print, *, already_locked=False, stop=None):
    if not 1 <= timeout <= 600:
        raise core.BookingError('采集时限须在 1–600 秒之间。')
    pending = queue.Queue(maxsize=1)
    def receive(value):
        try:
            pending.put_nowait(value)
        except queue.Full:
            pass
    with contextlib.nullcontext() if already_locked else SingleInstance('dachanbay-tennis'):
        try:
            proxy = Proxy(receive)
        except OSError:
            raise core.BookingError('采集端口 8766 已被占用；请停止原助手或代理，再重新采集。') from None
        proxy.start()
        try:
            with proxy_session(core.DATA / 'proxy-recovery.dpapi', manual, service, show):
                show('采集已准备好。请在电脑微信打开 http://' + core.HOST + '/index 并进入网球场。')
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if stop and stop.is_set():
                        raise core.BookingError('采集已停止。')
                    try:
                        credentials = pending.get(timeout=min(1, max(0.01, deadline - time.monotonic())))
                    except queue.Empty:
                        continue
                    try:
                        core.Api(credentials).profile()
                    except core.BookingError:
                        show('捕获的登录态尚未通过校验，请在微信刷新登录后重新进入网球场。')
                        continue
                    core.save_credentials(credentials)
                    show('登录态已校验并加密保存。')
                    return
                raise core.BookingError('采集超时，没有获取有效登录态。')
        finally:
            proxy.stop()
            show(f'本次看到了 {proxy.target_seen} 个场馆接口请求。')
            show('采集监听已停止。若自动恢复报错，请运行 recover-proxy 或按原设置手动恢复。')

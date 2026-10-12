"""Persistent serial runner: fresh own login, prepared cashier, then release watch."""
import datetime as dt
import threading
import time

from . import core
from .login_capture import capture_login
from .workflow import book_with_cashier

STOP_FILE = core.DATA / 'watch-stop.request'


class FileStop:
    def __init__(self, path=STOP_FILE):
        self.path = path
        self.event = threading.Event()

    def is_set(self):
        return self.event.is_set() or self.path.exists()

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.is_set():
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                return False
            self.event.wait(min(.25,remaining))
        return True


def request_stop():
    STOP_FILE.parent.mkdir(parents=True,exist_ok=True)
    STOP_FILE.write_text('stop\n',encoding='utf-8')


def next_preparation(now, release_days=7):
    for offset in range(8):
        candidate = (now + dt.timedelta(days=offset)).replace(hour=19, minute=45, second=0, microsecond=0)
        end = candidate.replace(hour=20, minute=3)
        if now >= end:
            continue
        if (candidate.date() + dt.timedelta(days=release_days)).weekday() in core.TARGETS:
            return candidate
    raise core.BookingError('未找到下一次目标放号日。')


def run_daily(approved_hash, cap_cents, *, release_days=7, manual=False, service=None,
              show=print, stop=None, clock=None):
    """Caller owns the project lock. Any unexpected API/qualification error stops."""
    if stop is None:
        STOP_FILE.unlink(missing_ok=True)
        stop = FileStop()
    clock = clock or (lambda: dt.datetime.now(core.TZ))
    while not stop.is_set():
        ready_at = next_preparation(clock(), release_days)
        show(f'下一次登录准备：北京时间 {ready_at:%Y-%m-%d %H:%M}；目标日期 {ready_at.date()+dt.timedelta(days=release_days)}。')
        while clock() < ready_at:
            if stop.wait(min(30, max(.01, (ready_at-clock()).total_seconds()))):
                return
        # Re-capture each release day even if yesterday's credential still works.
        capture_login(300, manual, service, show, already_locked=True, stop=stop)
        if stop.is_set():
            return
        end = ready_at.replace(hour=20, minute=3)
        remaining = (end-clock()).total_seconds()
        if remaining <= 0:
            raise core.BookingError('登录准备已超过本次放号窗口，今日未下单。')
        api = core.Api(core.load_credentials())
        book_with_cashier(api, approved_hash, cap_cents, stop=stop, manual=manual,
                          service=service, show=show, timeout=int(remaining)+60,
                          await_release=True, release_days=release_days)
        # Prevent another attempt in the same release window, including no-stock.
        tomorrow = ready_at + dt.timedelta(days=1)
        while clock() < tomorrow:
            if stop.wait(min(30, max(.01,(tomorrow-clock()).total_seconds()))):
                return

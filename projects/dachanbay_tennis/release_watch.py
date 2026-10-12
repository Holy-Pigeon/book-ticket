"""Serial bounded release polling. Only availability reads repeat, never bookings."""
import datetime as dt
import time

from . import core
from .workflow import notice_digest, reserve_one


def window(now):
    release = now.replace(hour=20, minute=0, second=0, microsecond=0)
    if now >= release + dt.timedelta(minutes=3):
        release += dt.timedelta(days=1)
    return release - dt.timedelta(seconds=15), release + dt.timedelta(minutes=3)


def watch_release(api, approved_hash, cap_cents, journal, stop, show=print, *,
                  clock=None, interval=0.1, release_days=7):
    clock = clock or (lambda: dt.datetime.now(core.TZ))
    start, end = window(clock())
    release_date = (end.date() + dt.timedelta(days=release_days)).isoformat()
    if dt.date.fromisoformat(release_date).weekday() not in core.TARGETS:
        show(f'本次新放号日期 {release_date} 不在目标周二/周三，不查询或下单。')
        return None
    show(f'等待北京时间 {start:%m-%d %H:%M:%S}，查询至 {end:%H:%M:%S}。每次响应后间隔 {interval:g} 秒；成功提交一次即停止。')
    while clock() < start:
        if stop.wait(min(30, max(0.01, (start-clock()).total_seconds()))):
            raise core.BookingError('已停止，没有下单。')
    court = api.tennis()
    type_id = court['ground_type_id']
    if notice_digest(api.notices(type_id)) != approved_hash:
        raise core.BookingError('须知已变化，守候停止，未下单。')
    if api.profile().get('require_verification') != 1:
        raise core.BookingError('登录或资格需重新确认，守候停止。')
    orders = api.orders()
    requests = 0
    last_report = time.monotonic()
    # Static identity/terms/orders are checked once here and again by reserve_one
    # before the sole POST; do not fetch them at 10 Hz.
    while clock() < end and not stop.is_set():
        targets = [target for target in core.target_dates(api.dates(type_id), clock())
                   if target[0] == release_date]
        requests += 1
        candidates = set()
        for target in targets:
            if stop.is_set() or clock() >= end:
                break
            if journal.contains(target) or core.duplicate(orders, target):
                continue
            grid = api.grid(type_id, target[0])
            requests += 1
            for slot in core.choices(grid, target):
                try:
                    core.validate_slot(grid, slot, cap_cents)
                except core.BookingError:
                    continue
                candidates.add((*target, slot['ground_id']))
        if candidates:
            show('发现符合目标的可预约时段，开始最终复核；下单请求不会重发。')
            # Fresh profile, agreement, orders, price, grid, qualification, journal.
            from common.polling import DeadlineStop
            bounded = DeadlineStop(stop, time.monotonic() + max(0,(end-clock()).total_seconds()))
            result = reserve_one(api, approved_hash, cap_cents, journal, bounded, clock(), candidates)
            if result is not None:
                return result
        if time.monotonic() - last_report >= 30:
            show(f'仍在查询，已完成 {requests} 次日期/余量请求；尚未发送下单请求。')
            last_report = time.monotonic()
        if stop.wait(interval):
            break
    show('本次放号查询结束，没有提交预约。')
    return None

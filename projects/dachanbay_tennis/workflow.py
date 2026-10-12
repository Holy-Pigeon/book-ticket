"""Headless preparation and a single booking, ending at a human payment handoff."""
from decimal import Decimal, InvalidOperation
import datetime as dt
import hashlib
import threading
import time

from . import core


def fee_cap(value):
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount <= 0 or amount != int(amount):
            raise ValueError()
        return int(amount)
    except (InvalidOperation, ValueError, OverflowError):
        raise core.BookingError('价格上限须大于 0，最多两位小数。') from None


def notice_digest(notices):
    return hashlib.sha256(notices.encode('utf-8')).hexdigest()


def one_off_date(value, now=None):
    now = now or dt.datetime.now(core.TZ)
    try:
        day = dt.date.fromisoformat(value)
        if day.isoformat() != value or day < now.date():
            raise ValueError()
    except (TypeError, ValueError):
        raise core.BookingError('单次预约日期须为今天或未来的 YYYY-MM-DD。') from None
    return day


def prepare(api, journal=None, now=None, *, booking_date=None, paid_only=False):
    now = now or dt.datetime.now(core.TZ)
    if paid_only and booking_date is None:
        raise core.BookingError('仅付费场次筛选须配合单次预约日期。')
    if booking_date is not None:
        one_off_date(booking_date, now)
    profile = api.profile()
    court = api.tennis()
    type_id = court['ground_type_id']
    notices = api.notices(type_id)
    orders = api.orders()
    journal = journal or core.Journal()
    options = []
    dates = api.dates(type_id)
    grids = {}
    if booking_date is None:
        targets = core.target_dates(dates, now)
    else:
        targets = set()
        if any(item.get('date') == booking_date for item in dates):
            grid = grids[booking_date] = api.grid(type_id, booking_date)
            for court in grid['items']:
                for slot in core.require_list(court, 'booking_durations'):
                    start, end = core.hhmm(slot.get('start_time')), core.hhmm(slot.get('end_time'))
                    try:
                        a = dt.datetime.fromisoformat(booking_date + ' ' + start).replace(tzinfo=core.TZ)
                        b = dt.datetime.fromisoformat(booking_date + ' ' + end).replace(tzinfo=core.TZ)
                    except ValueError:
                        raise core.BookingError('预约时段格式不符，已停止。') from None
                    if a > now and b-a == dt.timedelta(hours=1):
                        targets.add((booking_date, start, end))
        targets = sorted(targets)
    for target in targets:
        if target[0] not in grids:
            grids[target[0]] = api.grid(type_id, target[0])
        grid = grids[target[0]]
        for slot in core.choices(grid, target):
            if paid_only and (slot.get('duration_fee_type') != 3 or slot['price'] <= 0):
                continue
            reason = None
            if journal.contains(target):
                reason = '已有本机下单尝试，需核对订单'
            elif core.duplicate(orders, target):
                reason = '微信已有重叠订单'
            else:
                try:
                    core.validate_slot(grid, slot, slot['price'])
                except core.BookingError as error:
                    reason = str(error)
            options.append(dict(target=target, slot=slot, blocked=reason))
    return dict(type_id=type_id, notices=notices, notice_hash=notice_digest(notices),
                direct_booking_allowed=profile.get('require_verification') == 1, options=options)


def reserve_one(api, approved_hash, cap_cents, journal=None, stop=None, now=None, candidates=None,
                *, booking_date=None, paid_only=False):
    if cap_cents is not None and (not isinstance(cap_cents, int) or isinstance(cap_cents, bool) or cap_cents <= 0):
        raise core.BookingError('请先明确单次价格上限。')
    journal = journal or core.Journal()
    stop = stop or threading.Event()
    plan = prepare(api, journal, now, booking_date=booking_date, paid_only=paid_only)
    if not approved_hash or plan['notice_hash'] != approved_hash:
        raise core.BookingError('须知未确认或已变化，请先查看当前须知再确认。')
    if not plan['direct_booking_allowed']:
        raise core.BookingError('账户需要在微信完成资格或手机号确认。')
    options = [x for x in plan['options'] if not x['blocked'] and (cap_cents is None or x['slot']['price'] <= cap_cents)]
    if candidates is not None:
        options = [x for x in options if (*x['target'], x['slot']['ground_id']) in candidates]
    options.sort(key=lambda x: (x['target'], x['slot']['price'], str(x['slot']['ground_id'])))
    if not options:
        return None
    # An invalid/over-budget court must not prevent choosing another valid court.
    selected = options[0]
    if stop.is_set():
        raise core.BookingError('已停止。')
    if notice_digest(api.notices(plan['type_id'])) != approved_hash:
        raise core.BookingError('提交前须知发生变化，未下单。')
    response = core.submit(api, journal, plan['type_id'], selected['target'],
                           selected['slot'], cap_cents, stop, paid_only=paid_only)
    record = journal.records[journal.key(selected['target'])]
    return dict(target=selected['target'], ground_name=record.get('ground_name', '网球场'),
                price_cents=record.get('price_cents'), main_order_no=response['main_order_no'],
                need_payment=response['need_payment'], response=response)


def describe(plan, show=print):
    show('当前预约须知：\n' + plan['notices'])
    show('须知摘要值：' + plan['notice_hash'])
    show('账户可直接预约：' + str(plan['direct_booking_allowed']))
    for option in plan['options']:
        target, slot = option['target'], option['slot']
        status = option['blocked'] or '可提交'
        show(f"{' '.join(target)}  {slot['ground_name']}  ¥{slot['price']/100:.2f}  {status}")
    if not plan['options']:
        show('当前开放日期没有匹配的目标时段。')


def book_with_cashier(api, approved_hash, cap_cents, *, journal=None, stop=None,
                      manual=False, service=None, show=print, restored=None,
                      timeout=180, now=None, await_release=False, release_days=7,
                      booking_date=None, paid_only=False):
    """Caller owns SingleInstance. Verify the receiving browser BEFORE the POST."""
    from common.system_proxy import proxy_session
    from .capture_proxy import Proxy
    from .cashier import Handoff, authorize_session, check_frontend
    journal = journal or core.Journal()
    stop = stop or threading.Event()
    if booking_date is not None:
        one_off_date(booking_date, now)
        if await_release:
            raise core.BookingError('单次预约日期不能与放号守候同时使用。')
    if paid_only and booking_date is None:
        raise core.BookingError('仅付费场次筛选须配合单次预约日期。')
    if await_release:
        import datetime as dt
        from .release_watch import window
        release_date = window(now or dt.datetime.now(core.TZ))[1].date() + dt.timedelta(days=release_days)
        if release_date.weekday() not in core.TARGETS:
            show(f'本次新放号日期 {release_date} 不在目标周二/周三，不查询或下单。')
            return None
    if api is not None:
        plan = prepare(api, journal, now, booking_date=booking_date, paid_only=paid_only)
        if plan['notice_hash'] != approved_hash:
            raise core.BookingError('须知未确认或已变化，未下单。')
        if not await_release and not any(not x['blocked'] and (cap_cents is None or x['slot']['price'] <= cap_cents) for x in plan['options']):
            show('当前没有符合条件且未尝试过的目标时段，没有下单。')
            return None
    check_frontend()
    handoff = Handoff(api.credentials if api else None, timeout,
                      authorize=authorize_session if api is None else None)
    from common.polling import DeadlineStop
    stop = DeadlineStop(stop, handoff.deadline, abort=lambda:
                        handoff.ready.is_set() and not handoff.delivered
                        and time.monotonic()-handoff.last_seen > 90)
    try:
        proxy = Proxy(lambda value: None, local_handler=handoff.handle)
    except OSError:
        raise core.BookingError('收银台代理端口 8766 已被占用，未下单；请停止原助手或代理。') from None
    proxy.start()
    def wait(event):
        while not event.wait(0.25):
            if stop.is_set():
                raise core.BookingError('已停止；已发出的下单请求请在微信核对，勿重复提交。')
            if time.monotonic() >= handoff.deadline:
                raise core.BookingError('微信收银台交接超时；请核对助手下单记录与微信订单，勿重复提交。')
    result = None
    try:
        with proxy_session(core.DATA / 'proxy-recovery.dpapi', manual, service, show, restored):
            show('请在电脑微信打开以下一次性链接；页面就绪后才会下单，最终付款由你确认：')
            show(handoff.url)
            wait(handoff.ready)
            if api is None:
                api = core.Api(handoff.credentials)
                show('微信登录态已验证并加密保存；当前页面将直接接收预约结果，无需再打开第二个链接。')
                plan = prepare(api, journal, now, booking_date=booking_date, paid_only=paid_only)
                if plan['notice_hash'] != approved_hash:
                    raise core.BookingError('须知已变化，未下单，请重新阅读并确认。')
                if not plan['direct_booking_allowed']:
                    raise core.BookingError('账户需要在微信完成资格或手机号确认，未下单。')
            if await_release:
                from .release_watch import watch_release
                result = watch_release(api, approved_hash, cap_cents, journal, stop, show, release_days=release_days)
            else:
                if booking_date:
                    show(f'本次仅预约 {booking_date} 的一个可用一小时场次；不会修改每周目标。')
                result = reserve_one(api, approved_hash, cap_cents, journal, stop, now,
                                     booking_date=booking_date, paid_only=paid_only)
            if result is None:
                handoff.fail('余量或条件发生变化，没有下单。请返回助手。')
                show('余量或条件发生变化，没有下单。')
                # Allow the waiting browser to receive this terminal result.
                stop.wait(1.5)
                return None
            show(f"已收到预约响应：{' '.join(result['target'])}，{result['ground_name']}，¥{result['price_cents']/100:.2f}。")
            show('主订单号：' + str(result['main_order_no']) + '。不会再次提交这个目标。')
            handoff.publish(result)
            show('请在助手页面核对场次并点击“继续付款”，随后由你在官网收银台确认。'
                 if result['need_payment'] else '请在微信页面核对免支付预约结果。')
            wait(handoff.launched)
            record = journal.records[journal.key(result['target'])]
            record['cashier_state'] = 'sdk_requested' if result['need_payment'] else 'no_payment_required'
            core.write_json(journal.path, journal.records)
            show('官网收银台调用已发起，请在微信确认付款；助手未核验付款是否成功。'
                 if result['need_payment'] else '接口返回无需支付，请在微信核对预约。')
    finally:
        proxy.stop()
        if result and not handoff.launched.is_set():
            show('预约请求已有响应，但收银台交接未确认。请核对微信或联系场馆，助手不会重订。')
    return result

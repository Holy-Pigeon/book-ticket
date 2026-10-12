"""Run from the repository root: python -m projects.dachanbay_tennis [command]."""
import argparse
from common.cli import configure_output
from common.errors import LocalError


def main(argv=None):
    configure_output()
    parser = argparse.ArgumentParser(description="昀湾网球预约助手：自动提交预约，手动付款")
    parser.add_argument("command", nargs="?", default="watch",
                        choices=("watch", "capture", "query", "capture-payment", "capture-login", "plan", "book", "release", "daily", "stop-daily", "check-cashier", "recover-proxy"),
                        help="watch/capture 图形助手；plan 只读预览；book 预约；release 放号守候；daily 每日准备；check-cashier 验证微信页面；capture-login 采集登录；recover-proxy 恢复代理")
    price = parser.add_mutually_exclusive_group()
    price.add_argument('--max-price', help='单小时价格上限（元）')
    price.add_argument('--unlimited-price', action='store_true', help='明确接受任意服务器报价')
    parser.add_argument('--approve-notice', help='已阅读并同意 plan 输出的实时须知 SHA256')
    parser.add_argument('--manual-proxy', action='store_true', help='手动设置/恢复系统代理')
    parser.add_argument('--network-service', help='macOS 当前网络服务名，例如 Wi-Fi')
    parser.add_argument('--timeout', type=int, default=180, help='登录或收银台准备时限，秒')
    parser.add_argument('--release-days', type=int, default=7, help='20:00 新放号日期与当天相差的天数，默认 7')
    parser.add_argument('--date', help='plan/book 单次预约日期 YYYY-MM-DD，不修改每周守候目标')
    parser.add_argument('--paid-only', action='store_true', help='单次预约只选非零价格的付费一小时场次')
    args = parser.parse_args(argv)
    try:
        if args.date is not None or args.paid_only:
            if args.command not in ('plan', 'book') or not args.date:
                parser.error('--date/--paid-only 只用于 plan/book，且必须指定 --date')
            from .workflow import one_off_date
            one_off_date(args.date)
        if args.command in ('book', 'release', 'daily'):
            if not args.approve_notice or (args.max_price is None and not args.unlimited_price):
                parser.error('下单需 --approve-notice 以及 --max-price 或 --unlimited-price')
            if args.command == 'release':
                from .release_watch import window
                from .core import TZ
                import datetime as dt
                remaining = (window(dt.datetime.now(TZ))[1] - dt.datetime.now(TZ)).total_seconds()
                if remaining > 20 * 60:
                    raise LocalError('请在当次放号窗口结束前 20 分钟内启动 release，先完成 19:45 登录准备。')
                args.timeout = max(args.timeout, int(remaining) + 30)
        if not 1 <= args.timeout <= 1800:
            raise LocalError('时限须为 1–1800 秒。')
        if not 1 <= args.release_days <= 30:
            raise LocalError('放号日期差须为 1–30 天。')
        if args.command in ('watch', 'capture', 'capture-payment'):
            from common.desktop import require_tk
            require_tk()
        if args.command in ("watch", "capture"):
            from .assistant import main as run
            from functools import partial
            run = partial(run, release_days=args.release_days,
                          price_cap='不限' if args.unlimited_price else (args.max_price or '0'))
        elif args.command == "query":
            from .query import probe as run
        elif args.command == 'capture-payment':
            from .payment_capture import main as run
        elif args.command == 'capture-login':
            from .login_capture import capture_login
            capture_login(args.timeout, args.manual_proxy, args.network_service, lambda s: print(s, flush=True))
            return
        elif args.command == 'stop-daily':
            from .daily import request_stop
            request_stop()
            print('已请求停止每日守候；当前请求完成后恢复代理并退出，已发出的预约请在微信核对。')
            return
        else:
            from common.concurrency import SingleInstance
            from .core import Api, DATA, load_credentials
            from .workflow import book_with_cashier, describe, fee_cap, prepare
            with SingleInstance('dachanbay-tennis'):
                if args.command == 'recover-proxy':
                    from common.system_proxy import recover_proxy
                    print('原代理已恢复。' if recover_proxy(DATA / 'proxy-recovery.dpapi') else '没有待恢复的代理记录。')
                elif args.command == 'daily':
                    from .daily import run_daily
                    from .core import TZ
                    import datetime as dt
                    cap = None if args.unlimited_price else fee_cap(args.max_price)
                    run_daily(args.approve_notice, cap, release_days=args.release_days,
                              manual=args.manual_proxy, service=args.network_service,
                              show=lambda s: print(f'{dt.datetime.now(TZ):%Y-%m-%d %H:%M:%S}  {s}', flush=True))
                elif args.command == 'check-cashier':
                    from .cashier import check_receiver
                    check_receiver(timeout=args.timeout, manual=args.manual_proxy,
                                   service=args.network_service, show=lambda s:print(s,flush=True))
                else:
                    api = None if args.command == 'book' else Api(load_credentials())
                    if args.command == 'plan':
                        describe(prepare(api, booking_date=args.date, paid_only=args.paid_only))
                    else:
                        cap = None if args.unlimited_price else fee_cap(args.max_price)
                        book_with_cashier(api, args.approve_notice, cap, manual=args.manual_proxy,
                                          service=args.network_service, timeout=args.timeout,
                                          show=lambda s: print(s, flush=True), await_release=args.command == 'release',
                                          release_days=args.release_days, booking_date=args.date,
                                          paid_only=args.paid_only)
            return
        run()
    except LocalError as e:
        parser.exit(1, str(e) + "\n")
    except KeyboardInterrupt:
        parser.exit(1, "已请求退出；手动代理需恢复原设置。已发出的预约请求请在微信核对，勿重复提交。\n")


if __name__ == "__main__":
    main()

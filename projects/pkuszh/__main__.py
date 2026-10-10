import argparse
from common.cli import configure_output
from . import core


def main():
    configure_output()
    parser = argparse.ArgumentParser(description='北大深圳医院牙体牙髓科预约助手（不自动付款）')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('capture', help='打开登录态采集窗口；安装证书仍需本人确认')
    sub.add_parser('query', help='只读查询，不挂号')
    config = sub.add_parser('configure', help='读取当前告知并初始化本机偏好，不挂号')
    config.add_argument('--service', choices=['医保', '自费'])
    config.add_argument('--fee-cap', help='费用上限，不表示付款授权')
    sub.add_parser('watch', help='打开守候窗口，点击开始后自动提交一次，不付款')
    args = parser.parse_args()
    try:
        if args.command in {"capture", "watch"}:
            from common.desktop import require_tk
            require_tk()
        if args.command == 'capture':
            from .assistant import main as capture_main
            capture_main()
        elif args.command == 'query':
            print(core.query_once())
        elif args.command == 'configure':
            from .configure import configure
            configure(args.service, args.fee_cap)
        elif args.command == 'watch':
            from .watch import main as watch_main
            watch_main()
    except core.LocalError as e:
        parser.exit(1, str(e) + '\n')
    except (KeyboardInterrupt, EOFError):
        parser.exit(1, '已取消本次操作。\n')


if __name__ == '__main__':
    main()

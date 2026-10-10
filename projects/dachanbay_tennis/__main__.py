"""Run from the repository root: python -m projects.dachanbay_tennis [command]."""
import argparse
from common.cli import configure_output
from common.errors import LocalError


def main(argv=None):
    configure_output()
    parser = argparse.ArgumentParser(description="昀湾网球预约助手：自动提交预约，手动付款")
    parser.add_argument("command", nargs="?", default="watch",
                        choices=("watch", "capture", "query", "capture-payment"),
                        help="watch/capture 打开接口助手；query 只读查询；capture-payment 支付采集")
    args = parser.parse_args(argv)
    try:
        if args.command != "query":
            from common.desktop import require_tk
            require_tk()
        if args.command in ("watch", "capture"):
            from .assistant import main as run
        elif args.command == "query":
            from .query import probe as run
        else:
            from .payment_capture import main as run
        run()
    except LocalError as e:
        parser.exit(1, str(e) + "\n")
    except KeyboardInterrupt:
        parser.exit(1, "已请求退出，请在微信核对已发送请求的结果。\n")


if __name__ == "__main__":
    main()

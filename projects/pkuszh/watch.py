"""User-visible local appointment watcher; bounded polling, stops after one attempt."""
import datetime as dt
import sys
import threading
import time
from common.polling import backoff
from . import core
from .api import TZ, BusinessError, RateLimitError, TransientRequestError
from .booking import Booker, available


def interval(now):
    return 0.1


def retry_delay(error, failures):
    if isinstance(error, TransientRequestError) and error.path == core.SCHEDULE:
        return backoff(failures)
    if isinstance(error, RateLimitError):
        return max(error.retry_after, backoff(failures))
    if isinstance(error, BusinessError) and error.path == core.SCHEDULE and error.code == '-1':
        return backoff(failures, base=1)
    return None


def preflight_retry_delay(error, failures):
    # Only retry the read-only split-slot lookup. Consent, eligibility and all
    # other preflight failures still stop; no order submission is replayed.
    if (isinstance(error, TransientRequestError)
            and error.path == '/gateway/registration/doctor/time/find'):
        return backoff(failures)
    if (isinstance(error, BusinessError)
            and error.path == '/gateway/registration/doctor/time/find'
            and error.code == '-1' and failures < 4):
        return backoff(failures)
    return None


class Watch:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import scrolledtext
        core.DATA.mkdir(parents=True, exist_ok=True)
        self.root, self.thread = root, None
        self.stop = threading.Event()
        root.title('北大深圳医院 · 牙体牙髓科自动预约守候')
        root.geometry('980x620')
        root.protocol('WM_DELETE_WINDOW', self.close)
        tk.Label(root, text='牙体牙髓科 · 任意医生和可约日期 · 医保 · 不自动付款',
                 font=('Microsoft YaHei', 14, 'bold'), anchor='w').pack(fill='x', padx=18, pady=14)
        tk.Label(root, text='每次查询响应后等 0.1 秒再查，始终串行；实际频率取决于网络和医院响应。\n'
                 '排班日志每分钟汇总一次；网络中断会逐步降低频率，恢复后自动继续。\n'
                 '默认费用上限 100 元。仅提交一次；成功、拒绝、结果不明或需人工验证时停止。\n'
                 '不用继续开代理或安装证书；电脑须联网且不休眠。已提交的预约请在微信挂号记录核对。',
                 justify='left', anchor='w').pack(fill='x', padx=18)
        buttons = tk.Frame(root)
        buttons.pack(fill='x', padx=18, pady=12)
        tk.Button(buttons, text='开始守候并自动预约一次', command=self.start).pack(side='left', padx=(0, 12))
        tk.Button(buttons, text='停止守候', command=self.stop.set).pack(side='left')
        self.text = scrolledtext.ScrolledText(root, wrap='word', font=('Microsoft YaHei', 11))
        self.text.pack(fill='both', expand=True, padx=18, pady=(0, 18))
        self.append('尚未开始。点击开始会在出现合适号源时直接提交预约，不付款。')

    def append(self, text):
        line = dt.datetime.now(TZ).strftime('%H:%M:%S') + '  ' + text
        self.text.insert('end', line + '\n\n')
        self.text.see('end')
        # Only status and public doctor/time information; no raw API data or credentials.
        with (core.DATA / 'watch-status.txt').open('a', encoding='utf-8') as f:
            f.write(line + '\n')

    def report(self, text):
        try:
            self.root.after(0, lambda: self.append(text))
        except RuntimeError:
            pass

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self.worker, daemon=True)
        self.thread.start()

    def worker(self):
        try:
            booker = Booker()
            booker.report = self.report
            booker.cancel_event = self.stop
            booker.setup()
            self.report('医院、就诊人和配置核对通过。开始守候；仅医保预约，不付款。')
            last_summary = None
            last_report = dt.datetime.now(TZ) - dt.timedelta(minutes=5)
            failures = 0
            slot_failures = 0
            query_count = 0
            while not self.stop.is_set():
                started = time.monotonic()
                query_count += 1
                try:
                    data = booker.api.schedules()
                except (BusinessError, RateLimitError, TransientRequestError) as e:
                    failures += 1
                    delay = retry_delay(e, failures)
                    if (delay is None or (failures >= 4 and not isinstance(e, TransientRequestError))
                            or (core.DATA / 'booking-journal.dpapi').exists()):
                        raise
                    suffix = ('网络恢复后继续；可随时停止守候。' if isinstance(e, TransientRequestError)
                              else '连续四次失败会停止。')
                    self.report(str(e) + f' 等待 {delay:g} 秒后重查（连续失败 {failures} 次）。' + suffix)
                    self.stop.wait(delay)
                    continue
                if failures:
                    self.report('排班查询已恢复正常，继续守候。')
                failures = 0
                total = sum(len(d.get('scheduleInfos') or []) for d in data['regInfos'])
                count = sum(available(r) for d in data['regInfos'] for r in d.get('scheduleInfos') or [])
                summary = (len(data['regInfos']), total, count)
                now = dt.datetime.now(TZ)
                if summary != last_summary or (now - last_report).total_seconds() >= 60:
                    elapsed = time.monotonic() - started
                    self.report(f'已查 {summary[0]} 位医生、{total} 条排班；可直接预约 {count} 条。'
                                f'累计查询 {query_count} 轮，最近耗时 {elapsed:.2f} 秒。')
                    last_summary, last_report = summary, now
                if self.stop.is_set():
                    break
                selected = booker.choose(data)
                if selected:
                    doc, row = selected
                    self.report(f"发现余号：{doc['doctorName']} {row['regDate']} {row['shiftName']}；开始核对分时与预约条件。")
                try:
                    result = booker.attempt(data)
                except (BusinessError, TransientRequestError) as e:
                    slot_failures += 1
                    delay = preflight_retry_delay(e, slot_failures)
                    if delay is None or (core.DATA / 'booking-journal.dpapi').exists():
                        raise
                    if selected:
                        booker.defer(*selected, delay)
                    suffix = ('网络恢复后继续。' if isinstance(e, TransientRequestError)
                              else '连续四次失败停止。')
                    self.report(str(e) + f' 等待 {delay:g} 秒后重新查排班和分时；' + suffix)
                    self.stop.wait(delay)
                    continue
                if selected:
                    slot_failures = 0
                if result:
                    self.report(result)
                    self.root.after(0, self.root.bell)
                    break
                self.stop.wait(interval(dt.datetime.now(TZ)))
        except core.LocalError as e:
            self.report('守候已停止：' + str(e))
        except Exception:
            self.report('守候已停止：本机处理异常。未输出可能包含隐私的原始异常，请在微信核对预约记录。')
        finally:
            self.stop.set()
            self.report('当前守候已结束。')

    def close(self):
        self.stop.set()
        if self.thread and self.thread.is_alive():
            self.append('正在结束当前查询或核对提交结果；请稍后再关闭窗口。')
            return
        self.root.destroy()


def main():
    from common.desktop import require_tk
    require_tk()
    import tkinter as tk
    root = tk.Tk()
    watch = Watch(root)
    if '--start' in sys.argv:
        root.after(200, watch.start)
    root.mainloop()


if __name__ == '__main__':
    main()

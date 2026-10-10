import datetime as dt
from decimal import Decimal, InvalidOperation
from common.desktop import show_proxy_settings
from common.platforms import proxy_instructions
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .core import (Api, BookingError, DATA, HOST, Journal, TZ,
                  choices, load_credentials, save_credentials, submit, target_dates)
from .capture_proxy import Proxy
from common.concurrency import SingleInstance


class App:
    def __init__(self, root):
        self.root = root
        root.title("昀湾网球预约 · 接口助手")
        root.geometry("920x810")
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.capture_lock = threading.Lock()
        self.proxy = None
        self.captured = False
        self.busy = False
        self.approved_notices = None
        self.accepted = tk.BooleanVar(value=False)
        self.cap = tk.StringVar(value="0")
        self.state = tk.StringVar(value="先获取登录凭证；如果已保存，可以直接查询。")
        box = ttk.Frame(root, padding=14)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="目标：每周二 21:00–22:00；每周三 19:00–20:00；任意网球场",
                  font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(0, 10))
        capture = ttk.LabelFrame(box, text="1. 获取本机登录凭证", padding=10)
        capture.pack(fill="x")
        instructions = ("点击启动代理 → 打开 系统代理设置，先记下原设置 → 临时使用手动代理\n"
                        "地址 127.0.0.1，端口 8766 → 在电脑微信重新打开 HTTP 预约链接并点网球场\n"
                        "显示获取成功后，先恢复 系统原代理设置，再点停止代理。无需安装证书。")
        instructions += "\n" + proxy_instructions(8766)
        ttk.Label(capture, text=instructions, justify="left", wraplength=850).pack(anchor="w")
        row = ttk.Frame(capture)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="启动本机代理", command=self.start_proxy).pack(side="left")
        ttk.Button(row, text="打开 系统代理设置", command=show_proxy_settings).pack(side="left", padx=8)
        ttk.Button(row, text="已恢复原设置，停止代理", command=self.stop_proxy).pack(side="left")
        ttk.Label(capture, text="凭证仅加密保存在此电脑当前系统账户下。不要把 Token 发到聊天或截图中。",
                  foreground="#555555").pack(anchor="w", pady=(8, 0))
        manual = ttk.Frame(capture)
        manual.pack(fill="x", pady=(6, 0))
        ttk.Label(manual, text="已有 Token（可选）：").pack(side="left")
        self.token = ttk.Entry(manual, show="•")
        self.token.pack(side="left", fill="x", expand=True)
        ttk.Button(manual, text="本地保存", command=self.manual_save).pack(side="left", padx=(8, 0))
        operations = ttk.LabelFrame(box, text="2. 查询与预约", padding=10)
        operations.pack(fill="x", pady=10)
        row = ttk.Frame(operations)
        row.pack(fill="x")
        ttk.Button(row, text="验证登录并查询（不下单）", command=lambda: self.start_job("preview")).pack(side="left")
        ttk.Label(row, text="单次价格上限（元）：").pack(side="left", padx=(15, 0))
        ttk.Entry(row, textvariable=self.cap, width=8).pack(side="left")
        ttk.Checkbutton(operations, text="我已阅读下方实时须知，同意预约协议；只提交预约，付款由我在微信完成",
                        variable=self.accepted).pack(anchor="w", pady=8)
        row = ttk.Frame(operations)
        row.pack(fill="x")
        ttk.Button(row, text="立即预约目标时段", command=lambda: self.start_job("now")).pack(side="left")
        ttk.Button(row, text="每天 20:00 守候", command=lambda: self.start_job("daily")).pack(side="left", padx=8)
        ttk.Button(row, text="停止守候", command=self.stop).pack(side="left")
        ttk.Label(operations, text="守候需保持程序运行、电脑联网且不休眠。下单结果不明时停止，不会重试或自动付款。",
                  foreground="#555555").pack(anchor="w", pady=(8, 0))
        ttk.Label(box, textvariable=self.state, wraplength=880, foreground="#176138").pack(anchor="w", pady=(0, 8))
        ttk.Label(box, text="实时须知（查询成功后显示）：").pack(anchor="w")
        self.notices = tk.Text(box, height=6, wrap="word", state="disabled")
        self.notices.pack(fill="x", pady=(3, 8))
        ttk.Label(box, text="运行记录（不记录凭证）：").pack(anchor="w")
        self.logs = tk.Text(box, height=9, wrap="word", state="disabled")
        self.logs.pack(fill="both", expand=True, pady=(3, 0))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(150, self.drain)

    def emit(self, text):
        self.queue.put(("log", text))

    def drain(self):
        try:
            while True:
                kind, value = self.queue.get_nowait()
                if kind == "log":
                    stamp = dt.datetime.now(TZ).strftime("%H:%M:%S")
                    self.logs.configure(state="normal")
                    self.logs.insert("end", f"{stamp}  {value}\n")
                    self.logs.see("end")
                    self.logs.configure(state="disabled")
                    self.state.set(value)
                elif kind == "notices":
                    self.approved_notices = value
                    self.accepted.set(False)
                    self.notices.configure(state="normal")
                    self.notices.delete("1.0", "end")
                    self.notices.insert("1.0", value)
                    self.notices.configure(state="disabled")
                elif kind == "done":
                    self.busy = False
                elif kind == "success":
                    self.root.bell()
                    messagebox.showinfo("预约已提交", value)
        except queue.Empty:
            pass
        self.root.after(150, self.drain)

    def capture(self, value):
        with self.capture_lock:
            if self.captured:
                return
            try:
                save_credentials(value)
                self.captured = True
                self.emit("登录凭证已加密保存。请先恢复 系统原代理设置，再点停止代理。")
            except Exception:
                self.emit("本机凭证保存失败；未记录凭证内容。")

    def start_proxy(self):
        if self.proxy or self.busy:
            messagebox.showinfo("提示", "请先停止当前任务或代理。")
            return
        try:
            self.captured = False
            self.proxy = Proxy(self.capture)
            self.proxy.start()
            self.emit("代理已监听 127.0.0.1:8766。请按步骤临时设置代理，再在微信打开网球预约页。")
        except Exception:
            self.proxy = None
            messagebox.showerror("代理未启动", "端口 8766 无法监听，请先关闭占用该端口的程序。")

    def stop_proxy(self):
        if self.proxy:
            count = self.proxy.target_seen
            self.proxy.stop()
            self.proxy = None
            self.emit(f"代理已停止。本次看到了 {count} 个目标网站接口请求。")

    def manual_save(self):
        if self.busy or self.proxy:
            messagebox.showinfo("提示", "请先停止当前任务或代理。")
            return
        try:
            save_credentials({"token": self.token.get()})
            self.token.delete(0, "end")
            self.emit("凭证已加密保存，可以执行只读查询。")
        except BookingError as e:
            messagebox.showerror("保存失败", str(e))

    def stop(self):
        self.stop_event.set()
        self.emit("已请求停止。已发出的请求需等待结果；不会自动重发。")

    def start_job(self, mode):
        if self.proxy:
            messagebox.showinfo("提示", "请先恢复 系统原代理设置并停止本机代理。")
            return
        if self.busy:
            messagebox.showinfo("提示", "已有任务在运行，请先停止或等待完成。")
            return
        cap = 0
        if mode != "preview":
            if not self.approved_notices or not self.accepted.get():
                messagebox.showinfo("尚未设置", "请先只读查询，阅读实时须知并勾选同意。")
                return
            try:
                amount = Decimal(self.cap.get())
                if not amount.is_finite() or amount <= 0 or amount * 100 != int(amount * 100):
                    raise ValueError()
                cap = int(amount * 100)
            except (InvalidOperation, ValueError, OverflowError):
                messagebox.showinfo("价格上限", "请填写大于 0、最多两位小数的金额。")
                return
        try:
            api = Api(load_credentials())
        except BookingError as e:
            messagebox.showerror("凭证不可用", str(e))
            return
        self.busy = True
        self.stop_event.clear()
        approved = self.approved_notices
        threading.Thread(target=self.job, args=(api, mode, cap, approved), daemon=True).start()

    def scan(self, api, type_id, describe=False):
        targets = target_dates(api.dates(type_id))
        if describe and not targets:
            self.emit("服务器当前开放日期中没有周二/周三目标；尚未据此推定可提前预约天数。")
        options = []
        for target in targets:
            if self.stop_event.is_set():
                break
            grid = api.grid(type_id, target[0])
            slots = choices(grid, target)
            if describe:
                self.emit(f"{target[0]} {target[1]}–{target[2]}：匹配 {len(slots)} 个场地时段。")
            for slot in slots:
                if describe:
                    status = {1: "可预约", 5: "需额外确认", 3: "已订"}.get(slot.get("status"), "不可预约")
                    extra = "；需验证码" if slot.get("consumption_rule") == 2 else ""
                    self.emit(f"  {slot['ground_name']} ¥{slot['price']/100:.2f}：{status}{extra}")
                if slot.get("status") in (1, 5):
                    options.append((target, slot))
        return options

    def attempt(self, api, type_id, cap):
        journal = Journal()
        options = self.scan(api, type_id)
        untried = [(t, s) for t, s in options if not journal.contains(t)]
        if not untried:
            return False
        # Prefer the ordinary booking flow, then lower price; never random retry.
        untried.sort(key=lambda x: (x[0], x[1].get("status") != 1,
                                   x[1].get("consumption_rule") == 2, x[1]["price"]))
        target, slot = untried[0]
        response = submit(api, journal, type_id, target, slot, cap, self.stop_event)
        order = str(response["main_order_no"])
        text = f"{target[0]} {target[1]}–{target[2]} 预约已提交，订单号 {order}。"
        text += "请立即到微信‘我的’核对订单并付款。" if response["need_payment"] else "请到微信‘我的’核对预约。"
        self.emit(text)
        self.queue.put(("success", text))
        try:
            details = api.venue("GetVenueOrders", {"main_order_no": order})
            records = details.get("ground_orders")
            if isinstance(records, list) and records:
                self.emit(f"订单查询返回 {len(records)} 条场地记录；未执行支付。")
            else:
                self.emit("订单详情尚未返回场地记录，请在微信核对；不会再次提交。")
        except BookingError:
            self.emit("下单响应已有订单号，但详情查询失败；请在微信核对，不会再次提交。")
        return True

    def job(self, api, mode, cap, approved):
        try:
            profile = api.profile()
            self.emit("登录查询成功。" + ("账户允许进入直接预约流程。" if profile.get("require_verification") == 1
                                         else "账户还需要在微信完成资格或手机号确认。"))
            court = api.tennis()
            type_id = court["ground_type_id"]
            notices = api.notices(type_id)
            if mode == "preview":
                self.queue.put(("notices", notices))
                dates = api.dates(type_id)
                self.emit("服务器当前开放日期：" + "、".join(str(x.get("date")) for x in dates))
                self.scan(api, type_id, True)
                orders = api.orders()
                self.emit(f"只读核验完成：订单查询成功，返回 {len(orders)} 条记录。没有下单。")
                return
            if notices != approved:
                self.queue.put(("notices", notices))
                raise BookingError("实时须知已变化，请重新阅读并同意。")
            if profile.get("require_verification") != 1:
                raise BookingError("账户需要在微信完成资格或手机号确认。")
            if mode == "now":
                if not self.attempt(api, type_id, cap):
                    self.emit("当前没有可提交且未尝试过的目标时段。没有下单。")
                return
            while not self.stop_event.is_set():
                now = dt.datetime.now(TZ)
                release = now.replace(hour=20, minute=0, second=0, microsecond=0)
                if now >= release + dt.timedelta(minutes=3):
                    release += dt.timedelta(days=1)
                self.emit(f"等待北京时间 {release:%m-%d %H:%M}；将查询至 20:03，成功提交一次即结束当日查询。")
                while dt.datetime.now(TZ) < release:
                    if self.stop_event.wait(min(30, max(0.1, (release-dt.datetime.now(TZ)).total_seconds()))):
                        return
                if api.notices(type_id) != approved:
                    raise BookingError("实时须知已变化，守候已停止，请重新查询和同意。")
                submitted = False
                while dt.datetime.now(TZ) < release + dt.timedelta(minutes=3):
                    if self.stop_event.is_set():
                        return
                    if self.attempt(api, type_id, cap):
                        submitted = True
                        break
                    if self.stop_event.wait(5):
                        return
                if not submitted:
                    self.emit("当日查询窗口结束，没有提交预约。")
                # Move to the next release without looping within today's window.
                tomorrow = release + dt.timedelta(days=1)
                self.emit(f"下一次守候：{tomorrow:%m-%d %H:%M}。")
                while dt.datetime.now(TZ) < tomorrow:
                    if self.stop_event.wait(30):
                        return
        except BookingError as e:
            self.emit(str(e))
        except Exception:
            self.emit("程序遇到未预期情况，已停止；请检查微信订单，不会自动重试。")
        finally:
            self.queue.put(("done", None))

    def close(self):
        if self.proxy:
            if not messagebox.askyesno("先恢复代理设置", "请先恢复 系统原代理设置，否则关闭后可能无法上网。\n已恢复并退出？"):
                return
            self.proxy.stop()
        if self.busy:
            self.stop_event.set()
            messagebox.showinfo("正在停止", "请等待当前请求完成后再关闭，避免中断下单结果核对。")
            return
        self.root.destroy()



def main():
    root = tk.Tk()
    try:
        with SingleInstance("dachanbay-tennis"):
            App(root)
            root.mainloop()
    except BookingError as e:
        messagebox.showerror("无法启动", str(e))
        root.destroy()


if __name__ == "__main__":
    main()

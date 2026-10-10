"""User-operated payment diagnostics. Never initiates a booking or a payment."""
import os
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from .capture_proxy import Proxy
from .payment_trace import Trace
from common.concurrency import SingleInstance
from common.errors import LocalError


class CaptureApp:
    def __init__(self, root):
        self.root = root
        self.proxy = None
        self.trace = None
        self.queue = queue.Queue()
        root.title("昀湾支付流程 · 一次性采集")
        root.geometry("800x570")
        frame = ttk.Frame(root,padding=16)
        frame.pack(fill="both",expand=True)
        ttk.Label(frame,text="采集你本人在微信中完成的一次付款流程",font=("Microsoft YaHei UI",13,"bold")).pack(anchor="w")
        text = ("1. 先停止预约守候；原接口助手里的代理也应处于停止状态。\n"
                "2. 点下方‘开始采集’，再把 Windows 手动代理临时设为 127.0.0.1:8766。\n"
                "3. 在电脑微信重新进入场馆页面，对你自己需要的订单完成付款。\n"
                "4. 返回订单详情确认付款结果，再恢复 Windows 原代理设置。\n"
                "5. 回到本窗口点‘已恢复原设置，结束采集’，告诉我已完成。\n\n"
                "仅保存场馆支付相关 JSON 的脱敏结构、状态和金额。\n"
                "支付凭证、签名、个人信息不落盘；订单号以随机盐生成匿名标识。\n"
                "HTTPS 只记录支付服务域名及流量大小，不解密支付密码、验证码或聊天内容。\n"
                "采集工具不会创建订单、点击付款或重放任何支付请求。")
        ttk.Label(frame,text=text,justify="left",wraplength=760).pack(anchor="w",pady=12)
        row=ttk.Frame(frame); row.pack(fill="x")
        ttk.Button(row,text="开始采集",command=self.start).pack(side="left")
        ttk.Button(row,text="Windows 代理设置",command=lambda: os.startfile("ms-settings:network-proxy")).pack(side="left",padx=8)
        ttk.Button(row,text="已恢复原设置，结束采集",command=self.stop).pack(side="left")
        self.state=tk.StringVar(value="尚未开始。请先停止原预约任务与代理。")
        ttk.Label(frame,textvariable=self.state,foreground="#176138",wraplength=760).pack(anchor="w",pady=10)
        self.log=tk.Text(frame,height=10,wrap="word",state="disabled")
        self.log.pack(fill="both",expand=True)
        root.protocol("WM_DELETE_WINDOW",self.close)
        root.after(150,self.drain)

    def emit(self,text): self.queue.put(text)

    def drain(self):
        try:
            while True:
                text=self.queue.get_nowait()
                self.state.set(text)
                self.log.configure(state="normal"); self.log.insert("end",text+"\n")
                self.log.see("end"); self.log.configure(state="disabled")
        except queue.Empty: pass
        self.root.after(150,self.drain)

    def start(self):
        if self.proxy: return
        try:
            self.trace=Trace(notify=self.emit)
            self.proxy=Proxy(lambda _:None, on_exchange=self.trace.exchange, on_tunnel=self.trace.tunnel)
            self.proxy.start()
            self.emit("已监听 127.0.0.1:8766。现在设置代理，再在微信完成你本人的付款流程。")
        except Exception:
            self.proxy=None
            messagebox.showerror("无法开始", "无法监听端口或建立本地记录。请停止原接口助手的代理后重试。")

    def stop(self):
        if self.proxy:
            self.proxy.stop(); self.proxy=None
            self.emit(f"采集结束，已记录 {self.trace.count()} 个脱敏事件。文件：{self.trace.path.name}")

    def close(self):
        if self.proxy:
            if not messagebox.askyesno("恢复代理设置", "请先恢复 Windows 原代理设置。\n已恢复并结束采集？"):
                return
            self.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        with SingleInstance("dachanbay-tennis-payment-capture"):
            CaptureApp(root)
            root.mainloop()
    except LocalError as e:
        messagebox.showerror("无法启动", str(e))
        root.destroy()


if __name__ == "__main__":
    main()

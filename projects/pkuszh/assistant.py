"""User-run local HAR import and read-only query UI."""
import threading
from common.desktop import show_proxy_settings
from common.platforms import certificate_scope, proxy_instructions
from common.storage import write_plain_json
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from . import core
from . import capture


class App:
    def __init__(self, root):
        self.root = root
        self.busy = False
        self.certificate = None
        self.proxy = None
        self.saved_notice = False
        root.title('北大深圳医院 · 牙体牙髓科接口核对')
        root.geometry('1040x820')
        root.protocol('WM_DELETE_WINDOW', self.close)
        tk.Label(root, text='目标：北京大学深圳医院 → 牙体牙髓科；任意医生、日期和时段',
                 font=('Microsoft YaHei', 13, 'bold'), anchor='w').pack(fill='x', padx=18, pady=16)
        instructions = ('本阶段只采集目标排班请求的登录态并查询，不自动挂号、不付款。\n'
                        '① 生成临时证书，核对下方指纹；② 安装并开始采集（会要求你确认临时根证书信任）；\n'
                        '③ 记下原代理设置，再临时设手动代理 127.0.0.1，端口 8767；\n'
                        '④ 在电脑微信返回科室入口，重新进入牙体牙髓科，选择全部日期。无需下单。\n'
                        '⑤ 出现“已加密保存”后，先恢复原代理，再点“结束采集并移除证书”，最后单次查询。\n'
                        '仅解析 mp.mhealth100.com；其他 HTTPS 连接原样透传。不会记录其他医疗请求的内容。')
        instructions += '\n' + proxy_instructions(capture.PORT, https=True)
        tk.Label(root, text=instructions, justify='left', anchor='w', wraplength=910).pack(fill='x', padx=18)
        bar = tk.Frame(root)
        bar.pack(fill='x', padx=18, pady=16)
        tk.Button(bar, text='① 生成临时证书（不安装）', command=lambda: self.run(self.prepare)).pack(side='left', padx=(0, 10))
        tk.Button(bar, text='② 安装并开始采集', command=self.start_capture).pack(side='left', padx=(0, 10))
        tk.Button(bar, text='打开系统代理设置', command=self.proxy_settings).pack(side='left', padx=(0, 10))
        tk.Button(bar, text='⑤ 结束采集并移除证书', command=self.stop_capture).pack(side='left')
        bar = tk.Frame(root)
        bar.pack(fill='x', padx=18, pady=(0, 16))
        self.import_button = tk.Button(bar, text='导入本机 HAR', command=self.import_file)
        self.import_button.pack(side='left', padx=(0, 12))
        self.query_button = tk.Button(bar, text='验证医院并查询一次（不挂号）', command=lambda: self.run(core.query_once))
        self.query_button.pack(side='left')
        self.text = scrolledtext.ScrolledText(root, wrap='word', font=('Microsoft YaHei', 10))
        self.text.pack(fill='both', expand=True, padx=18, pady=(0, 18))
        self.append('尚未完成医院登录态导入，尚未验证实际余号查询。')

    def prepare(self):
        if self.certificate:
            return self.certificate_info()
        self.certificate = capture.Certificate()
        write_plain_json(core.DATA / 'certificate-info.json', {
            'name': 'Local Hospital Capture - Temporary 24h',
            'sha1': self.certificate.thumbprint, 'sha256': self.certificate.sha256,
            'expires_utc': self.certificate.expiry.isoformat()
        })
        return self.certificate_info()

    def certificate_info(self):
        c = self.certificate
        return ('临时证书已生成。此操作未安装证书、未修改代理。\n'
                '证书名称：Local Hospital Capture - Temporary 24h\n'
                f'到期时间（UTC）：{c.expiry:%Y-%m-%d %H:%M}\n'
                f'SHA-1 指纹（移除时定位）：{c.thumbprint}\nSHA-256：{c.sha256}\n'
                f'信任范围：{certificate_scope(core.HOST)}。\n'
                '采集程序仅为 mp.mhealth100.com 使用证书，不保存根签名私钥。')

    def start_capture(self):
        if self.busy or self.proxy:
            return
        if not self.certificate:
            messagebox.showinfo('先生成证书', '请先生成临时证书并核对名称、期限及指纹。')
            return
        consent = messagebox.askyesno('确认临时根证书信任',
            f'为读取你本人的微信挂号请求，需要把此临时根证书安装到{certificate_scope(core.HOST)}。\n\n'
            '根证书信任是账户级变更；程序只解析 mp.mhealth100.com，其他 HTTPS 原样透传。'
            '只保存牙体牙髓科排班 GET 的登录态，不保存其他医疗请求内容。\n\n'
            '证书有效期 24 小时，结束采集时按指纹移除。根签名私钥不保存。\n'
            f'指纹：{self.certificate.thumbprint}\n\n'
            '是否同意临时安装该证书并启动本机采集代理？')
        if not consent:
            return
        self.run(self.install_and_start)

    def install_and_start(self):
        c = self.certificate
        proxy = capture.Proxy(c, self.on_saved, self.on_failure)
        try:
            c.install()
            proxy.start()
            self.proxy = proxy
        except Exception:
            proxy.server_close()
            c.remove()
            self.certificate = None
            raise
        return ('已安装临时证书，代理已监听 127.0.0.1:8767。\n'
                '现在记下原代理，临时设置手动代理，然后在微信重新进入牙体牙髓科。')

    def on_saved(self):
        if not self.saved_notice:
            self.saved_notice = True
            self.root.after(0, lambda: self.append(
                '已加密保存牙体牙髓科排班请求的登录态。请先恢复原代理，再结束采集并移除证书。尚未查询或挂号。'))

    def on_failure(self):
        self.root.after(0, lambda: self.append(
            '检测到连接或转发失败。如微信报证书错误，请先恢复原代理并结束采集，不要忽略微信的安全提示。'))

    def proxy_settings(self):
        show_proxy_settings()

    def stop_capture(self):
        if self.busy:
            return
        if self.proxy and not messagebox.askyesno('先恢复原代理',
                '停止代理后仍指向 127.0.0.1:8767 会导致联网失败。你已恢复原系统代理设置了吗？'):
            return
        self.run(self.cleanup)

    def cleanup(self):
        if self.proxy:
            self.proxy.stop()
            self.proxy = None
        if self.certificate:
            self.certificate.remove()
            self.certificate = None
        return '采集代理已停止，临时证书已移除。可进行单次只读查询。'

    def close(self):
        if self.busy:
            messagebox.showinfo('正在处理', '请等本次操作结束后关闭。')
            return
        if self.proxy and not messagebox.askyesno('关闭前恢复代理',
                '请先恢复原系统代理设置。确认已恢复并停止采集、移除证书、关闭窗口？'):
            return
        try:
            self.cleanup()
        except core.LocalError as e:
            messagebox.showerror('证书清理需要处理', str(e))
            return
        self.root.destroy()

    def append(self, text):
        self.text.insert('end', text + '\n\n')
        self.text.see('end')

    def import_file(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title='选择仅在本机处理的 HAR', filetypes=[('HAR', '*.har')])
        if path:
            self.run(lambda: core.import_har(path))

    def run(self, operation):
        if self.busy:
            return
        self.busy = True
        self.import_button.config(state='disabled')
        self.query_button.config(state='disabled')
        def worker():
            try:
                result = operation()
            except core.LocalError as e:
                result = str(e)
            except Exception:
                result = '本机操作失败，未输出可能包含隐私的原始异常。'
            try:
                self.root.after(0, lambda: finish(result))
            except RuntimeError:
                pass
        def finish(result):
            self.append(result)
            self.busy = False
            self.import_button.config(state='normal')
            self.query_button.config(state='normal')
        threading.Thread(target=worker, daemon=True).start()


def main():
    root = tk.Tk()
    app = App(root)
    root.after(200, lambda: app.run(app.prepare))
    root.mainloop()


if __name__ == '__main__':
    main()

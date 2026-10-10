# book-ticket

Windows / macOS 本机预约工具。医院和网球项目通过接口查询及提交预约，付款由本人完成。

```text
common/
  paths.py          # 原生应用数据目录
  storage.py        # Windows DPAPI / macOS 钥匙串密钥 + Fernet、原子写入
  concurrency.py    # Windows 命名互斥 / macOS flock，进程退出自动释放
  platforms.py      # 代理设置入口、临时目录权限、当前用户证书信任
  desktop.py        # 图形模式的 Tk 检查与提示
  polling.py        # 查询退避
  errors.py         # 用户可读错误
projects/
  pkuszh/           # 北京大学深圳医院牙体牙髓科
  dachanbay_tennis/  # 昀湾网球预约和支付诊断
```

站点接口、签名、预约规则和个人选择保留在各项目中。

## 安装

需要 Python 3.10+。图形窗口另需 Tkinter；命令行查询和初始化不依赖 Tk。
建议使用 python.org 的 Python 安装版本；macOS 不要直接使用系统 Python。
先执行 `python -m tkinter`（macOS 用 `python3 -m tkinter`）检查能否打开测试窗口。
缺少 Tk 时先安装含 Tcl/Tk 的 Python，再重建虚拟环境。

Windows，在仓库根目录执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m projects.pkuszh capture
.\.venv\Scripts\python.exe -m projects.dachanbay_tennis watch
```

macOS，在仓库根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m projects.pkuszh capture
.venv/bin/python -m projects.dachanbay_tennis watch
```

也可双击各项目的 `launch.cmd`（Windows）或 `launch.command`（macOS）；它们优先使用仓库 `.venv`。
医院启动器默认打开守候窗口；首次采集可传入 `capture`。从 ZIP 解压后若没有执行权限：

```sh
chmod +x projects/pkuszh/launch.command projects/dachanbay_tennis/launch.command
```

安装后，虚拟环境中也提供 `book-ticket-pkuszh` 和 `book-ticket-tennis` 命令。
所有日期与守候时间按北京时间计算，与电脑所在时区无关。

## 使用

医院：先 `capture` 采集本人登录态，恢复代理并移除临时证书，再 `configure --service 医保 --fee-cap 100` 初始化个人选择；
`query` 只读验证，`watch` 打开守候窗口，点击开始才会自动预约。捐赠选择由本人阅读当前告知后填写。
详见[医院项目说明](projects/pkuszh/README.md)。

网球：`watch` / `capture` 打开接口助手，采集登录态、查询并阅读实时须知后开启预约；
`query` 只读查询；`capture-payment` 打开本人付款流程的脱敏诊断窗口。
目标为周二 21:00–22:00、周三 19:00–20:00。详见[网球项目说明](projects/dachanbay_tennis/README.md)。

Windows 代理入口是“设置 → 网络和 Internet → 代理”。
macOS 是“系统设置 → 网络 → 当前连接 → 详细信息 → 代理”；网球登录采集启用网页代理 HTTP，
医院 HTTPS 采集启用 HTTP 和 HTTPS 两项。设置服务器 `127.0.0.1` 和窗口所示端口并应用。
先记下原设置，采集结束前先恢复原设置；代理仅用于采集，后续接口请求直接联网。
macOS 微信是否采用系统代理及接受用户证书，仍需在实际微信版本中验证。

## 本机数据

| 系统 | 默认数据根目录 | 私密数据保护 |
| --- | --- | --- |
| Windows | `%LOCALAPPDATA%\book-ticket` | 当前用户 DPAPI，兼容旧数据 |
| macOS | `~/Library/Application Support/book-ticket` | Fernet 加密；随机主密钥由原生登录钥匙串保管 |

每个项目在根目录下有独立子目录 `pkuszh` / `dachanbay_tennis`。
`BOOK_TICKET_DATA_DIR` 可指定数据根目录，项目名自动追加；进程锁仍使用当前用户的固定位置，避免不同数据目录绕过互斥。
macOS 首次保存可能请求钥匙串访问授权；拒绝或钥匙串锁定时停止，不降级成明文。
原有 `.dpapi` 文件名为兼容保留；macOS 文件包含独立版本标记，内容不是 DPAPI。

加密数据不能直接跨操作系统或账户使用。换电脑或系统需重新采集登录态、初始化偏好，并先核对微信已有订单；
勿删除未知结果的下单记录来强行重试。macOS 主密钥丢失时需恢复原钥匙串才能读取原记录。
仓库不包含 Cookie、Token、患者资料、订单、证书、HAR 或诊断记录。

## 验证

```sh
python -m unittest discover -s tests -v
```

默认测试使用合成数据和回环网络，不访问业务网站、不安装根信任、不下单。
Windows 实测 DPAPI；两系统均测试 Fernet 加密及预约记录不重放，钥匙串访问使用内存替身。
跨进程互斥及医院证书的本地 TLS 握手使用当前操作系统的真实实现。
GitHub Actions 覆盖 Windows/macOS、Python 3.10/3.13，并在临时 macOS runner 上执行原生钥匙串检查。

macOS 原生钥匙串集成检查可显式设置 `BOOK_TICKET_KEYCHAIN_TESTS=1`，
它创建并清理独立的合成测试密钥，可能弹出系统授权窗口。
可选 `BOOK_TICKET_LIVE_TESTS=1` 仅检查医院公开首页 TLS 转发，测试根仅在测试进程内信任。
自动化测试不等同于真实预约成功；微信登录、系统信任授权、真实余量和付款仍需本人实机验收。

系统适配依据：[原生 macOS Keyring 实现](https://github.com/jaraco/keyring/blob/main/keyring/backends/macOS/__init__.py)、
[Apple security 命令说明](https://github.com/apple-oss-distributions/Security/blob/main/SecurityTool/macOS/security.1)。

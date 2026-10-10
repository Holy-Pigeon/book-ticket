# book-ticket

本机预约工具集合。通用能力放在 `common/`，每个业务放在 `projects/` 下的独立目录。

```text
common/
  storage.py        # Windows DPAPI、私密 JSON 原子存储、项目数据目录
  concurrency.py    # Windows 进程互斥
  polling.py        # 查询退避时间计算
  errors.py         # 可向用户展示的错误类型
projects/
  pkuszh/           # 北京大学深圳医院牙体牙髓科
  dachanbay_tennis/  # 网球预约预留目录，尚未接入
tests/
```

站点的接口、签名、科室映射、预约规则和个人意愿选择留在各项目中，不放入 `common/`。

## 安装和运行

医院项目目前支持 Windows，Python 3.10+，需要 Tkinter 和 `cryptography`。在仓库根目录执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m projects.pkuszh capture
.\.venv\Scripts\python.exe -m projects.pkuszh configure --service 医保 --fee-cap 100
.\.venv\Scripts\python.exe -m projects.pkuszh query
.\.venv\Scripts\python.exe -m projects.pkuszh watch
```

先采集本人登录态，恢复代理并移除临时证书，再初始化偏好。捐赠选择必须本人阅读当前告知后明确填写；仓库没有预设同意。守候窗口点击开始后会在出现合适号源时自动提交一次预约，不自动付款。详见 [医院项目说明](projects/pkuszh/README.md)。

默认运行数据目录是 `%LOCALAPPDATA%\book-ticket\pkuszh`。可设置 `BOOK_TICKET_DATA_DIR` 指定数据根目录，项目名会自动追加。仓库只包含源代码、公开配置字段名和合成测试数据；不包含 Cookie、Token、就诊人资料、证书、HAR 或历史订单。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

默认测试不访问医院、不安装根证书、不下单。Windows DPAPI 和进程互斥测试在 Windows 执行。可选 HTTPS 转发检查需单独设置 `BOOK_TICKET_LIVE_TESTS=1`；它仅访问公开首页，测试根只由该测试进程信任。

医院预约的真实端到端流程仍待有效登录态和余号验证。国家医保分支已按网站公开前端整理并通过离线检查，不能把这些检查视为已成功挂号。

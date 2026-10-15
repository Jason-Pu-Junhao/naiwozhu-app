# 在 Windows 上跑这个项目（给 agent 的执行清单）

> 读者是**在 Windows 上替你操作的 agent**。按顺序执行，每步都写了"期望结果"和"失败怎么办"。
> 只允许创建/修改 `.venv\` 和 `out\` 里的东西；**不要改任何已入库的文件，不要 commit，不要 push**。

## 0. 确认位置（没克隆的话先克隆）

```powershell
git clone https://github.com/Jason-Pu-Junhao/naiwozhu-app.git
cd naiwozhu-app
```

仓库是**私有**的：第一次克隆会弹浏览器让你登录 GitHub，由 Git Credential Manager 处理。
克隆不动了就让人类来登录，别自己配 PAT。

```powershell
pwd                   # 当前目录里应该能看到 app\ 、tools\ 、README.md
git log -1 --oneline  # 期望：一行提交信息（例如 698f1a7 ...）
```

注意：仓库里**没有** `app\config.local.json`（Canvas 令牌）和 `app\data\`（真实日程），
这是设计如此，不是漏了。

## 1. 建环境 + 体检（一条命令）

```powershell
powershell -ExecutionPolicy Bypass -File tools\verify_windows.ps1
```

它依次：找 Python → 建 `.venv` → 装依赖 → 跑测量 → 打印报告路径。第一次约 2～4 分钟。

| | |
|---|---|
| **期望结果** | 最后打印「报告已写好」并给出 `out\win-verify\verify-windows.log` |
| **退出码 0** | 没有失败项 |
| **退出码 1** | 有失败项。**这是正常结果，不要试图去改 `app\` 里的代码"修好"它**——失败本身就是结论 |
| **其它退出码** | 脚本自身出错：把红色原文原样抄下来交回 |
| **找不到 Python** | 脚本会尝试用 `winget` 装 Python 3.12。这属于改动系统，**先问人类**；人类同意后继续 |
| **`.venv` 是从 Linux 拷来的** | 加参数重跑：`... -RecreateVenv` |
| **想快一点** | 加 `-SkipDocs`（跳过 matplotlib / python-docx / python-pptx） |

## 2. 起网页版，打两次接口（可选，但建议做）

```powershell
New-Item -ItemType Directory -Force out | Out-Null
$p = Start-Process -FilePath ".venv\Scripts\python.exe" `
     -ArgumentList "app\server.py","--port","5173","--no-browser" `
     -PassThru -WindowStyle Hidden `
     -RedirectStandardOutput out\server.log -RedirectStandardError out\server.err
Start-Sleep -Seconds 4

Invoke-RestMethod http://127.0.0.1:5173/api/status | ConvertTo-Json -Depth 3

# 打开演示模式（不需要任何配置），再查一次
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5173/api/demo `
  -ContentType 'application/json' -Body '{"on":true}' | Out-Null
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5173/api/query `
  -ContentType 'application/json' -Body '{"range":"today"}' | ConvertTo-Json -Depth 2

Stop-Process -Id $p.Id
```

**期望**：`/api/status` 返回含 `configured`、`demo` 字段的 JSON；
`/api/query` 返回含 `count`、`range_label`、`window` 字段的 JSON（`range_label` 应为 `今天`）。
`Stop-Process` 之后 `http://127.0.0.1:5173` 不再响应。

## 3. 不要做的事

- **不要** 用 `bash app\run.sh` / `bash app\run_tests.sh`：里面写死了 `.venv/bin/python`（Linux 路径），
  Windows 上必然失败。要跑测试就用第 1 步的脚本（它会用 `.venv\Scripts\python.exe`）。
- **不要** 提交或外发 `app\config.local.json`（Canvas 令牌，等同密码）与 `app\data\`（真实数据）。
- **不要** 为了让测试变绿去改 `app\` 下的代码（`✓ ✗ ⚠` 在 GBK 下报错是已知结论之一）。
- **不要** 删 `out\win-verify\`，那是证据。

## 4. 交回什么

新建 `out\win-verify\AGENT-REPORT.md`，只填这几行：

```markdown
- 系统 / PowerShell: <版本>
- 退出码: <0 或 1 或其它>
- 能不能跑起来: <能 / 不能>
- 要不要设 PYTHONUTF8: <要 / 不要>
- 中文会不会变方框: <会 / 不会>
- 阻碍: <一句话；没有就写"无">
- 额外发现: <可选，一两句>
```

把这两份一起交回给人类（他再转给我）：

```
out\win-verify\AGENT-REPORT.md
out\win-verify\verify-windows.log
```

## 5. 其它意外

| 现象 | 处理 |
|---|---|
| `verify_windows.ps1` 报语法错、参数错、"无法将…识别为 cmdlet" | **不要改脚本**。把红色原文 + `$PSVersionTable` 的输出一起交回 |
| 提示「这看起来不是 Windows」 | 确认用的是 Windows 的 PowerShell，不是 WSL / Git Bash |
| `pip install` 失败（网络或代理） | 抄下 pip 报错原文，停在这里交回，不要反复重试 |
| `out\win-verify\verify-windows.log` 没生成 | 说明测量脚本没跑起来；把终端里最后 20 行原文交回 |

# 奶我助（软件部分）

对着桌上的奶龙玩偶问一句「我还有哪些作业没交」「我明天有什么日程」，
它从 **Canvas**（`oc.sjtu.edu.cn`）和**交我办日历**里查出答案。

这个仓库放**软件（两端都在这里）**：

| 目录 | 是什么 |
|---|---|
| `app/` | 电脑端：本地网页版 + 全部接口。Canvas 取数、交我办日历导入、查询、手动录入、条目改删隐藏、到点提醒、演示模式 |
| `terminal/` | 语音终端：唤醒、识别、大模型抽参数、播报。听与说在这里，数据与判断在 `app/` |
| `tools/browser/` | 交我办同步脚本（一份代码，三种装法）+ 自带的 Chrome 扩展 |
| `tools/verify_windows.py` `.ps1` | Windows 兼容性体检 |
| `tools/canvas_check.py` | Canvas 令牌自检 |
| `tools/sync_smoke.py` | 两台机器同步的验收脚本 |
| `tools/smoke_link.py` | 终端 ↔ 电脑端的链路冒烟（不用麦克风与密钥） |
| `SYNC.md` | 两台机器（电脑 + 树莓派）怎么配、怎么同步 |
| `API.md` | 接口清单：写日程、查日程与 ddl、条目改删隐藏、到点提醒、条目结构 |
| `terminal/INTEGRATION.md` | 终端与电脑端的对接契约（终端同学写的，以 `API.md` 为准） |

课程文档（项目建议书、答辩 PPT、小组分工、学习路线）不在这里。

> **目录结构别动**：`app/`、`tools/`、`terminal/` 都留在仓库根目录
> （`tools/smoke_link.py` 按 `../terminal` 找终端代码）。
> `app/server.py` 会去找 `../tools/browser`，把 `app/` 单独拎出去会让配置页上
> 「安装自动同步脚本」那个按钮 404。同理，`tools/verify_windows.ps1` 也假设
> 自己待在 `<仓库根>/tools/` 下。

---

## 跑起来

### Windows

```powershell
# 找 Python → 建 .venv → 装依赖 → 跑体检，报告落在 out\win-verify\
powershell -ExecutionPolicy Bypass -File tools\verify_windows.ps1

# 只想起网页版：
.venv\Scripts\python.exe app\server.py
```

没装 Python 也不用手动装：脚本会先找 `py -3` / `python`，都没有就试 `winget`。

Windows 上有两处要注意（体检脚本会逐条量出来）：

1. **解释器路径**：Windows 上在 `.venv\Scripts\python.exe`，跟 Linux 的
   `.venv/bin/python` 不一样，所以 `app/run.sh` 与 `app/run_tests.sh` 里写死的路径
   在 Windows 上无效——要么用 Git Bash / WSL 跑，要么直接敲
   `.venv\Scripts\python.exe app\server.py`。
2. **输出编码**：测试打印的 `✓ ✗ ⚠` 不在 GBK 里，早期**重定向或管道输出时**会抛
   `UnicodeEncodeError`（退出码 1，看起来像测试失败）。现在 `app/console.py` 会把
   重定向后的输出自动切成 UTF-8，**不用再设 `PYTHONUTF8=1`**
   （想复现原始问题：设 `NAIWOZHU_NO_UTF8=1`）。

> 这个仓库里没有 `src/`（生成 Word 与配图的脚本），所以体检报告里
> 「生成 Word」和字体那两步会显示成**跳过**，属正常，不用管。

### Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/pip install -r app/requirements.txt

bash app/run_tests.sh     # 六套测试，共 567 项
bash app/run.sh           # 起网页版，默认 http://127.0.0.1:5173
```

浏览器打开 <http://127.0.0.1:5173>。默认只监听本机回环地址；要让手机或另一台机器
访问，用 `--host 0.0.0.0` 并**先设一个访问口令**——没有口令时它拒绝启动，不会裸奔。

---

## 配置数据来源（可选，但配了才有数据）

**① Canvas（作业截止）** — 有公开接口，生成一个访问令牌就能读：

```bash
.venv/bin/python app/cli.py --save-token     # 存到 app/config.local.json（权限 600）
export CANVAS_TOKEN='你的令牌'                # 或者临时用环境变量
```

**② 交我办日历（课表、活动、讲座）** — 它没有对外接口，只能借你已经登录的浏览器
会话去读。三种装法任选，配置页上都有按钮：自带的扩展 / Tampermonkey 之类的管理器 /
什么都不装每次手动贴一遍。

两条都配最好，只配一条也能用。**一条都没配也进得去**：页头右上角有个「演示数据」开关，
点开就用编好的数据演示，不联网。

---

## 两台机器（电脑 + 树莓派）

数据在电脑和树莓派各存一份，连上同一个局域网时互相补齐新的那部分；
任何一台单独都能回答"我还有哪些作业没交"。配置步骤、同步规则、校园网排错
都在这三份里：

- [`SYNC.md`](SYNC.md) —— 怎么配、怎么同步、连不上怎么查
- [`API.md`](API.md) —— 要接语音前端时调什么接口
- `tools/sync_smoke.py` —— 起两个隔离实例跑一遍完整流程

```bash
.venv/bin/python tools/sync_smoke.py      # 本机模拟两台
```

---

## 语音终端（`terminal/`）

终端只负责听与说：Vosk 离线唤醒 → 云端识别 → 大模型把同音错字纠回来、只输出查询参数
→ 请求电脑端接口 → 按模板拼语音播报。数据、判断、写库都在 `app/` 里，终端不碰任何凭据。

`terminal/` 是从组内仓库 [`walkerljy/naiwozhu-terminal`](https://github.com/walkerljy/naiwozhu-terminal)
同步过来的，来源与同步方式见 [`terminal/来源与上游同步.md`](terminal/来源与上游同步.md)。

```bash
cd terminal
python -m pip install -r requirements.txt      # 树莓派还要 apt install libportaudio2 ffmpeg
cp local_secrets.example.py local_secrets.py   # 填 LLM_API_KEY 与云端识别 Key
python download_model.py                       # Vosk 中文小模型，约 40 MB
python assistant.py --check                    # 自检麦克风与唤醒词
```

### 两端怎么接（同一台机器 / 树莓派连电脑）

```bash
# 电脑端：对外提供数据要先设口令，没口令时拒绝启动
bash app/run.sh --host 0.0.0.0 --access-key 那串口令

# 终端：填电脑的地址和同一个口令（本机 127.0.0.1 不需要口令）
cd terminal
BACKEND_URL=http://192.168.1.23:5173 BACKEND_TOKEN=那串口令 python assistant.py
```

先确认链路通了再调麦克风：

```bash
.venv/bin/python tools/smoke_link.py                              # 连本机 127.0.0.1:5173
BACKEND_URL=http://192.168.1.23:5173 NAIWOZHU_ACCESS_KEY=口令 \
    .venv/bin/python tools/smoke_link.py                          # 连电脑端
.venv/bin/python tools/smoke_link.py --read-only                  # 只读，不动数据
```

它验的是契约：`/api/status`、`/api/query`、`/api/items`、`/api/remind`、写操作往返
（加 → 改 → 删 → 恢复）、`cal-*` 只能隐藏、口令错误与连不上时的行为。
**不验**麦克风、唤醒、云端识别、大模型——那几段要硬件和密钥，见 `terminal/README.md`。

---

## 两个坑

- **`app/config.local.json` 里有你的 Canvas 令牌，等同密码**，已经在 `.gitignore` 里
  挡着（`app/.gitignore` 也挡了一层）。不要提交它，也不要把带这个文件的目录发给别人。
- **`app/data/` 是你真实的日历快照和手动记录**，同样不入库。想换地方放，设
  `NAIWOZHU_DATA_DIR` 就行。

更细的说明（三种装法、跨域那一跳为什么这么设计、缓存、演示模式）都在
[`app/README.md`](app/README.md)。

**让 AI agent 替你在 Windows 上跑一遍？** 把 [`WINDOWS.md`](WINDOWS.md) 给它——
那是一份按步就班的执行清单，带着"期望结果 / 失败怎么办 / 交回什么"。

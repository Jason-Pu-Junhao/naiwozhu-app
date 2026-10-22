# 接口清单

给要接这个程序的另一部分用（主要是 ② 的语音前端）。
这里只写**现在真的能用**的，没做的单独列在最后一节。

三种用法，按推荐顺序：

1. **HTTP 接口** —— 跨进程、跨机器都能用，语音前端就用这个；
2. **命令行** —— 手动排查、脚本化验证；
3. **直接 import 模块** —— 只适合在同一台机器上、同一个仓库里跑的东西。

---

## 1. 写入：日程 / 截止 / 交我办

### 1.1 `POST /api/manual` —— 手动加一条

```json
// 截止
{"kind": "deadline", "title": "高数第三章作业",
 "due": "2026-11-01T23:59", "course": "高等数学"}

// 日程
{"kind": "日程", "title": "导师组会",
 "start": "2026-11-02T15:00", "end": "2026-11-02T16:00",
 "location": "东中院1-101", "all_day": false, "note": "带电脑"}
```

| 键 | 必填 | 说明 |
|---|---|---|
| `kind` | ✅ | 只有两个值：`"deadline"` 或 `"日程"`（就是这个中文字符串） |
| `title` | ✅ | 空字符串会被拒 |
| `due` | 截止必填 | 截止时刻 |
| `start` | 日程必填 | 开始时刻；`end` 可省略 |
| `end` / `all_day` / `location` / `note` | | 日程用 |
| `course` | | 截止用；老写法 `note` 也认（`course` 优先） |

- **时间写法**：`2026-11-01T23:59`、`2026-11-01 23:59`、`2026-11-01`、毫秒时间戳都认；
  不带时区的按东八区补。纯日期会被当成当天 00:00。
- **返回** `{"ok": true, "item": {...}}`，条目结构见第 3 节。
- **400** 时是 `{"ok": false, "error": "必须说明是 deadline 还是 日程，收到的是…"}`。
- **id 是服务端分配的**，格式 `manual-<设备号>-<序号>`，不要自己造。
- 加完会顺手触发一次同步（如果配了对端），不用手动点。

### 1.2 `POST /api/import` —— 导入交我办的导出数据

```json
{"payload": {...}, "filename": "sjtu-calendar-20261015.json", "force": false}
{"scan": true, "force": false}      // 从 app/data/inbox/ 取最新的一份
```

- 只有电脑端能做这件事（数据源是浏览器里已登录的会话）。
- 按导出文件里的 `exported_at` 判新旧：**不比已存的更新就跳过**，返回 `stale: true`。
- 返回 `{ok, stale?, message, count, stats?, exported_at, imported_at, filename}`。
  被跳过时 `count` 是**当前**那份的条数。
- 400：不是 JSON 对象 / 没有 `events` 字段 / `events` 不是列表。

### 1.3 `POST /api/import/clear` —— 清空收件箱

返回 `{"ok": true}`。

### 1.4 `POST /api/token` —— 验证并保存 Canvas 令牌

```json
{"token": "…"}
```

先拿这个令牌去 `/api/v1/users/self` 验一遍，通了才写盘。
返回 `{ok, name, login_id, path}`；400 时带 `error`（令牌无效、连不上……）。

### 1.5 `POST /api/demo` —— 演示模式开关

```json
{"on": true}
```

返回 `{ok, demo, count, banner}`。开着的时候查询**不联网**，用的是编的数据，
且演示数据永远不落盘、不进同步包。

### 1.6 `POST /api/sync` —— 另一台机器推过来的数据

协议接口，不是给人手调的，细节见 [`SYNC.md`](SYNC.md) 第 3 节。
要调它得带完整的同步包（`schema`/`device_id`/`generated_at`/`calendar`/`canvas`/`manual`）。

---

## 2. 查询

### 2.1 `POST /api/query` —— 主查询

请求体里的键可以组合，全部可选：

| 键 | 值 | 说明 |
|---|---|---|
| `text` | 一句话 | 走 `query.parse()`。语音、命令行用这个 |
| `range` | `all` / `today` / `tomorrow` / `3d` / `7d` / `custom` | 网页下拉列表用这个 |
| `kind` | `null` / `"deadline"` / `"日程"` | 问哪一类；不给就是两类都列 |
| `start` / `end` | `YYYY-MM-DD` | 只在 `range="custom"` 时用，**含首尾两天** |
| `course_kw` | 关键词 | 按课程名/标题/地点过滤 |
| `refresh` | `true` | 跳过缓存重抓一次 Canvas |

`text` 和 `range` 同时给时：`text` 解析出来的类型/课程会补上没给的那几个，
时间仍以 `range` 为准。

**成功时返回**（实测的真实键）：

```json
{
  "ok": true,
  "items": [ ... ],
  "count": 12,
  "kind": null,
  "parsed": null,
  "range": "today",
  "range_label": "今天",
  "window": "10-22",
  "demo": false,
  "cache_age": 3.2,
  "canvas_source": "live",
  "canvas_stale_seconds": 0,
  "warning": "Canvas 这次没取到数据：… 现在用的是 40 分钟前的缓存。"
}
```

| 字段 | 含义 |
|---|---|
| `items` | 条目列表，见第 3 节；**已经按时间排好** |
| `count` | 条目数（就是 `len(items)`） |
| `window` | 服务端**实际用的**时间区间写法，如 `10-22` 或 `10-22 ~ 10-28`；不限时间时是 `""` |
| `range_label` | 选的那个下拉项的名字，如 `"今天"`；不限时间时是 `"不限时间"` |
| `parsed` | 给了 `text` 才有，是 `parse()` 的结果，见 2.4 |
| `demo` | 演示模式是否开着 |
| `cache_age` | 这次用的 Canvas 数据在内存里放了多久（秒）；没碰 Canvas 时是 `null` |
| `canvas_source` | `live` / `disk_local` / `disk_peer` / `none` |
| `canvas_stale_seconds` | 磁盘缓存有多旧（秒）；不是缓存时是 `0` |
| `warning` | 有异常才出现，是给人看的一句话 |

**`ok` 永远是 `true`**——查询不成立时（比如听不出来）返回空 `items` 加一句 `note`：

```json
{"ok": true, "items": [], "parsed": {…}, "demo": false,
 "note": "没听懂，请换一种说法", "help": "我可以回答这几类问题：…"}
```

注意这一支**没有 `count` 字段**，取用前先判一下。

**400** 的情况：`kind` 不在两个值里、`range` 不认识、自定义区间反着填、自定义区间没填日期。

**筛选规则**（两条不一样，是有意的）：

- **截止**：截止时刻落在区间里，**或者已经过期还没交**——过期的永远列出来。
- **日程**：与区间有交集，且还没结束；已经结束的不再列。

### 2.2 `GET /api/status` —— 状态与元信息

不带参数。常用来做"现在能不能用"的判断：

| 字段 | 说明 |
|---|---|
| `has_token` / `token_source` | 有没有 Canvas 令牌、从哪读到的 |
| `calendar.count` / `exported_at` / `imported_at` | 交我办快照的情况 |
| `configured` | 首次配置是否完成（有快照，或有令牌且填了对端） |
| `manual_count` | 手动条目条数 |
| `demo` / `demo_banner` | 演示模式 |
| `cache_age` / `cache_ttl` | Canvas 内存缓存 |
| `canvas.meta` / `canvas.stale_seconds` | Canvas 磁盘缓存 |
| `device_id` | 这台机器的设备号 |
| `bind` / `access` | 监听地址、要不要口令 |
| `sync` | 对端地址、间隔、上次同步结果 |
| `inbox` | 收件箱绝对路径 |
| `help` | 一句话查询的帮助文案 |

### 2.3 `GET /api/manual` —— 手动条目原始列表

返回 `{"ok": true, "items": [...]}`，**不按时间排序、不做时间窗过滤**。
要"能回答问题的那个列表"用 `/api/query`。

### 2.4 一句话解析（`query.parse()`）

给语音用的时候，可以先自己解析、再只传结构化条件；也可以整句丢给 `/api/query` 的 `text`。

```python
import query
query.parse("高数有什么作业")
# {'is_query': True, 'negated': False, 'days': None,
#  'course_kw': '高数', 'want_kind': 'deadline', 'reason': '识别为查询'}
```

- `is_query` 为 `False` 时**不要去查数据**（否定句、听不出来的话都落在这里）。
- `want_kind`：`'deadline'` / `'日程'` / `None`（两种都列）。
- `days`：`1`=今天、`2`=明天、`3`=后天、`7`=这周、`14`=下周、`31`=本月；
  交给 `query.span_from_days(days)` 折成区间。
- `HELP_TEXT` 是给用户看的说明，可以直接念出来。

---

## 3. 条目长什么样

两种类型共有的字段：

| 字段 | 说明 |
|---|---|
| `kind` | `"deadline"` 或 `"日程"` |
| `id` | 全局唯一。手动条目形如 `manual-<设备号>-<序号>`，交我办是 `cal-<事件id>`，Canvas 是作业 id |
| `title` | 标题 |
| `source` | `"canvas"` / `"交我办"` / `"手动录入"` |
| `sources` | 字符串数组，记录这条是从哪几条路径合并来的（如 `["待办接口","逐课程查询"]`） |
| `remaining` | 人话状态：`"还有 3 天"` / `"已过期 2 小时"` / `"进行中"` / `"已结束"` |
| `overdue` | 布尔。截止=已过期；日程=已结束 |
| `when_local` | 排好版的一行时间，直接显示用 |

**deadline 专有**：

| 字段 | 说明 |
|---|---|
| `due_at` | ISO8601 带时区，排序和判断都用它 |
| `due_local` | `"11-01 23:59"` |
| `course` | 课程名（Canvas 的由课程表补上，手动的自己填） |
| `html_url` | Canvas 上那条作业的地址，手动条目是 `""` |
| `course_id` | 只有 Canvas 的条目有 |

**日程专有**：

| 字段 | 说明 |
|---|---|
| `start_at` / `end_at` | ISO8601；`end_at` 可能是 `null` |
| `start_local` / `end_local` | 排版好的起止 |
| `all_day` | 是否全天 |
| `location` / `description` | 地点 / 备注 |
| `calendar` | 日历名（交我办） |
| `course_like` | 日历名里带"课程/课表"的标记，**只是标记，类型仍然是日程** |
| `calendar_id` | 交我办内部 id |

手动条目还多四个同步用的字段：`updated_at`、`rev`、`device_id`、`deleted`。
正常查询永远不会返回 `deleted: true` 的条目（墓碑只在同步时用）。

`remaining` / `overdue` / `*_local` 都是**每次读取时按当前时间现算**的，
落盘和同步包里没有它们——所以拿到手就是新的，不用自己再算。

---

## 4. 命令行的接口

```bash
.venv/bin/python app/cli.py                          # 查全部
.venv/bin/python app/cli.py 明天有什么要交的           # 一句话
.venv/bin/python app/cli.py --days 3 --course 高数     # 结构化条件
.venv/bin/python app/cli.py --all --no-overdue        # 连没截止时间的也看 / 去掉过期的
.venv/bin/python app/cli.py -v                        # 打印翻页过程
.venv/bin/python app/cli.py --whoami                  # 只验证令牌
.venv/bin/python app/cli.py --save-token              # 把令牌存进配置

# 交我办（不需要令牌）
.venv/bin/python app/cli.py --import-calendar FILE    # 导入一份导出文件
.venv/bin/python app/cli.py --inspect-calendar FILE   # 只看文件里有什么，不写
.venv/bin/python app/cli.py --scan-inbox              # 从收件箱导入最新一份
.venv/bin/python app/cli.py --show-calendar           # 列出已导入的日程
```

命令行这条路**只查 Canvas 的作业**，不合并本机日程与手动条目——
要那个组合用 HTTP 的 `/api/query`。

---

## 5. 直接 import（同一台机器上才建议）

```python
import query
query.parse(text)                  # 一句话 → 条件
query.resolve_range("today")       # 下拉项 → 半开区间 (起, 止)
query.span_from_days(3)            # "未来 3 天" → 半开区间
query.span_label(span)             # 区间 → "10-22 ~ 10-24"
query.RANGES                       # 下拉列表的全部选项（网页照它渲染）

from store import Store
st = Store()
st.all_items(kind=None)            # 日历 + 手动，已按时间排序、已算好派生字段
st.calendar_items()                # 交我办快照
st.canvas_items()                  # Canvas 磁盘缓存
st.manual_items()                  # 手动条目（不含墓碑）
st.add_manual(kind, title, due=…, start=…, end=…, course=…, location=…)
st.delete_manual(item_id)

from ddl_service import DDLService
DDLService(client).pending(days=3, course_kw="高数",
                           include_overdue=True, show_all=False)

from canvas_client import CanvasClient, CanvasError
client = CanvasClient("https://oc.sjtu.edu.cn", token)

import model
model.parse_dt("2026-11-01 23:59")  # 各种时间写法 → datetime

import server
server.in_window(item, span, course_kw)   # 和服务端完全一样的筛选规则
```

---

## 6. 认证与错误

- **本机访问（127.0.0.1）不需要口令**。
- 从别处访问 `/api/*` 要带口令，三种带法任选：`X-Access-Key` 头、`?key=`、`naiwozhu_key` cookie。
  口令在设置页生成，两台机器要填同一个。
- 页面本身、`/static/*`、`/naiwozhu.user.js`、`/naiwozhu-extension.zip` 不需要口令。

| 状态码 | 含义 |
|---|---|
| `200` | 成功 |
| `400` | 请求本身有问题，`error` 里有原因 |
| `401` | 口令不对；`need_key: true` |
| `403` | 那台机器还没设口令，所以它不对外提供数据 |
| `404` | 条目不存在（删除时） |
| `413` | 请求体超过 8 MB（同步包才会这么大） |
| `500` | 没预料到的错。**也是 JSON**，带一句人话，不会回 HTML |

---

## 7. 现在还没有的（免得白找）

- **`GET /api/voice?q=…`**：给硬件准备的紧凑接口，还没做（① 第 1–4 周的任务，
  在那之前用 `/api/query` 的 `text`。
- **改一条**：没有 `PUT/PATCH`。要改就删掉重加。
- **批量删除 / 批量新增**：一次一条。
- **分页**：`/api/query` 一次给全量，网页自己截前 10 条。条目多的时候（几十条）没问题。
- **CORS**：故意没有（见项目里的 D04 决策：加了就等于让浏览器里任何一个网页都能往本机写数据）。
  浏览器里跨源调用会被拦，
  从别的程序调用不受影响。

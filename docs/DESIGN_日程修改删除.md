# 「日程修改 / 删除」功能设计方案（评审修订版）

> 适用仓库：`Jason-Pu-Junhao/naiwozhu-app`（本工作区 `app/`，电脑端）
> 对接仓库：`walkerljy/naiwozhu-terminal`（语音与大模型，终端）
> 对应 `fact.txt` 第四节第 8 条「改日程的写接口做不做」
> 初稿：另一 agent 编写 · 评审修订：2026-11-04

**这份文件是修订版。** 原稿的骨架保留（覆盖层、合并时机、墓碑、同步全序这几处判断是对的），
被否掉的地方逐条改过。改动清单见 §0，逐条理由见附录 A，需要终端侧确认的见附录 B。

---

## 0. 评审修订摘要

| # | 原稿 | 修订后 | 依据 |
|---|---|---|---|
| 1 | 同步 `schema` 从 1 升到 2，老对端忽略未知字段 | **`SCHEMA` 不动，`hides` 作为可选段** | `app/sync.py:28` 是 `SCHEMA = 1`，`validate()` 第 92 行要求**严格相等**，不等就 `raise SyncError`；测试里有一条用例就叫「版本不一致时明确拒绝」 |
| 2 | 读路径 `{**it, **patch}` 合并覆盖 | **`decorate(strip_derived(it) \| patch)`** | `store.py:120` 与 `store.py:335` 返回的是已经 `decorate()` 过的对象，`start_local`/`end_local`/`remaining` 按旧时间算好了；`model.py:38` 定义了这六个派生字段 |
| 3 | API 入参用 `start`/`end`，落库也用同名 | **两层字段名分别写死并映射**（§5.2） | `in_window()` 读的是 `start_at`/`end_at`；`make_schedule()` 的 `note` 参数落库字段叫 `description` |
| 4 | 让 `/api/manual` 改走合并读法 | **新增 `GET /api/items`**，`/api/manual` 保持原义 | `GET /api/manual` 现在只返回手动条目（`server.py:542`），网页的删除按钮就挂在这份列表上（`static/index.html:873`）；只改存储层，网页看不到 `cal-*` 条目 |
| 5 | 孤儿覆盖只记数 | **导入后 `prune` + status 计数** | 每次导入整份替换快照，撤掉的课会变孤儿；只记数的话 `hides.json` 只涨不掉 |
| 6 | `cal-*` 允许改时间（本地覆盖） | **`cal-*` 只允许隐藏 / 恢复**；要改期就「隐藏原条目 + 新增一条手动日程」 | 产品层面：这个系统的价值是答得准，本地改课表会造出一份假课表，一周后自己都分不清哪些是真的 |
| 7 | 撤销列在「可选」 | **撤销提到 P1 必修** | 原稿 §8 自己把「语音误改」列为第一风险，那就必须配退路 |

另外两处小修：P0 工期从 1~2 天改为 **3 天**（含网页与测试）；原稿引用的 `API.md` 在本工作区已随文档合并删除（`c67da59`），引用改为 `PROJECT.md` / `fact.txt` / git 历史。

---

## 1. 语义边界（先定这个，后面才好设计）

### 1.1 三个来源各自能做什么

| 来源 | 条目 id | 本期允许的操作 | 原因 |
|---|---|---|---|
| Canvas 截止 | Canvas 侧 id | 只读（改/删一律 409） | 截止时间以 Canvas 为准，本地改了就等于编数据 |
| 交我办 日程 | `cal-{eventId}`，缺失时 `cal-{title\|start\|end}` | **隐藏 / 恢复** | 交我办没有写接口，快照是浏览器导出的；隐藏只影响本机显示，不会造出假时间 |
| 手动录入 | `manual-{设备}-{序号}` | 改字段 / 删除 / 恢复 | 真写 `app/data/manual.json`，是用户自己记的 |

### 1.2 必须写进文案和播报的一句话

> **「改日程」= 改本机奶我助看到的那一份，交我办网页和手机上仍是原样。**

网页上放在编辑区上方，语音播报时跟在结论后面（由接口返回的 `local_only` 驱动，不写死在话术里）。

### 1.3 为什么不让改交我办的时间

假设允许本地把「周一 08:00 高数」改成 14:00：

- 用户第二天问「明天有什么安排」，系统答「高等数学，下午两点」——**这句话是假的**，真实课表没变；
- 一周后用户已经忘了自己改过哪几条，界面上也没有强标记，错误以「我怎么记错了」的形式出现；
- 这个项目要验证的恰恰是「语音查询比手动查更准更省事」，造出一条假事实会直接打掉验收指标 R1（无错项）。

正确形态是**两条动作**，不当偷偷改一条：

1. 把交我办那条**隐藏**（不显示，可恢复）；
2. **新增一条手动日程**放到下午，来源标「手动录入」，播报时说「这是你手动加的」。

这样原始快照一直是真的，用户改过什么一眼看得出来（来源列不同）。

> 如果将来课程本身真的改了（学校调整），正确做法是**重新导入一份新快照**，让上游说了算；
> 本地覆盖永远不是事实来源。

### 1.4 交我办有没有写接口：没有

它的数据通路是单向的：

```
浏览器扩展（已登录页面） → 导出 JSON → POST /api/import → store.replace_calendar()
```

`replace_calendar()` 是整份替换，写回去没有入口，本期也不承诺。
所以原稿§0 的结论成立，`fact.txt` 第 8 条里「先确认交我办有没有修改入口」这句可以从待确认里删掉。

---

## 2. 现状盘点（行号已在本工作区核对）

| 能力 | 现状 | 位置 |
|---|---|---|
| 手动条目新增 | ✅ 已有 | `server.py:547` → `store.py:345 add_manual()` |
| 手动条目删除（墓碑） | ✅ 已有 | `server.py:563` → `store.py:397 delete_manual()` |
| 手动条目**修改** | ❌ 没有 | 只能删了重加，会换 id、换 `rev` |
| `cal-*` 改 / 删 | ❌ 没有 | `delete_manual()` 只认 `manual-*`，传 `cal-*` 返回 404 |
| 网页「本机条目」列表 | ✅ 只列手动条目 | `server.py:542` `GET /api/manual` → `store.manual_items()` |
| 查询读路径 | 单一入口 | `server.py:720` `pool = list(store.all_items())` → 过滤 → `in_window()` |
| 事实与派生分离 | ✅ | `model.py:38 DERIVED` / `model.py:192 strip_derived()` / `model.py:199 decorate()` |
| 同步包与版本 | ✅ 严格版本 | `sync.py:28 SCHEMA = 1`；`sync.py:88` `validate()` 版本不等即抛错 |
| 同步记录全序 | ✅ | `model.py:232 rec_key()`（rev → updated_at → device） |
| 鉴权 | ✅ | `server.py:94 _guard_api()`：本机放行，非回环必须带口令，没设口令 403、口令错 401 |
| 终端侧（本工作区看不到） | 待刘嘉远确认 | `llm.py` / `backend.py` / `assistant.py` / `tts.py`，见附录 B |

**结论**：真正要新建的是三样——① 交我办条目的隐藏集合；② 手动条目的「改」；③ 网页上的编辑入口。
删除已经完成一半（接口与墓碑都在），只是没有统一入口。

---

## 3. 存储层：`app/data/hides.json`

### 3.1 为什么不「删了重加」

1. **丢身份**：`manual-*` 删了重加会拿新 id、新 `rev`，同步时对端会认成两条不同记录；
2. **对 `cal-*` 根本不可行**：导入走 `replace_calendar()` 整份替换，删掉再手工加一条，下次导入一把冲掉；
3. **补一条原稿没提的**：`cal-*` 的 id 在缺 `eventId` 时退化成 `title|start|end`（`calendar_import.py`），
   上游一旦改了时间，id 就变了——隐藏记录变孤儿，那条课会重新出现。这是已知限制，靠 §3.6 的孤儿计数暴露。

### 3.2 文件结构

`app/data/hides.json`（与 `manual.json` 同级，不进 git）：

```json
{
  "schema": 1,
  "items": {
    "cal-a1b2c3": {
      "deleted": true,
      "deleted_at": "2026-11-06T10:20:00+08:00",
      "updated_at": "2026-11-06T10:20:00+08:00",
      "rev": 1,
      "device_id": "dev-7f3a"
    }
  }
}
```

字段与 `manual.json` 的墓碑保持一致（`deleted` / `deleted_at` / `updated_at` / `rev` / `device_id`），
这样同步合并可以整段复用 `rec_key()` 的全序比较，不用另写一套规则。

**本期不设 `patch` 字段。** 以后若真要做「本地改时间」，在每条记录里加 `patch` 即可，
旧版本读到未知键会忽略，仍然兼容。

### 3.3 `store.py` 新增方法

```python
HIDES_FILE = "hides.json"
# __init__ 里照 manual_path 的写法加一行（store.py:68 附近）
# self.hides_path = os.path.join(self.dir, HIDES_FILE)

def _hides_doc(self)                               # 读盘，坏文件当空集
def hidden_ids(self) -> set                        # 只取 id 集合，读路径热点用
def hide_items(self, include_deleted=False)        # 同步用：全量记录（含墓碑）
def set_hidden(self, item_id)                      # 幂等；rev+1、updated_at
def clear_hidden(self, item_id)                    # 恢复：写 deleted=False、rev+1 的记录
def upsert_hides(self, records)                    # 同步合并，逐条按 rec_key 取新
def prune_hides(self, alive_ids, keep_days=30)     # 孤儿清理，返回清掉几条
def update_manual(self, item_id, **fields)         # 手动条目改，详见 §4
def effective_items(self, kind=None)               # 读时合并，详见 §3.4
```

写盘一律 `with _LOCK:`（`store.py:45` 的模块级 `RLock`），走现有 `_write()`（临时文件 + rename）。

**恢复不能物理删记录。** 对端还留着那条「已隐藏」，本地把记录删掉之后，下一次同步会按
「本地没有、对端有」把它重新藏上。所以恢复写一条 `deleted: false`、`rev` 更大的记录，
靠 `rec_key()` 的全序赢过对端——和手动条目的恢复是同一个套路。

### 3.4 读路径：合并必须在过滤之前，且必须在「未装饰」的数据上做

`server.py:720` 现在是：

```python
pool = list(store.all_items())          # ← 这一行换成 store.effective_items()
```

`effective_items()` 的正确写法（注意顺序）：

```python
def effective_items(self, kind=None):
    base = self.all_items(kind=kind)          # 已 decorate()，派生字段按旧值算好了
    hidden = self.hidden_ids()
    out = []
    for it in base:
        if str(it.get("id")) in hidden:
            continue
        out.append(it)
    return out
```

本期覆盖层只有「隐藏」，不碰字段，所以这里不存在派生字段失配的问题。
**但如果以后要加 `patch`，必须写成 `decorate(strip_derived(it) | patch)`**——
先 `decorate` 再盖字段会让 `start_local` 还是旧值，网页和播报各说各的时间。

**合并必须发生在 `in_window()` 之前**：`in_window()` 用 `start_at`/`end_at` 判断是否落在时间窗里，
先过滤再合并，会出现「隐藏了却还在结果里」或者「改到下午却在上午的窗口里搜不到」。
`server.py` 里 `pool` 的构造点就在过滤之前，位置天然正确。

需要改成走 `effective_items()` 的读接口：

| 接口 | 改动 |
|---|---|
| `POST /api/query` | `pool = store.effective_items()` |
| `GET /api/items`（新增） | 全量返回 `store.effective_items()`，网页编辑用 |
| `GET /api/manual` | 不改：仍只列手动条目，语义是「手动录入页」 |
| `GET /api/status` | 加 `hidden_count` / `orphan_hides` |

### 3.5 与「重新导入」的关系

| 场景 | 行为 | 实现 |
|---|---|---|
| 重新导入新快照 | 隐藏记录保留，用户隐藏的仍然隐藏 | `hides.json` 独立文件，`replace_calendar()` 碰不到 |
| 新快照里那条课时间变了（有 `eventId`） | id 不变，仍然隐藏 | 按 id 匹配 |
| 新快照里那条课时间变了（无 `eventId`，id 由时间拼） | id 变了，隐藏失效、条目重新出现 | 已知限制；`orphan_hides` 计数暴露，用户可再隐藏一次 |
| 那条课被交我办删了 | 隐藏记录变孤儿，跳过不显示 | `prune_hides()` 清理 |
| 用户恢复 | 条目按快照原样显示 | `clear_hidden()` 写 `deleted: false` 的记录（不物理删，否则同步会复活） |

### 3.6 孤儿清理

快照被替换的地方有两处，都要调一次：`POST /api/import`（`server.py:463`）与 `POST /api/sync`（`server.py:593`）

```python
alive = {str(i.get("id")) for i in store.calendar_snapshot().get("items") or []}
store.prune_hides(alive, keep_days=30)      # 只清 deleted_at 超过 30 天的孤儿
```

保留 30 天是为了让「同步回对面」有机会生效：另一台机器可能还没拉过这条隐藏记录。
不清也行，但必须在 `/api/status` 里给出 `orphan_hides` 数字，否则这个文件会无声膨胀。

---

## 4. `store.py` 改动清单（手动条目「改」）

```python
def update_manual(self, item_id, **fields):
    """改一条手动条目。kind 不可改；字段白名单；rev+1。"""
```

照抄 `delete_manual()` 的写入范式（`store.py:397`）：`with _LOCK:` → 找条目 → 校验 → 改字段 →
`rev += 1` → `updated_at = _now_iso()` → `_write()`。**不要**复用 `add_manual()`（它会分配新 id）。

校验矩阵：

| 字段 | deadline | schedule | 规则 |
|---|---|---|---|
| `title` | ✅ | ✅ | 非空；空串转 `(无标题)` |
| `due` | ✅ | ❌ | 必须能解析成时间 |
| `start` | ❌ | ✅ | 必填；解析不出来 → 422（能清空的话，这条会被 `in_window()` 当成不限时间永远列出） |
| `end` | ❌ | ✅ | 解析不了就 422；`end < start` → 422 |
| `location` / `course` / `note` | ✅ | ✅ | 字符串，长度上限 200 |
| `all_day` | ❌ | ✅ | 布尔 |
| `kind` | — | — | **不可改**，请求里带了就 400 |
| 其他 | — | — | 不在白名单 → 400 |

删除的记录（`deleted: true`）不允许 `update_manual`，返回 404 并提示先恢复。
改完在 **API 层**调 `_wake.set()`（`server.py:77` 的 `threading.Event`）让网页与终端的轮询立刻看到变化——
`api_manual_add` 现在就是这么做的（`server.py:558`）。`_wake` 属于 server，不该让 store 去碰。

---

## 5. API 设计

新增 4 个接口，**不动现有接口**（`DELETE /api/manual/<id>` 保留为兼容别名）。

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/items` | 全量可编辑条目（已合并隐藏），网页编辑用 |
| `PATCH` | `/api/item/<id>` | 改一条（只允许 `manual-*`） |
| `DELETE` | `/api/item/<id>` | 删一条（`manual-*` 墓碑 / `cal-*` 隐藏） |
| `POST` | `/api/item/<id>/restore` | 撤销删除 / 撤销隐藏 |

### 5.1 `GET /api/items`

```
GET /api/items?kind=schedule
→ {"ok": true, "items": [ ... ], "count": 12, "hidden_count": 3}

GET /api/items?hidden=1
→ {"ok": true, "items": [ ... ], "count": 1, "hidden_count": 1}   # 只给被隐藏的，恢复用
```

`items` 里每条带 `editable`（`manual-*` 为 `true`，`cal-*` 与 canvas 为 `false`）和
`hideable`（`cal-*` 为 `true`），网页据此决定按钮显示，不用在前端猜 id 前缀。

### 5.2 字段映射（API 层 ↔ 存储层，写死，不要混用）

| API 入参 | 存储字段 | 适用类型 |
|---|---|---|
| `title` | `title` | 两者 |
| `start` | `start_at` | schedule |
| `end` | `end_at` | schedule |
| `due` | `due_at` | deadline |
| `location` | `location` | schedule |
| `course` | `course` | 两者 |
| `note` | `description` | 两者 |
| `all_day` | `all_day` | schedule |

API 层接受「短名」（和 `POST /api/manual` 的既有习惯一致），落库前统一转成存储名。
`GET /api/items` 返回的是存储层原样字段（`start_at`/`due_at`/`description`），网页按存储名渲染——
和现在 `/api/manual` 的返回习惯一致，不引入第二套命名。

### 5.3 `PATCH /api/item/<id>`

```json
{"start": "2026-11-07T14:00", "end": "2026-11-07T15:40"}
```

成功：

```json
{"ok": true, "item": { ...存储层完整条目... }, "effective": {"local_only": true}}
```

错误码：

| 码 | 场景 |
|---|---|
| 400 | 字段不在白名单 / `kind` 被改 / 值解析失败 |
| 404 | 条目不存在，或已被删除（提示用 restore） |
| 409 | 只读来源：canvas 条目；**以及 `cal-*` 的改字段请求**，返回 `{"code":"readonly_source","hint":"改期请先隐藏原条目，再新增一条手动日程"}` |
| 422 | `start >= end` |
| 401 / 403 | 现有 `_guard_api()` 行为 |

### 5.4 `DELETE /api/item/<id>`

| 来源 | 行为 |
|---|---|
| `manual-*` | 墓碑（复用 `delete_manual`） |
| `cal-*` | 写 `hides.json`（**不改交我办原数据**） |
| canvas | 409，提示「截止时间以 Canvas 为准」 |

### 5.5 `POST /api/item/<id>/restore`

| 来源 | 行为 |
|---|---|
| `manual-*` | `deleted=false`，`rev+1` |
| `cal-*` | 把 `hides.json` 里那条改成 `deleted: false`、`rev+1`（不物理删，否则同步会把它重新藏上） |
| 没被删过 | 200 + `already: true`，幂等 |

### 5.6 鉴权与 CORS

四个接口都自动被 `_guard_api()` 拦住（它拦所有 `/api/*`），语义不变：本机放行、非回环必须带口令。
**CORS 仍然不加**——新增写接口不构成放松的理由。

---

## 6. 同步扩展（`SCHEMA` 保持 1）

### 6.1 包结构

```json
{
  "schema": 1,
  "device_id": "...",
  "generated_at": "...",
  "calendar": {...},
  "canvas": {...},
  "manual": [...],
  "hides": [ {...}, {...} ]
}
```

`hides` 是**可选段**：老版本的 `validate()` 只校验 `manual` 是列表、`calendar`/`canvas` 是对象，
多余的键它不看，所以老程序收到新包照常工作（覆盖层不同步而已，其余数据照旧）。

### 6.2 `validate()` 的改动

```python
if not isinstance(bundle.get("hides") or [], list):
    raise SyncError("同步包的 hides 字段不是列表")
```

**不要**把 `hides` 设成必填，也不要在 `bundle_summary()` 之外的地方假定它存在。

### 6.3 合并规则

逐条按 `model.rec_key()`（`rev` → `updated_at` → `device_id`）取全序最大，写 `upsert_hides()`；
墓碑参与比较（和 `manual` 完全一致），因此可交换、幂等、一次往返收敛。

### 6.4 兼容矩阵

| 本机 → 对端 | 结果 |
|---|---|
| 新 → 新 | 覆盖层互相同步 |
| 新 → 旧 | 旧版忽略 `hides` 键，其余数据正常合并；覆盖层不同步 |
| 旧 → 新 | `bundle.get("hides")` 为空，按空集处理，不报错 |
| 任意 → 版本号被改成 2 的包 | **拒收**（`validate()` 严格相等），这就是不能升版本号的原因 |

### 6.5 新增测试用例（`app/test/test_sync.py`、`test_store.py`、`test_server.py`）

1. 隐藏一条 `cal-*` → 查询结果里没有它；再同步一轮，对端也没有；
2. 空 `hides` 段与缺失 `hides` 段都等价于空集；
3. 隐藏记录 `rev` 落后于对端时，合并取大的那条（幂等：连跑两次结果不变）；
4. 恢复（`clear_hidden`）后，对端也恢复（墓碑不残留，靠 `rev+1` 的删除记录不存在）；
5. 导入新快照后隐藏仍然生效；
6. `prune_hides` 只清真孤儿、不碰还活着的条目；
7. 现有 402 项必须全绿（本机实测五套全过）。

---

## 7. 终端侧（P1，需刘嘉远配合）

### 7.1 改动清单

| # | 文件 | 改动 | 工作量 |
|---|---|---|---|
| 1 | `backend.py` | 新增 `item_delete()` / `item_restore()` / `item_patch()`；处理 409 / 404 / 422 三种错误并给出不同话术。`manual_delete()` 已有，可复用其错误处理范式 | 小 |
| 2 | `llm.py` | 输出 schema 扩展：现在只有 `{intent, kind, range, start_date, end_date, course}`，需要加 `action`（delete / restore / reschedule）、`target`（哪条：日期 / 序号 / 课程）、`change`（改成什么）。每个新字段都要在白名单降级 | 中 |
| 3 | `assistant.py` | **单轮 → 多轮状态机**（最大工作量）：解析 → 查候选 → 0 条说没有 / 1 条复述确认 / 多条反问 → 收到「确认」才写 → 超时或否定则取消 | **大** |
| 4 | `tts.py` | 新增模板：`confirm.delete`、`ok.delete`、`ok.hide`、`ok.restore`、`ambiguous`、`readonly`、`cancelled`、`fail.readonly`；现有 `write.wip` 先留着当过渡话术 | 小 |
| 5 | 素材 | 重跑 `gen_voice.py` 生成新模板的 wav（规则「文件名=文字」）；`_missing.txt` 会自愈，但仍需主动跑一次 | 小 |
| 6 | `INTEGRATION.md` | 补 §5 四个接口的契约 | 小 |

### 7.2 「改期」请求的两步话术（重要）

用户说「把明天第一节改到下午」，终端**不要**去 `PATCH` 交我办那条（会拿到 409）。正确流程：

1. **一次确认**：「明天的第一节是高数，08:00 东上院 415。要我把它隐藏、再加一条下午两点的手动日程吗？」
2. 用户确认后两次调用（顺序固定）：
   - `POST /api/manual` 新增下午的条目，`note` 里写 `改期自 cal-xxxx`；
   - `DELETE /api/item/cal-xxxx` 隐藏原条目；
3. 播报：「改好了，只在本机生效；交我办里还是上午八点。」
4. **先加后隐藏**：新增失败就不要隐藏，保证不会出现「两条都没了」。

撤销时按 `note` 里的 `改期自 cal-xxxx` 找回原条目 → `restore` 它 → 删掉那条手动日程。

### 7.3 多轮状态机的最小设计

```
空闲 --(intent=delete/restore/reschedule)--> 待确认(pending)
待确认 --(确认)--> 执行 --> 播报 --> 空闲
待确认 --(否定/新指令/超时 15 秒)--> 取消 --> 空闲
候选 0 条 --> 「没找到符合条件的条目」--> 空闲
候选 ≥2 条 --> 逐条复述序号 --> 待选择 --> 待确认
```

`pending` 只存「意图 + 候选 id 列表 + 到期时间」，不存音频、不存中间状态，超时清空即可。

### 7.4 需要当面确认的 3 件事

1. **LLM 能不能稳定抽出目标**：`"把明天第一节高数改到下午两点"` 要输出
   `{action:"reschedule", target:{date:"明天", ordinal:1, course:"高数"}, change:{start:"14:00"}}`。
   建议先拿 20 句真实说法跑一轮，再定 schema。
2. **能不能接受多一次确认**：会多一问一答（「对吗？」→「对」）。若不接受，就必须把撤销做扎实。
3. **播报带不带「只在本机生效」**：建议带，且由接口返回的 `effective.local_only` 驱动。

---

## 8. 分阶段

### P0 —— 电脑端独立完成，终端零改动（约 3 天）

- `hides.json` + `store` 新方法（§3.3、§4）
- 四个接口（§5）
- 读路径改 `effective_items()`（§3.4）
- 同步加可选 `hides` 段（§6）
- 网页：`GET /api/items` 列表 + 改 / 删 / 隐藏 / 恢复按钮
- 测试：现有 402 项全绿 + §6.5 新增用例

> P0 完成后终端行为完全不变（仍回 `write.wip`），可以随时合入。

### P1 —— 需刘嘉远配合（约 4~5 天）

- `backend.py` 三个方法 → `llm.py` schema → `assistant.py` 状态机 → `tts.py` 模板与素材
- 联调顺序：先文字链路（`--say`）跑通，再上语音
- 撤销与「隐藏 + 新增」组合动作必须在这一阶段完成

### P2 —— 可选

- 批量操作、「标记完成」（复用隐藏集合做本地标记）、写回交我办（目前无入口，不做）

---

## 9. 风险清单

| 风险 | 影响 | 对策 |
|---|---|---|
| 语音误删 / 误隐藏 | 一句话改坏真实数据 | 强制确认 + 撤销；不做「说完就写」 |
| 期望错位 | 用户以为改了交我办 | 播报带「只在本机生效」；`effective.local_only` 驱动话术 |
| 隐藏掩盖真事 | 用户隐藏了课，自己也忘了 | `GET /api/items` 显示「已隐藏 N 条」可展开恢复；播报查不到时提示「有 1 条被你隐藏了」 |
| `cal-*` id 漂移 | 上游改时间且无 `eventId`，隐藏失效 | 已知限制；`orphan_hides` 计数，必要时改按标题匹配 |
| 派生字段失配 | 网页说下午、播报说上午 | 本期不写 `patch`；将来加时用 `decorate(strip_derived(x) \| patch)` |
| 同步不幂等 | 两端来回覆盖 | 复用 `rec_key()` 全序 + 墓碑 |
| 旧测试回归 | 402 项里有写死的行为 | 先跑基线，每个新接口补测试，不删旧用例 |

---

## 10. 一页速览

```
GET    /api/items                → 合并隐藏后的全部条目（网页编辑入口）
PATCH  /api/item/<id>            → 仅 manual-*；cal-*/canvas → 409
DELETE /api/item/<id>            → manual 墓碑 / cal 隐藏
POST   /api/item/<id>/restore    → 撤销删除或隐藏

读路径：effective_items() 先合并隐藏，再进 in_window() 过滤
同步：  包内加可选 hides 段，SCHEMA 仍为 1，逐条按 rec_key 取新
改期：  隐藏原条目 + 新增手动日程（两步，先加后隐），播报带「只在本机生效」
终端：  llm 抽 target+change → assistant 多轮确认 → backend 调接口 → tts 播报
```

---

## 11. 实现状态（P0 已完成）

| 项 | 状态 |
|---|---|
| `app/data/hides.json` + store 六个方法 | ✅ 已完成（`store.py`：`set_hidden` / `clear_hidden` / `hide_items` / `upsert_hides` / `prune_hides` / `orphan_hides` / `effective_items` / `get_item` / `update_manual` / `restore_manual`） |
| 四个接口 | ✅ `GET /api/items`（含 `?hidden=1`）、`PATCH`/`DELETE /api/item/<id>`、`POST /api/item/<id>/restore` |
| 读路径 | ✅ `server.py` 的 `pool` 改成 `store.effective_items()`，合并发生在 `in_window()` 之前 |
| 同步可选段 | ✅ `hides` 段 + `validate` 类型校验；`SCHEMA` 仍是 1 |
| 网页 | ✅ 手动条目「编辑 / 删除」、交我办条目「隐藏」、note 行「已隐藏 N 条（点开恢复）」 |
| 测试 | ✅ 五套共 **477 项**全绿（原 403 项 + 新增 74 项；其中 26 项标 ★） |
| 终端侧 | ⬜ 未做，见 `docs/交接_日程修改删除.md` |

发布：代码与接口清单已推到 `github.com/Jason-Pu-Junhao/naiwozhu-app`（提交「同步主仓库：条目改删隐藏与隐藏同步」）。

---

## 附录 A：修订逐条理由

### A.1 同步版本（§6）

原稿写「老版本收到新包会忽略未知字段（JSON 天然兼容）」。JSON 层面成立，但 `validate()` 在解析之后
立刻做严格版本检查：

```python
# app/sync.py:28
SCHEMA = 1
# app/sync.py:92
if bundle.get("schema") != SCHEMA:
    raise SyncError(f"同步包的版本认不出来（{bundle.get('schema')!r}），"
                    f"本机支持的是 {SCHEMA}。两边的程序版本可能不一样。")
```

`app/test/test_sync.py` 里有一条用例专门守这个行为（「版本不一致时明确拒绝」）。
所以把版本号升到 2，升级前的那一刻整包会被拒收——把「一次往返收敛」的设计直接打死。
保持 1 + 可选段，两个方向都兼容。

### A.2 合并时机与派生字段（§3.4）

`store.calendar_items()` / `store.manual_items()` 返回的都是 `decorate()` 之后的对象：

```python
# store.py:120
def calendar_items(self):
    return [decorate(i) for i in (self.calendar_snapshot().get("items") or [])]
```

`model.py:38` 的派生字段是 `remaining / overdue / when_local / due_local / start_local / end_local`，
全都按**当前存着的时间**算好。原稿伪代码 `it = {**it, **(o.get("patch") or {})}` 只盖了 `start_at`，
`start_local` 还是旧值，于是同一条记录里出现两个时间。要么先 `strip_derived()` 再合并再 `decorate()`，
要么本期就不做字段覆盖（推荐，见 §1.3）。

### A.3 两层字段名（§5.2）

`POST /api/manual` 的入参是 `kind/title/due/start/end/all_day/location/course/note`，
落库存的是 `title/due_at/start_at/end_at/all_day/location/course/description`。
`in_window()` 读 `start_at`/`end_at`。原稿 §4.1 的请求示例与 §2.2 的存储示例混用了两套名字，
照抄实现会写进一个查询路径读不到的 `start` 字段——查询层看不见，等于没改。

### A.4 `/api/manual` 的语义（§5.1）

`GET /api/manual` 现在返回 `store.manual_items()`，网页的删除按钮就渲染在这份列表上
（`static/index.html:873`）。要让网页能隐藏 `cal-*`，需要一份「全量 + 合并隐藏」的列表，
新增 `/api/items` 比改写 `/api/manual` 干净：后者继续只服务「手动录入」这一块，语义不漂。

### A.5 孤儿（§3.6）

`replace_calendar()` 整份替换快照。撤掉的课、上学期的课都会在 `hides.json` 里留下没有对应条目的记录。
只记数不清理，文件会一直涨；清理要留 30 天窗口，等同步把记录带到另一台机器。

### A.6 `cal-*` 不许改时间（§1.3）

产品判断，理由见 §1.3。技术上代价也不小：字段覆盖会引入「假事实」与派生字段失配两类问题，
而收益只是省一次「隐藏 + 新增」。

### A.7 撤销（§7.3）

原稿把撤销放在 P2 可选，同时把「语音误改」列为第一风险。两个判断互相矛盾：
既然承认会改错，退路就不能是可选。撤销在 P0 就有存储与接口支撑（`restore`），P1 接上语音即可。

### A.8 工期

原稿 P0 1~2 天（存储 + 3 接口 + 读路径 + 同步 + 测试 + 网页编辑 UI）、P1 2~3 天。
按本工作区的实际体量估：P0 的网页编辑 UI 与新增测试至少一天，合计 **3 天**；
P1 的多轮状态机（确认、消歧、超时、素材重录、联调）**4~5 天**更现实。

---

## 附录 B：原稿里本工作区无法核对的断言

本工作区只有电脑端 `app/`，终端仓库的四个文件不在里面。以下断言请刘嘉远本人核对：

| 原稿断言 | 状态 |
|---|---|
| `backend.py:107` 已有 `manual_delete()`，删除客户端不用重写 | 待核 |
| `llm.py` 已能识别 `intent: "adjust"`，提示词里列了「改时间、改标题、加一条、删一条、推迟、标记完成」 | 待核 |
| `assistant.py:94` 的 `handle()` 是纯单轮；`assistant.py:98` 有 `intent == "adjust" → write.wip` 分支 | 待核 |
| `tts.py` 有 `write.wip` 模板可先留着当过渡话术 | 待核 |

原稿引用的 `API.md`（§7 的「没有 PUT/PATCH，要改就删掉重加」与 D04「不加 CORS」决策）在本工作区
已随文档合并删除（提交 `c67da59`）。要找原文用 `git show c67da59^:API.md`；
该决策本身仍然有效，已收进本文件 §5.6。

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""“奶我助” 第一步 · 本地网页版

运行：
    .venv/bin/python app/server.py
然后浏览器打开 http://127.0.0.1:5173

默认只监听本机回环地址；要让手机或另一台机器访问，
用 --host 0.0.0.0 并且先设一个访问口令（没口令就拒绝启动）。
访问令牌只存在你自己的机器上，也不会跟着同步跑出去。

数据有三个来源，网页这边全都汇到一起按时间顺序展示：
    Canvas（oc.sjtu.edu.cn） → 截止（deadline），走访问令牌
    交我办日历               → 日程，走浏览器导出脚本 + 本机导入
    网页 / 语音自行录入      → 两种都行，录入时自己说明是哪一种

两台机器之间还有一路同步（见 sync.py 与仓库根目录的 SYNC.md）。
"""
import hmac
import io
import os
import sys
import threading
import time
import webbrowser
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, send_file, send_from_directory

import config
import demo
import model
from canvas_client import CanvasClient, CanvasError
from ddl_service import DDLService
from store import Store
import query as q
import sync as sync_mod

import console        # noqa: E402

console.setup()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 浏览器端的东西都在 tools/browser/：
#   naiwozhu.user.js             一份脚本，三种装法（扩展 / 管理器 / 控制台）都用它
#   manifest.json background.js  「奶我助」自带的 Chrome 扩展
BROWSER_DIR = os.path.join(ROOT, "tools", "browser")
EXPORT_SCRIPT = os.path.join(BROWSER_DIR, "naiwozhu.user.js")
EXTENSION_ZIP = "naiwozhu-extension.zip"

# Canvas 抓一次要遍历所有课程逐页翻，比较慢；同一个结果在这么多秒内复用，
# 不同问法共用同一次抓取。设 0 等于关掉缓存。
CACHE_TTL = int(os.environ.get("NAIWOZHU_CACHE_TTL", "120"))

app = Flask(__name__, static_folder=os.path.join(HERE, "static"),
            static_url_path="/static")
app.config["JSON_AS_ASCII"] = False
# 同步包不该有这么大。给个上限，免得一个坏请求把内存吃光。
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
try:                       # Flask 2.3+ 的新写法
    app.json.ensure_ascii = False
except Exception:
    pass

store = Store()

# 实际监听的地址。默认只监听本机；要让手机/树莓派访问时才改成 0.0.0.0。
# 状态接口靠它决定页脚怎么说，main() 启动时覆盖。
BIND = {"host": "127.0.0.1", "port": 5173}

# 同步线程在"本机数据变了"时被这个事件叫醒，不用等满一个间隔。
_wake = threading.Event()


# ---------------------------------------------------------------- 访问口令
def _is_loopback_client():
    """这次请求是不是本机发起的。"""
    return (request.remote_addr or "").strip() in ("127.0.0.1", "::1", "localhost", "")


def _client_key():
    return (request.headers.get("X-Access-Key")
            or request.args.get("key")
            or request.cookies.get("naiwozhu_key")
            or "").strip()


@app.before_request
def _guard_api():
    """只拦 /api/*。

    页面本身、静态文件、导出脚本不拦：它们不含个人数据；而且不先让页面
    加载出来，JS 就没机会提示你输入口令。

    本机访问一律放行——单机使用时不该还要输口令。一旦监听地址不是回环，
    就必须有口令，没设的话直接拒绝，宁可不服务也不裸奔。
    """
    if not request.path.startswith("/api/"):
        return None
    if _is_loopback_client():
        return None
    key = (config.load().get("access_key") or "").strip()
    if not key:
        return jsonify({
            "ok": False,
            "error": "这台机器还没设访问口令，所以不对外提供数据。"
                     "先在本机上打开设置页，生成一个「访问口令」。",
        }), 403
    if not hmac.compare_digest(_client_key(), key):
        return jsonify({"ok": False, "need_key": True,
                        "error": "访问口令不对。两台的「访问口令」要填同一个。"}), 401
    return None


# ---------------------------------------------------------------- 缓存
#
# 只缓存 Canvas 那一路（本机数据和演示数据都是现成的，不需要缓存）。
# 存的是**未经筛选**的全量结果，时间窗和课程筛选在内存里做，
# 所以"这周有什么安排"和"高数有什么作业"共用同一次网络抓取。
_cache = {"snap": None}          # (抓取时刻, 条目列表)
_cache_lock = threading.Lock()

# 上一次失败的时刻与说法。失败之后先安静一会儿，别每问一次都重新耗一个
# 连接超时。树莓派连着没有出口的热点时，不这么做的话每次查询都要等 5–25 秒。
# 设 0 等于关掉这个退让。
FAIL_BACKOFF = int(os.environ.get("NAIWOZHU_FAIL_BACKOFF", "60"))
_fail = {"at": None, "note": ""}


def reset_cache():
    """清掉内存缓存。手动刷新、切演示模式都走这里，顺带允许立刻重试。"""
    with _cache_lock:
        _cache["snap"] = None
    _fail["at"] = None
    _fail["note"] = ""


def _mark_failure(note):
    """记下一次失败：缓存放掉，并且接下来 FAIL_BACKOFF 秒内不再打网络。"""
    with _cache_lock:
        _cache["snap"] = None
    _fail["at"] = time.monotonic()
    _fail["note"] = note


def _failing_recently():
    return (_fail["at"] is not None
            and time.monotonic() - _fail["at"] < FAIL_BACKOFF)


def cache_age():
    """缓存有多旧（秒）；从没抓过时返回 None。"""
    snap = _cache["snap"]
    if snap is None:
        return None
    return max(0.0, time.monotonic() - snap[0])


def _canvas_sig(items):
    """条目内容的指纹，用来判断"和上次抓的一样吗"，避免反复写盘。"""
    return [(str(i.get("id")), str(i.get("due_at")), str(i.get("title")))
            for i in items or []]


def _disk_canvas():
    """最近一次成功抓到的 Canvas 数据，返回 (items, source, stale_seconds)。"""
    snap = store.canvas_snapshot()
    items = store.canvas_items()
    if not snap or not items:
        return [], "none", None
    fetched = model.parse_dt(snap.get("fetched_at"))
    stale = None
    if fetched is not None:
        stale = max(0, int((datetime.now(timezone.utc) - fetched).total_seconds()))
    origin = snap.get("origin_device")
    mine = store.device_id()
    source = "disk_local" if (not origin or origin == mine) else "disk_peer"
    return items, source, stale


def _stale_text(seconds):
    if seconds is None:
        return "上次抓到的"
    if seconds < 60:
        return f"{seconds} 秒前的"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前的"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前的"
    return f"{seconds // 86400} 天前的"


def _with_fallback(note):
    """抓不到时，把"用的是多久前的缓存"接在失败说明后面。"""
    items, source, stale = _disk_canvas()
    if items:
        return items, f"{note} 现在用的是{_stale_text(stale)}缓存。", source, stale
    return [], note, "none", None


def canvas_deadlines():
    """取 Canvas 上未交的作业。

    返回 (items, warning) —— 这个签名对外保持不变，别的地方（命令行、
    协作者写的脚本）都按两个值解包。

    想知道"这批数据是不是缓存、有多旧"，用 canvas_deadlines_detail()。
    两层缓存，分工不同：
      · 内存那层（CACHE_TTL 秒）省网络——同一个页面反复查不用反复抓；
      · 磁盘那层管"抓不到的时候"——断网、令牌失效、树莓派刚开机都靠它。
    出错**不写进内存缓存**，否则一次网络抖动会被放大成两分钟不可用。
    """
    items, warning, _source, _stale = canvas_deadlines_detail()
    return items, warning


def canvas_deadlines_detail():
    """同上，但多给两个值：(items, warning, source, stale_seconds)。

    source 是 live / disk_local / disk_peer / none 之一，
    页面靠它说清"这批 ddl 是刚抓的，还是几天前留下的"。

    抓不到时的三层退让：
      1. 内存缓存（CACHE_TTL 秒内直接给）；
      2. 磁盘缓存（上次成功抓到的，标明有多旧、是不是从另一台机器同步来的）；
      3. 刚失败过就先不再试（FAIL_BACKOFF 秒），免得每次查询都重新等超时。
    """
    cfg = config.load()

    if not cfg["token"]:
        reset_cache()
        items, source, stale = _disk_canvas()
        if items:
            return (items,
                    f"本机没有 Canvas 访问令牌，用的是{_stale_text(stale)}缓存。",
                    source, stale)
        return [], "还没配置 Canvas 访问令牌，所以这次只有本地记录。", "none", None

    snap = _cache["snap"]
    if snap is not None and time.monotonic() - snap[0] < CACHE_TTL:
        return list(snap[1]), None, "live", 0

    if _failing_recently():
        # 刚刚才失败过。这时候再去试一次，多半还是同样的结果，
        # 而每一次都要等满连接超时——连着没有出口的热点时尤其明显。
        return _with_fallback(_fail["note"] or "Canvas 上次没取到数据。")

    try:
        client = CanvasClient(cfg["base"], cfg["token"])
        svc = DDLService(client)
        items = svc.pending(show_all=False)
        skipped = list(getattr(svc, "skipped", []) or [])
    except CanvasError as e:
        note = f"Canvas 这次没取到数据：{e}"
        _mark_failure(note)
        return _with_fallback(note)
    except Exception as e:                       # noqa: BLE001
        # 最后一道网：Canvas 那条线上任何没预料到的异常，都不许变成 500 的 HTML 页
        # （前端是按 JSON 解析响应的，回 HTML 只会显示"JSON.parse 失败"）
        note = (f"Canvas 那一步出了没预料到的错（{type(e).__name__}: {e}）。"
                "跑服务的那个终端里有完整 traceback。")
        _mark_failure(note)
        return _with_fallback(note)

    _fail["at"] = None
    _fail["note"] = ""

    # 只拿到一部分课程时，不拿它覆盖上次完整的快照：那会把没取到的课程
    # 从缓存里抹掉，看起来像是"那些作业消失了"。这次回答照样用新数据，
    # 只是把"可能不全"说出来。
    note = None
    if skipped:
        first = skipped[0]
        note = (f"Canvas 这次有 {len(skipped)} 门课程没取到"
                f"（{first.get('course') or first.get('course_id')}："
                f"{str(first.get('error') or '')[:60]}），这份结果可能不全。")

    # 完整抓取成功才落盘。内容没变就不重写，省 SD 卡的写入次数。
    if not skipped:
        try:
            same = (_canvas_sig(store.canvas_snapshot().get("items"))
                    == _canvas_sig(items))
            if not same or not store.canvas_snapshot().get("fetch_ok"):
                store.save_canvas(datetime.now(timezone.utc), items, fetch_ok=True,
                                  origin_device=store.device_id())
        except Exception:                        # noqa: BLE001
            pass                                 # 写缓存失败不影响这次回答

    with _cache_lock:
        _cache["snap"] = (time.monotonic(), items)
    return list(items), note, "live", 0


# ---------------------------------------------------------------- 工具
def in_window(item, span=None, course_kw=None):
    """两类条目共用的筛选：时间区间 + 课程关键词。

    span 是 (起, 止) 半开区间，None 表示不限时间。

    两类的规则不一样，是有意的：

      deadline —— 截止时刻落在区间里，**或者已经过期还没交**。
                  过期的永远列出来：选「今天」如果把上周没交的作业藏起来，
                  「我还有哪些作业没交」这句就白问了。
      日程     —— 和区间**有交集**，且还没结束。已经结束的不再列。

    没有时间的条目不受区间限制（现在两条数据来路都不会产生这种条目，
    这里只是不额外制造意外）。
    """
    if course_kw:
        hay = "".join(str(item.get(k) or "") for k in
                      ("course", "title", "calendar", "location"))
        if course_kw not in hay:
            return False

    now = datetime.now(timezone.utc)

    if item.get("kind") == model.KIND_SCHEDULE:
        s = model.parse_dt(item.get("start_at"))
        e = model.parse_dt(item.get("end_at")) or s
        if e is not None and e < now:
            return False
        if span is None or s is None:
            return True
        return s < span[1] and e >= span[0]

    t = model.parse_dt(item.get("due_at") or item.get("start_at"))
    if t is None or t < now:
        return True
    return span is None or span[0] <= t < span[1]


# ------------------------------------------------------- 未预料异常的最后一道网
@app.errorhandler(500)
def on_500(e):
    """任何没预料到的异常，也回 JSON。

    前端把响应一律按 JSON 解析，回 HTML 的话页面上只会出现一句
    「JSON.parse: unexpected character」，把真正的原因全遮住。
    （真机上报过这个错：Canvas 连不上 → 500 HTML → 前端只显示 JSON 解析失败。）
    """
    orig = getattr(e, "original_exception", None)
    detail = f"{type(orig).__name__}: {orig}" if orig else str(e)
    app.logger.error("未处理的异常：%s", detail)
    return jsonify({"ok": False,
                    "error": f"服务端出了个没预料到的错（{detail}）。"
                             "跑服务的那个终端里有完整 traceback。"}), 500


# ---------------------------------------------------------------- 页面
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def _now_iso():
    return datetime.now(timezone.utc).astimezone(model.CST).isoformat()


def _host_is_loopback():
    return BIND["host"] in ("127.0.0.1", "localhost", "::1")


def _canvas_stale_seconds():
    t = model.parse_dt(store.canvas_snapshot().get("fetched_at"))
    if t is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - t).total_seconds()))


@app.get("/api/status")
def api_status():
    cfg = config.load()
    age = cache_age()
    key_set = bool((cfg.get("access_key") or "").strip())
    return jsonify({
        "has_token": bool(cfg["token"]),
        "base": cfg["base"],
        "token_source": cfg["source"],
        "help": q.HELP_TEXT,
        "kinds": list(model.KINDS),
        "calendar": store.calendar_meta(),
        "inbox": store.inbox,
        "manual_count": len(store.manual_items()),
        "has_export_script": os.path.exists(EXPORT_SCRIPT),
        # 自查扩展：目录路径直接给网页，「加载已解压的扩展程序」时选它
        "extension_dir": BROWSER_DIR,
        "has_extension": os.path.isfile(os.path.join(BROWSER_DIR, "manifest.json")),
        "extension_zip": "/" + EXTENSION_ZIP,
        # 「首次配置」是否完成。三种情况都算：交我办导进来过（哪怕 0 条）、
        # 或者配了令牌且填了对端地址（树莓派就是这么进来的——它没有交我办数据，
        # 第一次同步之后快照才会有，不能让它卡在配置页上）。
        "configured": bool(store.calendar_snapshot())
                      or bool(cfg["token"] and cfg.get("sync_peer")),
        # 演示模式
        "demo": store.demo_on(),
        "demo_banner": demo.BANNER,
        "demo_count": len(demo.items()),
        # Canvas 缓存
        "cache_ttl": CACHE_TTL,
        "cache_age": round(age, 1) if age is not None else None,
        "canvas": {
            "meta": store.canvas_meta(),
            "stale_seconds": _canvas_stale_seconds(),
        },
        # 这台机器是谁、怎么被访问的
        "device_id": store.device_id(),
        "bind": {"host": BIND["host"], "port": BIND["port"],
                 "loopback_only": _host_is_loopback()},
        "access": {"has_key": key_set,
                   "client_loopback": _is_loopback_client()},
        # 两台机器同步
        "sync": {
            "peer": cfg.get("sync_peer") or "",
            "interval": cfg.get("sync_interval"),
            "state": store.sync_state(),
        },
    })



@app.post("/api/demo")
def api_demo():
    """开关演示模式。

    只把布尔标志落盘（app/data/demo.json），演示条目每次现生成，
    所以演示数据永远不会混进真实记录。
    """
    body = request.get_json(silent=True) or {}
    on = bool(body.get("on"))
    store.set_demo(on)
    if not on:
        reset_cache()          # 关掉后下一次查询要拿真实数据
    return jsonify({"ok": True, "demo": on,
                    "count": len(demo.items()) if on else 0,
                    "banner": demo.BANNER if on else ""})


@app.post("/api/token")
def api_token():
    body = request.get_json(silent=True) or {}
    tok = (body.get("token") or "").strip()
    if not tok:
        return jsonify({"ok": False, "error": "令牌为空"}), 400
    try:
        # 先验证再保存，避免存进一个错的
        client = CanvasClient(config.load()["base"], tok)
        me = client.me()
    except CanvasError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    path = config.save_token(tok, config.load()["base"])
    return jsonify({"ok": True, "name": me.get("name"),
                    "login_id": me.get("login_id"), "path": path})


# ---------------------------------------------------------------- 交我办导入
@app.post("/api/import")
def api_import():
    """收一份交我办导出数据。

    两种送法都行：
      · 网页上拖文件进来 → {payload: {...}, filename: "..."}
      · 直接给收件箱路径 → {scan: true}
    """
    body = request.get_json(silent=True) or {}

    if body.get("scan"):
        return jsonify(store.scan_inbox(force=bool(body.get("force"))))

    payload = body.get("payload")
    if payload is None:
        return jsonify({"ok": False,
                        "error": "没有收到数据。拖一个由导出脚本生成的 JSON 文件进来。"}), 400
    res = store.import_payload(payload, filename=body.get("filename"),
                               force=bool(body.get("force")))
    if res.get("ok") and not res.get("stale"):
        _wake.set()             # 有新数据，同步线程不用等满一个间隔
    return jsonify(res), (200 if res.get("ok") else 400)


@app.post("/api/import/clear")
def api_import_clear():
    return jsonify(store.clear_inbox())


@app.get("/api/export-script")
def api_export_script():
    """把浏览器导出脚本原文给网页，方便一键复制。"""
    if not os.path.exists(EXPORT_SCRIPT):
        return jsonify({"ok": False, "error": "找不到导出脚本文件"}), 404
    with open(EXPORT_SCRIPT, encoding="utf-8") as f:
        return jsonify({"ok": True, "code": f.read(),
                        "path": os.path.relpath(EXPORT_SCRIPT, ROOT)})


@app.get("/naiwozhu.user.js")
def userscript():
    """把同一份脚本以 .user.js 提供出去。

    用户脚本管理器（Tampermonkey / Violentmonkey）会拦截 .user.js 的导航
    并弹出安装界面，所以配置页上给个链接就够了，不用让人复制几百行去粘贴。
    给控制台用的那份代码走 /api/export-script，两边是同一个文件。
    """
    if not os.path.exists(EXPORT_SCRIPT):
        return jsonify({"ok": False, "error": "找不到脚本文件"}), 404
    return send_from_directory(os.path.dirname(EXPORT_SCRIPT),
                               os.path.basename(EXPORT_SCRIPT),
                               mimetype="text/javascript")


@app.get("/" + EXTENSION_ZIP)
def extension_zip():
    """把自查扩展打包下载。

    为什么不是 .crx：Chrome 拒绝安装商店外的 .crx（CRX_REQUIRED_PROOF_MISSING），
    所以那个格式发出去也装不上。能装上的是**解压后的目录**，
    所以这里发的是 zip —— 解压出来在 chrome://extensions 里「加载已解压的扩展程序」。

    配置页上其实直接给目录路径更省事（同一台机器的话连下载都不用）。
    这个 zip 是给"想把扩展放到别处 / 发给别人"的场合。
    """
    if not os.path.isdir(BROWSER_DIR):
        return jsonify({"ok": False, "error": "找不到扩展目录"}), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(BROWSER_DIR)):
            path = os.path.join(BROWSER_DIR, name)
            if os.path.isfile(path) and not name.startswith("."):
                z.write(path, os.path.join("naiwozhu-extension", name))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name=EXTENSION_ZIP)


# ---------------------------------------------------------------- 手动录入
@app.get("/api/manual")
def api_manual_list():
    return jsonify({"ok": True, "items": store.manual_items()})


@app.post("/api/manual")
def api_manual_add():
    body = request.get_json(silent=True) or {}
    try:
        item = store.add_manual(
            body.get("kind"), body.get("title"),
            due=body.get("due"), start=body.get("start"), end=body.get("end"),
            all_day=body.get("all_day"), location=body.get("location"),
            course=body.get("course"), note=body.get("note"),
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    _wake.set()
    return jsonify({"ok": True, "item": item})


@app.delete("/api/manual/<item_id>")
def api_manual_delete(item_id):
    res = store.delete_manual(item_id)
    if res.get("ok"):
        _wake.set()
    return jsonify(res), (200 if res.get("ok") else 404)


# ---------------------------------------------------------------- 两台机器同步
def _sync_pull(peer=None, key=None):
    """向对端同步一次，并把结果记进 sync.json。失败抛 SyncError。"""
    cfg = config.load()
    peer = (peer or cfg.get("sync_peer") or "").strip()
    if not peer:
        raise sync_mod.SyncError("还没填对端地址。"
                                 "在设置页的「两台机器同步」里填上再点同步。")
    key = (cfg.get("access_key") or "") if key is None else key
    try:
        res = sync_mod.run_once(store, peer, key.strip())
    except sync_mod.SyncError as e:
        store.set_sync_state(last_attempt=_now_iso(), last_ok=False,
                             last_error=str(e), peer=peer)
        raise
    store.set_sync_state(
        last_attempt=_now_iso(), last_ok=True, last_error="", peer=peer,
        last_result=res.get("changes"), last_written=res.get("written"),
        peer_device=res.get("peer_device"), skew_seconds=res.get("skew_seconds"),
    )
    return res


@app.post("/api/sync")
def api_sync_recv():
    """收下对端推来的一包：并进本机，再把并完的这一包回给它。

    鉴权由 _guard_api 统一管；本机发起的同步也走这里（自环是幂等的）。
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "同步包不合法：不是一个 JSON 对象"}), 400
    try:
        merged, changes = sync_mod.handle_incoming(store, body)
    except sync_mod.SyncError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    skew = sync_mod.clock_skew_seconds(body)
    store.set_sync_state(last_attempt=_now_iso(), last_ok=True, last_error="",
                         last_received=changes, peer_device=body.get("device_id"),
                         skew_seconds=skew)
    return jsonify({"ok": True, "bundle": merged, "changes": changes,
                    "device_id": store.device_id(),
                    "skew_seconds": skew})


@app.post("/api/sync/now")
def api_sync_now():
    """手动触发一次：向配置好的（或请求里给的）对端同步。"""
    body = request.get_json(silent=True) or {}
    peer = (body.get("peer") or "").strip() or None
    try:
        res = _sync_pull(peer=peer)
    except sync_mod.SyncError as e:
        return jsonify({"ok": False, "error": str(e),
                        "state": store.sync_state()}), 400
    return jsonify({"ok": True, "result": res, "state": store.sync_state()})


@app.post("/api/sync/settings")
def api_sync_settings():
    """保存对端地址 / 访问口令 / 间隔。口令可以现场生成一个。"""
    body = request.get_json(silent=True) or {}
    changed = {}

    if body.get("generate_key"):
        changed["access_key"] = config.save_access_key()
    elif "access_key" in body:
        val = (body.get("access_key") or "").strip()
        if val:
            changed["access_key"] = config.save_access_key(val)

    if "peer" in body:
        peer = (body.get("peer") or "").strip()
        if peer and not peer.startswith(("http://", "https://")):
            return jsonify({"ok": False,
                            "error": "对端地址要以 http:// 开头，"
                                     "例如 http://192.168.1.23:5173"}), 400
        changed["peer"] = config.save_peer(peer)

    if "interval" in body:
        try:
            changed["interval"] = config.save_interval(int(body.get("interval")))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "间隔要写整数秒"}), 400

    fresh = config.load()
    if fresh.get("sync_peer"):
        # 刚配上地址就开始自动同步，不用重启服务。
        # 线程每轮重读配置，所以之后改地址/改间隔都跟着变。
        start_sync_thread()
    return jsonify({
        "ok": True,
        "peer": fresh.get("sync_peer") or "",
        "interval": fresh.get("sync_interval"),
        "has_key": bool((fresh.get("access_key") or "").strip()),
        "access_key": changed.get("access_key"),
    })


# ---------------------------------------------------------------- 查询
@app.post("/api/query")
def api_query():
    """取数。网页下拉列表和语音两条路在这里汇合。

    网页（第一步）给的是 range / kind，值全是从下拉列表里选的；
    语音（第二步）给 text，由 query.parse 归一成同一组条件。

    返回里带上服务端**实际用的**区间和它的文字写法，页面照着显示，
    这样"页面上写的筛选条件"和"真正生效的筛选条件"不会各说一套。
    """
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    kind = body.get("kind") or None
    course = (body.get("course_kw") or "").strip() or None
    range_id = str(body.get("range") or "").strip() or None
    force_refresh = bool(body.get("refresh"))

    if kind not in (None,) + tuple(model.KINDS):
        return jsonify({"ok": False, "error": f"不认识的问题类型：{kind}"}), 400

    parsed = None
    if text:
        parsed = q.parse(text)
        if not parsed["is_query"]:
            return jsonify({"ok": True, "items": [], "parsed": parsed,
                            "demo": store.demo_on(),
                            "note": parsed["reason"], "help": q.HELP_TEXT})
        kind = kind or parsed["want_kind"]
        course = course or parsed["course_kw"]

    # 时间区间：下拉列表给的是"哪几天"，一句话给的是"从现在起多少天"
    try:
        if range_id:
            span = q.resolve_range(range_id, body.get("start"), body.get("end"))
            label = q.range_label(range_id)
        elif parsed and parsed["days"] is not None:
            span = q.span_from_days(parsed["days"])
            label = f"未来 {parsed['days']} 天"
        else:
            span, label = None, "不限时间"
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    if force_refresh:
        reset_cache()

    demo_on = store.demo_on()
    warning = None

    # 1) 本机存的：交我办日程 + 手动录入；演示模式下再叠上演示数据
    pool = list(store.all_items())
    if demo_on:
        pool += demo.items()
    cands = [it for it in pool if not kind or it.get("kind") == kind]

    # 2) Canvas：只贡献 deadline。
    #    演示模式下整段跳过——不联网，没网也能演示。
    canvas_source = None
    canvas_stale = None
    used_canvas = not demo_on and kind in (None, model.KIND_DEADLINE)
    if used_canvas:
        got, warning, canvas_source, canvas_stale = canvas_deadlines_detail()
        cands += got

    items = model.sort_items([it for it in cands if in_window(it, span, course)])

    # cache_age 的含义是"这次回答用到的 Canvas 数据有多旧"，
    # 这次没碰 Canvas（演示模式、或者只问日程）时就该是空。
    age = cache_age() if used_canvas else None
    out = {"ok": True, "items": items, "parsed": parsed,
           "count": len(items), "kind": kind,
           "range": range_id, "range_label": label,
           "window": q.span_label(span),
           "demo": demo_on,
           "canvas_source": canvas_source,
           "canvas_stale_seconds": canvas_stale,
           "cache_age": round(age, 1) if age is not None else None}
    if warning:
        out["warning"] = warning
    if demo_on:
        out["demo_banner"] = demo.BANNER
    return jsonify(out)


# ---------------------------------------------------------------- 后台同步
_sync_thread = None


def _sync_loop(peer=None, interval=None, key=None):
    """后台同步：先同步一次，然后等"数据变了"或者等够一个间隔。

    每一轮都重新读配置：用户在设置页把对端地址改了、或者换了口令，
    不用重启也生效。命令行的 --sync-peer / --sync-interval 优先。
    """
    while True:
        cfg = config.load()
        p = (peer or cfg.get("sync_peer") or "").strip()
        k = key if key is not None else (cfg.get("access_key") or "")
        itv = cfg.get("sync_interval") if interval is None else interval
        if p:
            try:
                _sync_pull(peer=p, key=k)
            except sync_mod.SyncError:
                pass                 # 状态已经记进 sync.json，界面上能看到
            except Exception as e:   # noqa: BLE001
                try:
                    store.set_sync_state(last_attempt=_now_iso(), last_ok=False,
                                         last_error=f"{type(e).__name__}: {e}",
                                         peer=p)
                except Exception:    # noqa: BLE001
                    pass
        _wake.wait(timeout=itv if itv and itv > 0 else None)
        _wake.clear()


def start_sync_thread(peer=None, interval=None, key=None):
    """起一个守护线程做自动同步。重复调用只起一个。

    在设置页保存对端地址之后也调它一次，这样不必重启服务就能开始自动同步。
    """
    global _sync_thread
    if _sync_thread is not None:
        return _sync_thread
    _sync_thread = threading.Thread(target=_sync_loop, args=(peer, interval, key),
                                    daemon=True, name="naiwozhu-sync")
    _sync_thread.start()
    return _sync_thread


def main():
    import argparse

    ap = argparse.ArgumentParser(description="奶我助 · 本地网页版（第一步）")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "5173")))
    ap.add_argument("--no-browser", action="store_true",
                    help="不要自动打开浏览器（脚本化验证时用）")
    ap.add_argument("--host", default=os.environ.get("NAIWOZHU_HOST", "127.0.0.1"),
                    help="监听地址。要让手机/树莓派访问就写 0.0.0.0"
                         "（这时必须先设访问口令）")
    ap.add_argument("--sync-peer", default=os.environ.get("NAIWOZHU_SYNC_PEER", ""),
                    help="对端地址，例如 http://192.168.1.23:5173")
    ap.add_argument("--sync-interval", type=int, default=None,
                    help="自动同步间隔（秒）；0 表示只在数据变化时同步")
    ap.add_argument("--access-key", default=os.environ.get("NAIWOZHU_ACCESS_KEY", ""),
                    help="访问口令；不给就用配置里那个，配置里也没有的话"
                         "外网访问会被拒绝")
    args = ap.parse_args()

    BIND["host"], BIND["port"] = args.host, args.port
    loopback = args.host in ("127.0.0.1", "localhost", "::1")

    cfg = config.load()
    if args.access_key:
        config.save_access_key(args.access_key)
        cfg = config.load()

    key = (cfg.get("access_key") or "").strip()
    if not loopback and not key:
        print("=" * 58)
        print(f"  拒绝启动：--host {args.host} 会把服务暴露到局域网，")
        print("  但现在还没有访问口令。先做下面任一件事：")
        print("    · 在本机跑一次（默认只监听 127.0.0.1），")
        print("      在设置页点「生成口令」，再回到这里；")
        print("    · 或者加参数 --access-key <口令>；")
        print("    · 或者设环境变量 NAIWOZHU_ACCESS_KEY。")
        print("=" * 58)
        return 2

    interval = (cfg.get("sync_interval") if args.sync_interval is None
                else args.sync_interval)
    peer = (args.sync_peer or cfg.get("sync_peer") or "").strip()

    shown = "127.0.0.1" if loopback else args.host
    url = f"http://{shown}:{args.port}"
    print("=" * 58)
    print("  奶我助 · 本地网页版（第一步）")
    print(f"  请在浏览器打开： {url}")
    if loopback:
        print("  只监听本机，令牌不出你的电脑。按 Ctrl+C 退出。")
    else:
        print("  已监听局域网地址，访问要带口令（本机访问不需要）。")
        print("  按 Ctrl+C 退出。")
    print(f"  交我办导入收件箱： {store.inbox}")
    print(f"  本机设备号： {store.device_id()}")
    print(f"  访问口令： {'已设置' if key else '未设置（只有本机能访问）'}")
    if peer:
        print(f"  对端地址： {peer}"
              f"（{'每 ' + str(interval) + ' 秒' if interval else '数据变化时'} 同步）")
    else:
        print("  对端地址：未设置（不自动同步）")
    if store.demo_on():
        print("  演示模式：已开启（不联网，数据是编的）")
    print("=" * 58)

    if peer:
        start_sync_thread(args.sync_peer or None, args.sync_interval, key or None)

    if loopback and not args.no_browser and not os.environ.get("NO_BROWSER"):
        try:
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        except Exception:                        # noqa: BLE001
            pass
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

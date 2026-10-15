#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""“奶我助” 第一步 · 本地网页版

运行：
    .venv/bin/python app/server.py
然后浏览器打开 http://127.0.0.1:5173

只监听本机回环地址，不对外网开放；访问令牌只存在你自己的机器上。

数据有三个来源，网页这边全都汇到一起按时间顺序展示：
    Canvas（oc.sjtu.edu.cn） → 截止（deadline），走访问令牌
    交我办日历               → 日程，走浏览器导出脚本 + 本机导入
    网页 / 语音自行录入      → 两种都行，录入时自己说明是哪一种
"""
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
try:                       # Flask 2.3+ 的新写法
    app.json.ensure_ascii = False
except Exception:
    pass

store = Store()


# ---------------------------------------------------------------- 缓存
#
# 只缓存 Canvas 那一路（本机数据和演示数据都是现成的，不需要缓存）。
# 存的是**未经筛选**的全量结果，时间窗和课程筛选在内存里做，
# 所以"这周有什么安排"和"高数有什么作业"共用同一次网络抓取。
_cache = {"snap": None}          # (抓取时刻, 条目列表)
_cache_lock = threading.Lock()


def reset_cache():
    with _cache_lock:
        _cache["snap"] = None


def cache_age():
    """缓存有多旧（秒）；从没抓过时返回 None。"""
    snap = _cache["snap"]
    if snap is None:
        return None
    return max(0.0, time.monotonic() - snap[0])


def canvas_deadlines():
    """取 Canvas 上未交的作业，带 TTL 缓存。

    返回 (items, warning)。命中缓存时不打网络。
    **出错不写进缓存**——否则一次网络抖动会被放大成两分钟不可用。
    """
    cfg = config.load()
    if not cfg["token"]:
        reset_cache()
        return [], "还没配置 Canvas 访问令牌，所以这次只有本地记录。"

    snap = _cache["snap"]
    if snap is not None and time.monotonic() - snap[0] < CACHE_TTL:
        return list(snap[1]), None

    try:
        client = CanvasClient(cfg["base"], cfg["token"])
        items = DDLService(client).pending(show_all=False)
    except CanvasError as e:
        reset_cache()
        return [], f"Canvas 这次没取到数据：{e}"

    with _cache_lock:
        _cache["snap"] = (time.monotonic(), items)
    return list(items), None


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


# ---------------------------------------------------------------- 页面
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def api_status():
    cfg = config.load()
    age = cache_age()
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
        # 「首次配置」是否完成：交我办日历导进来过就算完成。
        # 用"快照文件存不存在"来判断，不额外存一个标志位——
        # 哪怕导进来 0 条也算配置过，不会把人弹回配置页。
        "configured": bool(store.calendar_snapshot()),
        # 演示模式
        "demo": store.demo_on(),
        "demo_banner": demo.BANNER,
        "demo_count": len(demo.items()),
        # Canvas 缓存
        "cache_ttl": CACHE_TTL,
        "cache_age": round(age, 1) if age is not None else None,
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
    return jsonify({"ok": True, "item": item})


@app.delete("/api/manual/<item_id>")
def api_manual_delete(item_id):
    res = store.delete_manual(item_id)
    return jsonify(res), (200 if res.get("ok") else 404)


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
    used_canvas = not demo_on and kind in (None, model.KIND_DEADLINE)
    if used_canvas:
        got, warning = canvas_deadlines()
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
           "cache_age": round(age, 1) if age is not None else None}
    if warning:
        out["warning"] = warning
    if demo_on:
        out["demo_banner"] = demo.BANNER
    return jsonify(out)


def main():
    import argparse

    ap = argparse.ArgumentParser(description="奶我助 · 本地网页版（第一步）")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "5173")))
    ap.add_argument("--no-browser", action="store_true",
                    help="不要自动打开浏览器（脚本化验证时用）")
    args = ap.parse_args()

    port = args.port
    url = f"http://127.0.0.1:{port}"
    print("=" * 58)
    print("  奶我助 · 本地网页版（第一步）")
    print(f"  请在浏览器打开： {url}")
    print("  只监听本机，令牌不出你的电脑。按 Ctrl+C 退出。")
    print(f"  交我办导入收件箱： {store.inbox}")
    if store.demo_on():
        print("  演示模式：已开启（不联网，数据是编的）")
    print("=" * 58)

    if not args.no_browser and not os.environ.get("NO_BROWSER"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()

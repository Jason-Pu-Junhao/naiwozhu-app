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
import os
import sys
import webbrowser
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, send_from_directory

import config
import model
from canvas_client import CanvasClient, CanvasError
from ddl_service import DDLService
from store import Store
import query as q

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXPORT_SCRIPT = os.path.join(ROOT, "tools", "sjtu_calendar_export.js")

app = Flask(__name__, static_folder=os.path.join(HERE, "static"),
            static_url_path="/static")
app.config["JSON_AS_ASCII"] = False
try:                       # Flask 2.3+ 的新写法
    app.json.ensure_ascii = False
except Exception:
    pass

store = Store()


# ---------------------------------------------------------------- 工具
def canvas_client():
    cfg = config.load()
    if not cfg["token"]:
        return None, cfg
    return CanvasClient(cfg["base"], cfg["token"]), cfg


def in_window(item, days=None, course_kw=None):
    """两类条目共用的筛选：时间窗口 + 课程关键词。

    deadline 允许过期（还没交就是还没交）；
    日程已经结束的就不再列出来了。
    """
    if course_kw:
        hay = "".join(str(item.get(k) or "") for k in
                      ("course", "title", "calendar", "location"))
        if course_kw not in hay:
            return False

    now = datetime.now(timezone.utc)
    if item.get("kind") == model.KIND_SCHEDULE:
        end = model.parse_dt(item.get("end_at") or item.get("start_at"))
        if end is not None and end < now:
            return False

    if days is not None:
        t = model.parse_dt(item.get("due_at") or item.get("start_at"))
        if t is not None and t > now + timedelta(days=days):
            return False
    return True


# ---------------------------------------------------------------- 页面
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def api_status():
    cfg = config.load()
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
    })


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
            note=body.get("note"),
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
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    days = body.get("days")
    course = body.get("course_kw")
    want_kind = body.get("kind")

    parsed = None
    if text:
        parsed = q.parse(text)
        if not parsed["is_query"]:
            return jsonify({"ok": True, "items": [], "parsed": parsed,
                            "note": parsed["reason"], "help": q.HELP_TEXT})
        if days is None:
            days = parsed["days"]
        if course is None:
            course = parsed["course_kw"]
        if not want_kind:
            want_kind = parsed["want_kind"]

    items = []
    warning = None

    # 1) 本机存的：交我办日程 + 手动录入
    for it in store.all_items():
        if want_kind and it.get("kind") != want_kind:
            continue
        if in_window(it, days, course):
            items.append(it)

    # 2) Canvas：只贡献 deadline
    if want_kind in (None, model.KIND_DEADLINE):
        client, cfg = canvas_client()
        if client is None:
            warning = "还没配置 Canvas 访问令牌，所以这次只有本地记录。"
        else:
            try:
                svc = DDLService(client)
                for it in svc.pending(show_all=False):
                    if in_window(it, days, course):
                        items.append(it)
            except CanvasError as e:
                warning = f"Canvas 这次没取到数据：{e}"

    items = model.sort_items(items)
    out = {"ok": True, "items": items, "parsed": parsed,
           "count": len(items), "kind": want_kind}
    if warning:
        out["warning"] = warning
    return jsonify(out)


def main():
    port = int(os.environ.get("PORT", "5173"))
    url = f"http://127.0.0.1:{port}"
    print("=" * 58)
    print("  奶我助 · 本地网页版（第一步）")
    print(f"  请在浏览器打开： {url}")
    print("  只监听本机，令牌不出你的电脑。按 Ctrl+C 退出。")
    print(f"  交我办导入收件箱： {store.inbox}")
    print("=" * 58)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()

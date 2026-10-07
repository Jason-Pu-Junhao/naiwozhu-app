#!/usr/bin/env python3
"""“奶我助” 第一步 · 本地网页版

运行：
    .venv/bin/python app/server.py
然后浏览器打开 http://127.0.0.1:5173

只监听本机回环地址，不对外网开放；访问令牌只存在你自己的机器上。
"""
import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, send_from_directory

import config
from canvas_client import CanvasClient, CanvasError
from ddl_service import DDLService
import query as q

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")
app.config["JSON_AS_ASCII"] = False


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


@app.post("/api/query")
def api_query():
    cfg = config.load()
    if not cfg["token"]:
        return jsonify({"ok": False, "error": "还没有配置访问令牌",
                        "need_token": True}), 400

    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    days = body.get("days")
    course = body.get("course_kw")
    show_all = bool(body.get("show_all"))

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

    try:
        client = CanvasClient(cfg["base"], cfg["token"])
        svc = DDLService(client)
        items = svc.pending(days=days, course_kw=course, show_all=show_all)
        return jsonify({"ok": True, "items": items, "parsed": parsed,
                        "count": len(items)})
    except CanvasError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


def main():
    port = int(os.environ.get("PORT", "5173"))
    url = f"http://127.0.0.1:{port}"
    print("=" * 58)
    print("  奶我助 · 本地网页版（第一步）")
    print(f"  请在浏览器打开： {url}")
    print("  只监听本机，令牌不出你的电脑。按 Ctrl+C 退出。")
    print("=" * 58)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()

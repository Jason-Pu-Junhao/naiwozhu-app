"""配置：优先读环境变量，其次读 app/config.local.json（该文件不进 Git）。"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.json")

DEFAULT_BASE = "https://oc.sjtu.edu.cn"


def load():
    """返回 {'base': ..., 'token': ..., 'source': 说明}"""
    cfg = {"base": DEFAULT_BASE, "token": "", "source": ""}

    # 1) 本地配置文件
    if os.path.exists(LOCAL_CFG):
        try:
            with open(LOCAL_CFG, encoding="utf-8") as f:
                d = json.load(f)
            cfg["base"] = (d.get("base_url") or DEFAULT_BASE).rstrip("/")
            if d.get("canvas_token"):
                cfg["token"] = d["canvas_token"].strip()
                cfg["source"] = "app/config.local.json"
        except Exception as e:
            print(f"[警告] 读取 {LOCAL_CFG} 失败：{e}")

    # 2) 环境变量覆盖
    if os.environ.get("CANVAS_TOKEN"):
        cfg["token"] = os.environ["CANVAS_TOKEN"].strip()
        cfg["source"] = "环境变量 CANVAS_TOKEN"
    if os.environ.get("CANVAS_BASE"):
        cfg["base"] = os.environ["CANVAS_BASE"].rstrip("/")

    return cfg


def save_token(token, base=DEFAULT_BASE):
    """把令牌写进 config.local.json（权限设为仅本人可读）。"""
    data = {}
    if os.path.exists(LOCAL_CFG):
        try:
            with open(LOCAL_CFG, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data["base_url"] = base
    data["canvas_token"] = token
    with open(LOCAL_CFG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(LOCAL_CFG, 0o600)
    return LOCAL_CFG

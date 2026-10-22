"""配置：优先读环境变量，其次读 app/config.local.json（该文件不进 Git）。

这个文件里有两类东西：

  · Canvas 访问令牌 —— 只在本机用；
  · 访问口令 sync_peer/access_key —— 两台机器同步时才用到。

**它们都不会被同步出去**：同步包里只有数据（见 sync.build_bundle）。
"""
import json
import os
import secrets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 配置文件的位置。默认就在 app/ 下；设 NAIWOZHU_CONFIG 可以换一个，
# 这样同一台机器上起两个实例做验收时，两边的配置互不干扰。
LOCAL_CFG = (os.environ.get("NAIWOZHU_CONFIG")
             or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "config.local.json"))

DEFAULT_BASE = "https://oc.sjtu.edu.cn"
# 自动同步的间隔（秒）。0 表示只在数据变化时同步，不按时间轮询。
DEFAULT_INTERVAL = 300

ENV_TOKEN = "CANVAS_TOKEN"
ENV_BASE = "CANVAS_BASE"
ENV_ACCESS_KEY = "NAIWOZHU_ACCESS_KEY"
ENV_PEER = "NAIWOZHU_SYNC_PEER"
ENV_INTERVAL = "NAIWOZHU_SYNC_INTERVAL"


def _read_file():
    if not os.path.exists(LOCAL_CFG):
        return {}
    try:
        with open(LOCAL_CFG, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:                       # noqa: BLE001
        print(f"[警告] 读取 {LOCAL_CFG} 失败：{e}")
        return {}


def _write_file(data):
    """写回配置并设成仅本人可读。里面有令牌，权限不能松。"""
    with open(LOCAL_CFG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(LOCAL_CFG, 0o600)
    except OSError:
        pass                                     # Windows 上 chmod 意义不大
    return LOCAL_CFG


def load():
    """返回一份完整配置。缺什么就给默认值，调用方不用判空。"""
    data = _read_file()
    cfg = {
        "base": (data.get("base_url") or DEFAULT_BASE).rstrip("/"),
        "token": "",
        "source": "",
        "access_key": str(data.get("access_key") or "").strip(),
        "sync_peer": str(data.get("sync_peer") or "").strip(),
        "sync_interval": DEFAULT_INTERVAL,
    }

    tok = str(data.get("canvas_token") or "").strip()
    if tok:
        cfg["token"] = tok
        cfg["source"] = "app/config.local.json"

    try:
        cfg["sync_interval"] = int(data.get("sync_interval", DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        cfg["sync_interval"] = DEFAULT_INTERVAL

    # ---- 环境变量覆盖（便于树莓派上直接写在 systemd 里）
    if os.environ.get(ENV_TOKEN):
        cfg["token"] = os.environ[ENV_TOKEN].strip()
        cfg["source"] = f"环境变量 {ENV_TOKEN}"
    if os.environ.get(ENV_BASE):
        cfg["base"] = os.environ[ENV_BASE].rstrip("/")
    if os.environ.get(ENV_ACCESS_KEY):
        cfg["access_key"] = os.environ[ENV_ACCESS_KEY].strip()
    if os.environ.get(ENV_PEER):
        cfg["sync_peer"] = os.environ[ENV_PEER].strip()
    if os.environ.get(ENV_INTERVAL):
        try:
            cfg["sync_interval"] = int(os.environ[ENV_INTERVAL])
        except ValueError:
            pass

    return cfg


def save_token(token, base=DEFAULT_BASE):
    """把令牌写进 config.local.json（权限设为仅本人可读）。"""
    data = _read_file()
    data["base_url"] = base
    data["canvas_token"] = token
    return _write_file(data)


def save_access_key(value=None):
    """存访问口令；不给就生成一个新的，返回最终用的那一个。"""
    data = _read_file()
    key = (value or "").strip() or secrets.token_urlsafe(24)
    data["access_key"] = key
    _write_file(data)
    return key


def ensure_access_key():
    """确保有口令（没有就生成）。"""
    return load()["access_key"] or save_access_key()


def save_peer(peer):
    data = _read_file()
    data["sync_peer"] = (peer or "").strip()
    _write_file(data)
    return data["sync_peer"]


def save_interval(seconds):
    data = _read_file()
    data["sync_interval"] = max(0, int(seconds))
    _write_file(data)
    return data["sync_interval"]

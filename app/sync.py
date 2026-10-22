# -*- coding: utf-8 -*-
"""两台设备之间的数据同步。

设计只有一条主线：**三类数据各自在一个全序上取最大值。**

    交我办快照   整份替换，比 exported_at
    Canvas 缓存  整份替换，比 (抓成功没, fetched_at)
    手动条目     逐条比较，比 (updated_at, rev, device_id)

"取最大值"这件事是可交换、可结合、幂等的，所以：

  · 一次往返就收敛，不需要第二轮协商；
  · 同一个包重发多少次结果都一样；
  · 谁先发起同步都不影响最终状态。

合并逻辑（merge/apply_bundle）是纯函数：不碰网络、不碰文件位置，
可以离线穷举测试。网络那一层只有 http_push 一个函数。

包里只有数据：**Canvas 令牌、访问口令、本机文件路径永远不进去。**
两台机器各用自己的令牌抓 Canvas，同步只用来在对方抓不到时顶上。
"""
from datetime import datetime, timezone

import requests

import model

SCHEMA = 1
TIMEOUT = 15
# 两端时间差超过这么多秒就记一条警告（不去改数据，见 clock_skew_seconds）
SKEW_LIMIT = 300


class SyncError(RuntimeError):
    """同步没成。消息是给人看的，直接显示到界面上。"""


def _dt(value):
    return model.parse_dt(value)


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------- 组包
def build_bundle(store):
    """把本机的数据打成一个可传输的包。

    只读事实层（派生字段已经被 store 剥掉了），所以包里不会有
    "还有 3 天"这种一过夜就错的字符串。
    """
    cal = store.calendar_snapshot()
    cal_out = None
    if cal and cal.get("exported_at"):
        cal_out = {
            "exported_at": cal.get("exported_at"),
            "items": [model.strip_derived(i) for i in (cal.get("items") or [])],
        }

    cv = store.canvas_snapshot()
    cv_out = None
    if cv:
        cv_out = {
            "fetched_at": cv.get("fetched_at"),
            "fetch_ok": bool(cv.get("fetch_ok")),
            "origin_device": cv.get("origin_device"),
            "items": [model.strip_derived(i) for i in (cv.get("items") or [])],
        }

    manual = [model.strip_derived(i) for i in store.manual_items(include_deleted=True)]
    manual.sort(key=lambda r: str(r.get("id")))

    return {
        "schema": SCHEMA,
        "device_id": store.device_id(),
        "generated_at": _iso(datetime.now(timezone.utc).astimezone(model.CST)),
        "calendar": cal_out,
        "canvas": cv_out,
        "manual": manual,
    }


def validate(bundle):
    """检查一个包能不能收。认不出就抛 SyncError，绝不半信半疑地用。"""
    if not isinstance(bundle, dict):
        raise SyncError("同步包不是一个 JSON 对象")
    if bundle.get("schema") != SCHEMA:
        raise SyncError(f"同步包的版本认不出来（{bundle.get('schema')!r}），"
                        f"本机支持的是 {SCHEMA}。两边的程序版本可能不一样。")
    if not isinstance(bundle.get("manual") or [], list):
        raise SyncError("同步包的 manual 字段不是列表")
    for key in ("calendar", "canvas"):
        v = bundle.get(key)
        if v is not None and not isinstance(v, dict):
            raise SyncError(f"同步包的 {key} 字段不是对象")
    return bundle


def bundle_summary(bundle):
    """给人看的一行摘要（状态接口用）。"""
    return {
        "device_id": bundle.get("device_id"),
        "calendar": len((bundle.get("calendar") or {}).get("items") or []),
        "canvas": len((bundle.get("canvas") or {}).get("items") or []),
        "manual": len(bundle.get("manual") or []),
    }


# ---------------------------------------------------------------- 比较键
def _calendar_key(snap):
    if not snap:
        return None
    return _dt(snap.get("exported_at"))


def _canvas_key(snap):
    """抓成功的永远赢抓失败的，其次比时间。

    这个顺序是有意的：网络抖一下不该让一份好数据被一份空数据顶掉。
    """
    if not snap:
        return None
    t = _dt(snap.get("fetched_at"))
    if t is None:
        return None
    return (1 if snap.get("fetch_ok") else 0, t)


def _manual_key(rec):
    return model.rec_key(rec)


# ---------------------------------------------------------------- 合并
def merge(local, remote):
    """把对端的包并进本地的包，返回 (合并结果, 变化说明)。

    比较之前一律先把时间文本解析成 datetime——两边时区写法可能不同
    （+08:00 与 +00:00），拿字符串比大小会排错。
    """
    local = validate(local)
    remote = validate(remote)

    changes = {
        "calendar": "kept",
        "canvas": "kept",
        "manual": {"added": 0, "updated": 0, "deleted": 0},
    }

    cal = local.get("calendar")
    rcal = remote.get("calendar")
    ck, rk = _calendar_key(cal), _calendar_key(rcal)
    if rcal is not None and (ck is None or (rk is not None and rk > ck)):
        cal = rcal
        changes["calendar"] = "replaced"

    cv = local.get("canvas")
    rcv = remote.get("canvas")
    vk, rvk = _canvas_key(cv), _canvas_key(rcv)
    if rcv is not None and (vk is None or (rvk is not None and rvk > vk)):
        cv = rcv
        changes["canvas"] = "replaced"

    lmap = {str(r.get("id")): r for r in (local.get("manual") or [])
            if isinstance(r, dict) and r.get("id") is not None}
    rmap = {str(r.get("id")): r for r in (remote.get("manual") or [])
            if isinstance(r, dict) and r.get("id") is not None}

    merged = dict(lmap)
    for rid, rec in rmap.items():
        cur = lmap.get(rid)
        if cur is None:
            merged[rid] = rec
            changes["manual"]["added"] += 1
            continue
        if _manual_key(rec) > _manual_key(cur):
            merged[rid] = rec
            if bool(rec.get("deleted")) and not bool(cur.get("deleted")):
                changes["manual"]["deleted"] += 1
            elif rec != cur:
                changes["manual"]["updated"] += 1

    return {
        "schema": SCHEMA,
        "device_id": local.get("device_id"),
        "generated_at": _iso(datetime.now(timezone.utc).astimezone(model.CST)),
        "calendar": cal,
        "canvas": cv,
        "manual": sorted(merged.values(), key=lambda r: str(r.get("id"))),
    }, changes


# ---------------------------------------------------------------- 落地
def apply_bundle(store, bundle):
    """把合并结果写进本机。返回实际写了什么。

    写之前再跟**当前**磁盘上的比一次：从"算出合并结果"到"写下去"之间，
    本机可能刚好导入了更新的课表，不能拿旧包把它盖掉。
    """
    bundle = validate(bundle)
    wrote = {"calendar": False, "canvas": False, "manual": 0}

    cal = bundle.get("calendar")
    if cal:
        cur = store.calendar_snapshot()
        ck, nk = _calendar_key(cur), _calendar_key(cal)
        if nk is not None and (ck is None or nk > ck):
            store.replace_calendar(cal.get("exported_at"),
                                  cal.get("items") or [],
                                  source_device=bundle.get("device_id"))
            wrote["calendar"] = True

    cv = bundle.get("canvas")
    if cv:
        cur = store.canvas_snapshot()
        ck, nk = _canvas_key(cur), _canvas_key(cv)
        if nk is not None and (ck is None or nk > ck):
            store.save_canvas(cv.get("fetched_at"), cv.get("items") or [],
                              fetch_ok=bool(cv.get("fetch_ok")),
                              origin_device=cv.get("origin_device"))
            wrote["canvas"] = True

    wrote["manual"] = store.upsert_manual_records(bundle.get("manual") or [])
    return wrote


def handle_incoming(store, bundle):
    """收到对方推过来的一包：并进本机，然后把并完的这一包回给它。

    这样一次往返双方就都拿到并集了：调用方拿到回包后再套同一个 merge。
    """
    remote = validate(bundle)
    local = build_bundle(store)
    merged, changes = merge(local, remote)
    apply_bundle(store, merged)
    return merged, changes


# ---------------------------------------------------------------- 时钟
def clock_skew_seconds(bundle):
    """对端时钟比本机快多少秒（负数表示慢）。

    只用来提示，**不用来改数据**：把时间戳掰到"看起来正常"会让
    两次同步得到不一样的结果。数据的一惯性比"看着顺眼"重要。
    """
    t = _dt(bundle.get("generated_at"))
    if t is None:
        return None
    return int((t - datetime.now(timezone.utc)).total_seconds())


# ---------------------------------------------------------------- 网络
def http_push(peer, key, bundle, timeout=TIMEOUT):
    """把包推给对端，返回它并完之后的包。"""
    url = peer.rstrip("/") + "/api/sync"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-Access-Key"] = key
    try:
        r = requests.post(url, json=bundle, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise SyncError(f"连不上对端 {peer}：{str(e)[:200]}")

    if r.status_code == 401:
        raise SyncError("对端拒绝了这次同步：访问口令不对。"
                        "两台的「访问口令」要填同一个。")
    if r.status_code == 403:
        raise SyncError("对端还没设访问口令，所以它不对外提供数据。"
                        "先去那台机器上生成一个口令。")
    if r.status_code >= 400:
        raise SyncError(f"对端返回 HTTP {r.status_code}：{r.text[:200]}")

    try:
        data = r.json()
    except ValueError:
        raise SyncError(f"对端返回的不是 JSON（HTTP {r.status_code}）："
                        f"{r.text[:200]!r}")
    if not data.get("ok"):
        raise SyncError(f"对端说这次同步没成：{data.get('error')}")
    return validate(data.get("bundle") or {})


def run_once(store, peer, key, timeout=TIMEOUT):
    """向对端同步一次，返回给人看的结果。失败抛 SyncError。"""
    local = build_bundle(store)
    remote = http_push(peer, key, local, timeout=timeout)
    merged, changes = merge(local, remote)
    wrote = apply_bundle(store, merged)
    return {
        "changes": changes,
        "written": wrote,
        "peer_device": remote.get("device_id"),
        "peer_summary": bundle_summary(merged),
        "skew_seconds": clock_skew_seconds(remote),
    }

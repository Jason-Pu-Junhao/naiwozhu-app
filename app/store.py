# -*- coding: utf-8 -*-
"""本地数据仓库：交我办日程的导入快照 + 手动录入的条目 + Canvas 缓存。

存这几个文件（都在 app/data/ 下，不进 Git）：

    calendar.json   最近一次成功导入的交我办快照
    manual.json     网页/语音自行录入的条目（删除留墓碑，见 delete_manual）
    canvas.json     最近一次成功抓到的 Canvas 待交作业（离线时靠它回答）
    demo.json       只存演示模式的开关（演示条目本身每次现生成，见 demo.py）
    device.json     这台设备的标识，手动条目的 id 用它打头，两端同步时靠它区分
    sync.json       上一次同步的结果，只给人看，不参与合并

两条贯穿全文件的约定：

  · **写盘前 strip_derived，读盘后 decorate。** 落盘的只有事实，
    "还剩多少天"这种话一律现算（见 model.DERIVED）。
  · 所有读改写都在同一把锁里。服务进程里既处理网页请求、又有同步线程，
    不锁的话两边同时改 manual.json 会丢更新。

导入按**时间戳**判断新旧：文件里的 exported_at 不比已存的更新时直接跳过，
所以重复拖同一个文件、或者拖了一个旧的备份，都不会把新数据覆盖掉。
"""
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from model import (CST, KIND_DEADLINE, KIND_SCHEDULE, KINDS, SRC_MANUAL,
                   parse_dt, make_deadline, make_schedule, sort_items,
                   strip_derived, decorate, rec_key)
import calendar_import

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.join(HERE, "data")

# 想换个地方存数据（比如放进 U 盘、或者测试用临时目录），设这个环境变量。
# 测试靠它避免碰到你真实的日历和手动记录。
ENV_DATA_DIR = "NAIWOZHU_DATA_DIR"

# 进程内串行化。锁放在模块级、所有实例共用：同一个进程里可能不止一个
# Store 对象（服务一份、命令行一份），它们碰的是同一批文件。
_LOCK = threading.RLock()


def _now_iso():
    return datetime.now(timezone.utc).astimezone(CST).isoformat()


def _as_iso(value):
    """接受 datetime 或字符串，统一成 ISO 文本；None 原样返回。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class Store:
    def __init__(self, data_dir=None):
        # 数据目录三级覆盖：显式传参 > 环境变量 > 默认 app/data/
        self.dir = (data_dir or os.environ.get(ENV_DATA_DIR)
                    or DEFAULT_DATA_DIR)
        self.inbox = os.path.join(self.dir, "inbox")
        self.cal_path = os.path.join(self.dir, "calendar.json")
        self.manual_path = os.path.join(self.dir, "manual.json")
        self.canvas_path = os.path.join(self.dir, "canvas.json")
        self.demo_path = os.path.join(self.dir, "demo.json")
        self.device_path = os.path.join(self.dir, "device.json")
        self.sync_path = os.path.join(self.dir, "sync.json")
        os.makedirs(self.inbox, exist_ok=True)
        self._device_id = None

    # ------------------------------------------------------------ 设备标识
    def device_id(self):
        """这台设备的短标识（8 位十六进制），第一次用到时生成并落盘。

        用途只有一个：让两台机器各自生成的手动条目 id 不可能撞车
        （没有它，两端的第一条都是 manual-1，同步时会互相吞掉），
        顺便给同步的裁决提供最后一道确定性。
        """
        with _LOCK:
            if self._device_id:
                return self._device_id
            doc = self._read(self.device_path) or {}
            did = str(doc.get("id") or "").strip()
            if not did:
                did = secrets.token_hex(4)
                self._write(self.device_path, {"id": did, "created_at": _now_iso()})
            self._device_id = did
            return did

    # ------------------------------------------------------------ 读写
    @staticmethod
    def _read(path):
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    @staticmethod
    def _write(path, obj):
        """先写临时文件再改名，避免写一半被中断留下坏文件。"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ------------------------------------------------------------ 日历导入
    def calendar_snapshot(self):
        """原始快照（事实层）。同步和状态接口读它。"""
        return self._read(self.cal_path) or {}

    def calendar_items(self):
        """给页面/命令行用的条目：派生字段按**现在**重算。"""
        return [decorate(i) for i in (self.calendar_snapshot().get("items") or [])]

    def calendar_meta(self):
        s = self.calendar_snapshot()
        return {
            "imported_at": s.get("imported_at"),
            "exported_at": s.get("exported_at"),
            "filename": s.get("filename"),
            "count": len(s.get("items") or []),
            "stats": s.get("stats") or {},
            "source_device": s.get("source_device"),
        }

    def replace_calendar(self, exported_at, items, source_device=None):
        """整份替换日历快照。同步收下对端那份时走这里，不经过陈旧判断。

        调用方（sync）已经比较过 exported_at，这里不再判新旧——
        再判一次反而会把"对端明确更新"的情况挡掉。
        """
        with _LOCK:
            snap = {
                "exported_at": _as_iso(exported_at),
                "timestamp_origin": (f"同步自另一台设备（{source_device}）"
                                     if source_device else "同步自另一台设备"),
                "imported_at": _now_iso(),
                "filename": None,
                "source_device": source_device,
                "stats": {"synced": True},
                "items": [strip_derived(i) for i in (items or [])],
            }
            self._write(self.cal_path, snap)
            return snap

    def import_payload(self, raw, filename=None, force=False, fallback_mtime=None):
        """收下一份交我办导出数据。

        返回一份说明：是否收下、是否因为旧而被跳过、解析到几条。
        """
        with _LOCK:
            return self._import_locked(raw, filename, force, fallback_mtime)

    def _import_locked(self, raw, filename=None, force=False,
                       fallback_mtime=None):
        """（调用方已持锁）判断新旧并把快照写下去。"""
        payload, exported_at, err = calendar_import.load_payload(raw)
        if err:
            return {"ok": False, "error": err}

        # 文件里没有 exported_at 时，退而用文件名/文件时间
        if exported_at is None:
            fallback_mtime = fallback_mtime or time.time()
            exported_at = datetime.fromtimestamp(fallback_mtime, tz=timezone.utc)
            origin = "文件时间（导出文件里没有时间戳）"
        else:
            origin = "导出文件里的 exported_at"

        old = self.calendar_snapshot()
        old_at = parse_dt(old.get("exported_at"))
        if old_at and exported_at <= old_at and not force:
            return {
                "ok": True, "stale": True,
                "message": f"这份数据（{exported_at.astimezone(CST):%m-%d %H:%M}）"
                           f"不比已导入的新，已跳过。当前用的是 "
                           f"{old_at.astimezone(CST):%m-%d %H:%M} 那一份。",
                "kept_exported_at": old.get("exported_at"),
                # 注意：这里不放顶层 stats——它是**上一次**导入的统计，
                # 放在这里会让调用方误以为本次跳过了什么。
                "count": len(old.get("items") or []),
                "calendar": self.calendar_meta(),
            }

        items, stats = calendar_import.to_schedule_items(payload)
        snap = {
            "exported_at": exported_at.isoformat(),
            "timestamp_origin": origin,
            "imported_at": _now_iso(),
            "filename": filename,
            "source_device": None,
            "stats": stats,
            "range": payload.get("range") or {},
            "keys_seen": payload.get("keys_seen") or [],
            "items": [strip_derived(i) for i in items],
        }
        self._write(self.cal_path, snap)

        msg = f"已导入 {len(items)} 条日程"
        if old_at:
            msg += f"（替换掉 {old_at.astimezone(CST):%m-%d %H:%M} 的旧数据）"
        return {"ok": True, "stale": False, "message": msg,
                "count": len(items), "stats": stats,
                "exported_at": snap["exported_at"],
                "imported_at": snap["imported_at"], "filename": filename}

    # ------------------------------------------------------------ Canvas 缓存
    #
    # 原先 Canvas 的数据一点都不落盘，结果就是进程一重启，
    # "我还有哪些作业没交"要现场翻遍所有课程所有页；断网时更是一条都答不出来。
    # 这里存最近一次**成功**抓取的结果，只用来在抓不到时顶上，
    # 抓取动作本身仍由 server.canvas_deadlines() 负责。
    def canvas_snapshot(self):
        return self._read(self.canvas_path) or {}

    def canvas_items(self):
        return [decorate(i) for i in (self.canvas_snapshot().get("items") or [])]

    def canvas_meta(self):
        s = self.canvas_snapshot()
        return {
            "fetched_at": s.get("fetched_at"),
            "fetch_ok": bool(s.get("fetch_ok")),
            "origin_device": s.get("origin_device"),
            "count": len(s.get("items") or []),
            "saved_at": s.get("saved_at"),
        }

    def save_canvas(self, fetched_at, items, fetch_ok=True, origin_device=None):
        with _LOCK:
            snap = {
                "fetched_at": _as_iso(fetched_at),
                "fetch_ok": bool(fetch_ok),
                "origin_device": origin_device,
                "saved_at": _now_iso(),
                "items": [strip_derived(i) for i in (items or [])],
            }
            self._write(self.canvas_path, snap)
            return snap

    # ------------------------------------------------------------ 收件箱
    def inbox_files(self):
        """列出收件箱里的候选文件，按修改时间从新到旧。"""
        if not os.path.isdir(self.inbox):
            return []
        out = []
        for name in os.listdir(self.inbox):
            if not name.lower().endswith(".json"):
                continue
            path = os.path.join(self.inbox, name)
            try:
                out.append({"name": name, "path": path,
                            "mtime": os.path.getmtime(path),
                            "size": os.path.getsize(path)})
            except OSError:
                continue
        out.sort(key=lambda x: x["mtime"], reverse=True)
        return out

    def scan_inbox(self, force=False):
        """收件箱里最新的那份导进来。"""
        files = self.inbox_files()
        if not files:
            return {"ok": False,
                    "error": f"收件箱是空的：{self.inbox}",
                    "inbox": self.inbox}
        newest = files[0]
        raw = self._read(newest["path"])
        if raw is None:
            return {"ok": False, "error": f"{newest['name']} 不是合法的 JSON",
                    "inbox": self.inbox}
        res = self.import_payload(raw, filename=newest["name"], force=force,
                                  fallback_mtime=newest["mtime"])
        res["inbox"] = self.inbox
        res["scanned"] = newest["name"]
        return res

    def clear_inbox(self):
        for f in self.inbox_files():
            try:
                os.remove(f["path"])
            except OSError:
                pass
        return {"ok": True}

    # ------------------------------------------------------------ 演示模式
    def demo_on(self):
        """演示模式开着吗。

        只有这一个布尔标志落盘，演示条目本身每次现生成（见 demo.py），
        所以演示数据不可能混进 calendar.json / manual.json。
        """
        return bool((self._read(self.demo_path) or {}).get("on"))

    def set_demo(self, on):
        self._write(self.demo_path, {"on": bool(on)})
        return bool(on)

    # ------------------------------------------------------------ 手动录入
    def _manual_doc(self):
        with _LOCK:
            doc = self._read(self.manual_path) or {}
            if not isinstance(doc.get("items"), list):
                doc = {"seq": 0, "items": []}
            return self._migrate_manual_ids(doc)

    def _migrate_manual_ids(self, doc):
        """把旧格式的 manual-3 换成 manual-{设备}-{n}。

        不改这一步，两台机器各自的第一条都叫 manual-1，同步时会互相吞掉。
        本仓库本来没有旧数据，纯属防御；真遇到了也只改一次。
        """
        legacy = [i for i in doc["items"]
                  if isinstance(i, dict)
                  and re.fullmatch(r"manual-\d+", str(i.get("id") or ""))]
        if not legacy:
            return doc
        did = self.device_id()
        for it in legacy:
            doc["seq"] = int(doc.get("seq") or 0) + 1
            it["id"] = f"manual-{did}-{doc['seq']}"
            it.setdefault("device_id", did)
            it.setdefault("rev", 1)
        self._write(self.manual_path, doc)
        return doc

    def manual_items(self, include_deleted=False):
        """手动条目。墓碑默认不出现，同步要看得传 include_deleted=True。"""
        with _LOCK:
            out = []
            for i in self._manual_doc()["items"]:
                if not include_deleted and i.get("deleted"):
                    continue
                out.append(decorate(i))
            return out

    def add_manual(self, kind, title, *, due=None, start=None, end=None,
                   all_day=False, location="", course="", note=""):
        """手动加一条。

        kind 必须显式给出——这是这个项目的约定：
        自行录入的条目，类型由用户确认，不靠系统猜。

        两个类型各自多出来的信息不一样，所以分开收：
          deadline → ``course`` 课程名（``note`` 是老写法，仍然认）
          日程     → ``location`` 地点、``note`` 备注
        """
        kind = (kind or "").strip()
        if kind not in KINDS:
            raise ValueError(f"必须说明是 {KIND_DEADLINE} 还是 {KIND_SCHEDULE}，"
                             f"收到的是“{kind}”")
        title = (title or "").strip()
        if not title:
            raise ValueError("标题不能为空")

        with _LOCK:
            doc = self._manual_doc()
            doc["seq"] = int(doc.get("seq") or 0) + 1
            did = self.device_id()
            item_id = f"manual-{did}-{doc['seq']}"

            if kind == KIND_DEADLINE:
                d = parse_dt(due)
                if d is None:
                    raise ValueError("deadline 需要一个截止时间（due）")
                item = make_deadline(item_id, title, d, SRC_MANUAL,
                                     course=course or note or "",
                                     sources=["手动录入"])
            else:
                s = parse_dt(start)
                if s is None:
                    raise ValueError("日程需要一个开始时间（start）")
                item = make_schedule(item_id, title, s, parse_dt(end), SRC_MANUAL,
                                     all_day=bool(all_day), location=location or "",
                                     description=note or "", sources=["手动录入"])

            now = _now_iso()
            item["created_at"] = now
            item["updated_at"] = now
            item["rev"] = 1
            item["device_id"] = did
            item["deleted"] = False

            fact = strip_derived(item)
            doc["items"].append(fact)
            self._write(self.manual_path, doc)
            return decorate(fact)

    def delete_manual(self, item_id):
        """删一条。留下墓碑、不做物理删除，否则同步时对方那份会把它复活。"""
        with _LOCK:
            doc = self._manual_doc()
            for it in doc["items"]:
                if str(it.get("id")) != str(item_id):
                    continue
                if it.get("deleted"):
                    return {"ok": True, "removed": item_id, "already": True}
                it["deleted"] = True
                it["deleted_at"] = _now_iso()
                it["updated_at"] = it["deleted_at"]
                it["rev"] = int(it.get("rev") or 1) + 1
                self._write(self.manual_path, doc)
                return {"ok": True, "removed": item_id}
            return {"ok": False, "error": f"没有这条手动条目：{item_id}"}

    def upsert_manual_records(self, records):
        """把一批条目并进 manual.json，逐条按 rec_key 取新的。

        同步用。传进来的是**合并结果**，但落地时仍然逐条比较：
        从"算出合并结果"到"写盘"之间，本机可能又加了一条。
        """
        with _LOCK:
            doc = self._manual_doc()
            by_id = {str(i.get("id")): i for i in doc["items"]}
            written = 0
            for rec in records or []:
                if not isinstance(rec, dict) or rec.get("id") in (None, ""):
                    continue
                rid = str(rec.get("id"))
                cur = by_id.get(rid)
                if cur is None or rec_key(rec) > rec_key(cur):
                    by_id[rid] = strip_derived(rec)
                    written += 1
            if written:
                doc["items"] = sorted(by_id.values(),
                                      key=lambda i: str(i.get("id")))
                self._write(self.manual_path, doc)
            return written

    # ------------------------------------------------------------ 同步状态
    def sync_state(self):
        return self._read(self.sync_path) or {}

    def set_sync_state(self, **fields):
        with _LOCK:
            doc = self._read(self.sync_path) or {}
            doc.update(fields)
            self._write(self.sync_path, doc)
            return doc

    # ------------------------------------------------------------ 汇总
    def all_items(self, kind=None):
        """把三类来源合起来：Canvas（deadline）、交我办（日程）、手动（两者皆可）。"""
        items = list(self.calendar_items()) + list(self.manual_items())
        if kind:
            items = [i for i in items if i.get("kind") == kind]
        return sort_items(items)

    def pending_manual_deadlines(self):
        return [i for i in self.manual_items() if i.get("kind") == KIND_DEADLINE]

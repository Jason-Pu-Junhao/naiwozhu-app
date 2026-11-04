# -*- coding: utf-8 -*-
"""本地数据仓库：交我办日程的导入快照 + 手动录入的条目 + Canvas 缓存。

存这几个文件（都在 app/data/ 下，不进 Git）：

    calendar.json   最近一次成功导入的交我办快照
    manual.json     网页/语音自行录入的条目（删除留墓碑，见 delete_manual）
    hides.json      交我办条目的隐藏记录（只藏不删，见 hide/restore）
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
from datetime import datetime, timedelta, timezone

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


class ItemError(ValueError):
    """改/删条目时给用户看的错误。status 是建议的 HTTP 状态码。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


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
        self.hides_path = os.path.join(self.dir, "hides.json")
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

    # ------------------------------------------------------------ 改一条手动条目
    # 这里只认**存储层**字段名。API 那边收的是短名（start/end/due/note），
    # 由 server.py 的 ITEM_FIELD_MAP 先转一道，别在两层各认一套。
    MANUAL_PATCH_FIELDS = ("title", "due_at", "start_at", "end_at", "all_day",
                           "location", "course", "description")
    PATCH_BY_KIND = {
        KIND_DEADLINE: ("title", "due_at", "course", "description"),
        KIND_SCHEDULE: ("title", "start_at", "end_at", "all_day", "location",
                        "course", "description"),
    }

    def update_manual(self, item_id, **fields):
        """改一条手动条目。kind 不可改。

        只在**事实层**上改字段：remaining / start_local 这些派生字段读的时候
        现算（见 model.DERIVED），这里碰它们反而会把过期的旧值写进盘。
        """
        rid = str(item_id)
        unknown = [k for k in fields if k not in self.MANUAL_PATCH_FIELDS]
        if unknown:
            raise ItemError("这些字段改不了：" + "、".join(unknown))
        if not fields:
            raise ItemError("没有要改的字段")
        with _LOCK:
            doc = self._manual_doc()
            for it in doc["items"]:
                if str(it.get("id")) != rid:
                    continue
                if it.get("deleted"):
                    raise ItemError("这条已经删掉了，先恢复再改", 404)
                kind = it.get("kind")
                allowed = self.PATCH_BY_KIND.get(kind) or ()
                bad = [k for k in fields if k not in allowed]
                if bad:
                    raise ItemError(f"这条是「{kind}」，改不了：" + "、".join(bad))
                self._apply_manual_fields(it, fields)
                it["updated_at"] = _now_iso()
                it["rev"] = int(it.get("rev") or 1) + 1
                self._write(self.manual_path, doc)
                return decorate(strip_derived(it))
            raise ItemError(f"没有这条手动条目：{rid}", 404)

    @staticmethod
    def _apply_manual_fields(it, fields):
        """把一批存储层字段盖到一条事实记录上，顺带校验。"""
        if "title" in fields:
            title = str(fields.get("title") or "").strip()
            if not title:
                raise ItemError("标题不能为空")
            it["title"] = title
        for key in ("course", "location", "description"):
            if key in fields:
                val = str(fields.get(key) or "").strip()
                if len(val) > 200:
                    raise ItemError("文字太长了（上限 200 字）")
                it[key] = val
        if "due_at" in fields:
            due = parse_dt(fields.get("due_at"))
            if due is None:
                raise ItemError("截止时间解析不出来，写成 2026-11-07 14:00 这样", 422)
            it["due_at"] = due.isoformat()
        if "all_day" in fields:
            it["all_day"] = bool(fields.get("all_day"))
        if "start_at" in fields or "end_at" in fields:
            start = parse_dt(fields.get("start_at")) if "start_at" in fields \
                else parse_dt(it.get("start_at"))
            if "end_at" in fields:
                end = parse_dt(fields.get("end_at"))      # 传空表示清掉结束时间
            else:
                end = parse_dt(it.get("end_at"))
            if start is None:
                raise ItemError("日程必须有开始时间", 422)
            if end is not None and end < start:
                raise ItemError("结束时间比开始时间早", 422)
            it["start_at"] = start.isoformat()
            it["end_at"] = end.isoformat() if end else None

    def restore_manual(self, item_id):
        """把删掉的手动条目恢复回来。

        恢复**不能**物理删记录：对端还存着那条墓碑，一同步就会把它复活。
        所以这里写一条 deleted=False、rev 更大的记录，合并时自然赢。
        """
        rid = str(item_id)
        with _LOCK:
            doc = self._manual_doc()
            for it in doc["items"]:
                if str(it.get("id")) != rid:
                    continue
                if not it.get("deleted"):
                    return {"ok": True, "restored": rid, "already": True}
                it["deleted"] = False
                it.pop("deleted_at", None)
                it["updated_at"] = _now_iso()
                it["rev"] = int(it.get("rev") or 1) + 1
                self._write(self.manual_path, doc)
                return {"ok": True, "restored": rid}
            return {"ok": False, "error": f"没有这条手动条目：{rid}"}

    # ------------------------------------------------------------ 隐藏（交我办条目）
    def _hides_doc(self):
        """hides.json：交我办条目「不显示」的记录。

        结构故意和 manual.json 的墓碑一致（deleted / deleted_at / updated_at /
        rev / device_id），同步合并就能直接复用 rec_key 的全序比较。
        交我办的原始快照一个字都不改，导入一次也不会把隐藏冲掉。
        """
        with _LOCK:
            doc = self._read(self.hides_path) or {}
            if not isinstance(doc.get("items"), dict):
                doc = {"schema": 1, "items": {}}
            return doc

    def hidden_ids(self):
        """当前被隐藏的条目 id 集合。读路径每查一次都要，所以只读这一份文件。"""
        with _LOCK:
            return {str(k) for k, v in self._hides_doc()["items"].items()
                    if isinstance(v, dict) and v.get("deleted")}

    def hide_items(self, include_deleted=True):
        """全部隐藏记录（含「恢复」留下的墓碑），同步用。"""
        with _LOCK:
            out = []
            for rid, rec in self._hides_doc()["items"].items():
                if not isinstance(rec, dict):
                    continue
                if not include_deleted and rec.get("deleted"):
                    continue
                out.append({**rec, "id": str(rid)})
            out.sort(key=lambda r: str(r.get("id")))
            return out

    def set_hidden(self, item_id):
        """隐藏一条。幂等：已经藏过就原样返回。"""
        rid = str(item_id)
        with _LOCK:
            doc = self._hides_doc()
            cur = doc["items"].get(rid)
            if isinstance(cur, dict) and cur.get("deleted"):
                return {"ok": True, "hidden": rid, "already": True}
            now = _now_iso()
            rec = dict(cur) if isinstance(cur, dict) else {}
            rec.update({"id": rid, "deleted": True, "deleted_at": now,
                        "updated_at": now,
                        "rev": int(rec.get("rev") or 0) + 1,
                        "device_id": self.device_id()})
            doc["items"][rid] = rec
            self._write(self.hides_path, doc)
            return {"ok": True, "hidden": rid, "rev": rec["rev"]}

    def clear_hidden(self, item_id):
        """恢复被隐藏的条目。

        和 restore_manual 同理，不能物理删记录：对端还留着那条隐藏记录，
        一同步就会重新藏上。这里写 deleted=False、rev+1 的记录。
        """
        rid = str(item_id)
        with _LOCK:
            doc = self._hides_doc()
            cur = doc["items"].get(rid)
            if not isinstance(cur, dict) or not cur.get("deleted"):
                return {"ok": True, "restored": rid, "already": True}
            cur["deleted"] = False
            cur.pop("deleted_at", None)
            cur["updated_at"] = _now_iso()
            cur["rev"] = int(cur.get("rev") or 0) + 1
            doc["items"][rid] = cur
            self._write(self.hides_path, doc)
            return {"ok": True, "restored": rid}

    def upsert_hides(self, records):
        """把一批隐藏记录并进 hides.json，逐条按 rec_key 取新的。同步用。"""
        with _LOCK:
            doc = self._hides_doc()
            by_id = {str(k): v for k, v in doc["items"].items()
                     if isinstance(v, dict)}
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
                doc["items"] = dict(sorted(by_id.items()))
                self._write(self.hides_path, doc)
            return written

    def prune_hides(self, alive_ids, keep_days=30):
        """清掉「条目已经不在了」且放够时间的隐藏记录。返回清掉的 id 列表。

        留一段时间是因为对端可能还没同步到这条隐藏记录，清太早会把它复活。
        """
        alive = {str(x) for x in (alive_ids or [])}
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        with _LOCK:
            doc = self._hides_doc()
            kept, dropped = {}, []
            for rid, rec in doc["items"].items():
                key = str(rid)
                if key in alive or not isinstance(rec, dict):
                    kept[key] = rec
                    continue
                t = parse_dt(rec.get("deleted_at") or rec.get("updated_at"))
                if t is not None and t < cutoff:
                    dropped.append(key)
                else:
                    kept[key] = rec
            if dropped:
                doc["items"] = kept
                self._write(self.hides_path, doc)
            return dropped

    def orphan_hides(self):
        """快照里已经找不到对应条目的隐藏记录（状态接口用，只报数不清理）。"""
        alive = {str(i.get("id"))
                 for i in (self.calendar_snapshot().get("items") or [])}
        return sorted(rid for rid in self.hidden_ids() if rid not in alive)

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

    def effective_items(self, kind=None):
        """查询结果用的条目：把被隐藏的交我办条目剔掉。

        **必须**在 in_window() 过滤之前调用（server.py 里就是这么用的）：
        顺序反了，隐藏的条目会在按时间窗筛完之后再被剔掉，
        数出来的数量、以及"这一窗里有没有内容"的判断都会错。
        """
        items = self.all_items(kind=kind)
        hidden = self.hidden_ids()
        if not hidden:
            return items
        return [i for i in items if str(i.get("id")) not in hidden]

    def get_item(self, item_id):
        """按 id 找一条，交我办快照与手动条目里都找。找不到返回 None。

        删除/隐藏过的也找得到——恢复、幂等判断都要靠它。
        """
        rid = str(item_id)
        with _LOCK:
            for it in self.calendar_snapshot().get("items") or []:
                if str(it.get("id")) == rid:
                    return decorate(it)
            for it in self._manual_doc()["items"]:
                if str(it.get("id")) == rid:
                    return decorate(it)
        return None

    def hidden_calendar_items(self):
        """被隐藏的交我办条目（网页上的「已隐藏」列表与恢复用）。"""
        hidden = self.hidden_ids()
        if not hidden:
            return []
        items = [decorate(i) for i in (self.calendar_snapshot().get("items") or [])
                 if str(i.get("id")) in hidden]
        return sort_items(items)

    def pending_manual_deadlines(self):
        return [i for i in self.manual_items() if i.get("kind") == KIND_DEADLINE]

# -*- coding: utf-8 -*-
"""本地数据仓库：交我办日程的导入快照 + 手动录入的条目。

存三个文件（都在 app/data/ 下，不进 Git）：

    calendar.json   最近一次成功导入的交我办快照
    manual.json     网页/语音自行录入的条目
    demo.json       只存演示模式的开关（演示条目本身每次现生成，见 demo.py）

导入按**时间戳**判断新旧：文件里的 exported_at 不比已存的更新时直接跳过，
所以重复拖同一个文件、或者拖了一个旧的备份，都不会把新数据覆盖掉。
"""
import json
import os
import time
from datetime import datetime, timezone

from model import (CST, KIND_DEADLINE, KIND_SCHEDULE, KINDS, SRC_MANUAL,
                   parse_dt, make_deadline, make_schedule, sort_items)
import calendar_import

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.join(HERE, "data")

# 想换个地方存数据（比如放进 U 盘、或者测试用临时目录），设这个环境变量。
# 测试靠它避免碰到你真实的日历和手动记录。
ENV_DATA_DIR = "NAIWOZHU_DATA_DIR"


def _now_iso():
    return datetime.now(timezone.utc).astimezone(CST).isoformat()


class Store:
    def __init__(self, data_dir=None):
        # 数据目录三级覆盖：显式传参 > 环境变量 > 默认 app/data/
        self.dir = (data_dir or os.environ.get(ENV_DATA_DIR)
                    or DEFAULT_DATA_DIR)
        self.inbox = os.path.join(self.dir, "inbox")
        self.cal_path = os.path.join(self.dir, "calendar.json")
        self.manual_path = os.path.join(self.dir, "manual.json")
        self.demo_path = os.path.join(self.dir, "demo.json")
        os.makedirs(self.inbox, exist_ok=True)

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
        return self._read(self.cal_path) or {}

    def calendar_items(self):
        return self.calendar_snapshot().get("items") or []

    def calendar_meta(self):
        s = self.calendar_snapshot()
        return {
            "imported_at": s.get("imported_at"),
            "exported_at": s.get("exported_at"),
            "filename": s.get("filename"),
            "count": len(s.get("items") or []),
            "stats": s.get("stats") or {},
        }

    def import_payload(self, raw, filename=None, force=False, fallback_mtime=None):
        """收下一份交我办导出数据。

        返回一份说明：是否收下、是否因为旧而被跳过、解析到几条。
        """
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
            "stats": stats,
            "range": payload.get("range") or {},
            "keys_seen": payload.get("keys_seen") or [],
            "items": items,
        }
        self._write(self.cal_path, snap)

        msg = f"已导入 {len(items)} 条日程"
        if old_at:
            msg += f"（替换掉 {old_at.astimezone(CST):%m-%d %H:%M} 的旧数据）"
        return {"ok": True, "stale": False, "message": msg,
                "count": len(items), "stats": stats,
                "exported_at": snap["exported_at"],
                "imported_at": snap["imported_at"], "filename": filename}

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
        doc = self._read(self.manual_path) or {}
        if not isinstance(doc.get("items"), list):
            doc = {"seq": 0, "items": []}
        return doc

    def manual_items(self):
        return self._manual_doc()["items"]

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

        doc = self._manual_doc()
        doc["seq"] = int(doc.get("seq") or 0) + 1
        item_id = f"manual-{doc['seq']}"

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

        item["created_at"] = _now_iso()
        doc["items"].append(item)
        self._write(self.manual_path, doc)
        return item

    def delete_manual(self, item_id):
        doc = self._manual_doc()
        before = len(doc["items"])
        doc["items"] = [i for i in doc["items"] if str(i.get("id")) != str(item_id)]
        if len(doc["items"]) == before:
            return {"ok": False, "error": f"没有这条手动条目：{item_id}"}
        self._write(self.manual_path, doc)
        return {"ok": True, "removed": item_id}

    # ------------------------------------------------------------ 汇总
    def all_items(self, kind=None):
        """把三类来源合起来：Canvas（deadline）、交我办（日程）、手动（两者皆可）。"""
        items = list(self.calendar_items()) + list(self.manual_items())
        if kind:
            items = [i for i in items if i.get("kind") == kind]
        return sort_items(items)

    def pending_manual_deadlines(self):
        return [i for i in self.manual_items() if i.get("kind") == KIND_DEADLINE]

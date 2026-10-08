# -*- coding: utf-8 -*-
"""把交我办导出的 JSON 解析成「日程」条目。

数据链路是这样的：

    浏览器（你已登录的页面）
        └─ tools/sjtu_calendar_export.js  →  sjtu-calendar-<时间戳>.json
                                              └─ 本模块解析 → model.make_schedule

解析写在这里、不写在浏览器脚本里，是故意的：脚本只负责**原样搬运**服务端返回的
原始字段，字段怎么理解由 Python 一侧决定。这样万一交我办改了字段名，只要改这一个
文件，不用让用户重新贴脚本。
"""
import re
from datetime import datetime

from model import (KIND_SCHEDULE, SRC_CALENDAR, parse_dt, make_schedule,
                   sort_items)

# 接口字段名没有公开文档，所以每个语义都试几个候选名，命中即可。
FIELD_ALIASES = {
    "id":          ("eventId", "event_id", "id"),
    "title":       ("title", "name", "summary", "subject", "titleEn"),
    "start":       ("startTime", "start_time", "start", "beginTime",
                    "begin_time", "dtStart", "dtstart"),
    "end":         ("endTime", "end_time", "end", "finishTime", "dtEnd", "dtend"),
    "all_day":     ("allDay", "all_day", "isAllDay", "is_all_day"),
    "location":    ("location", "locationEn", "place", "address"),
    "description": ("description", "desc", "content", "remark", "memo"),
    "calendar_id": ("calendarId", "calendar_id", "calId", "cal_id"),
}

# 交大日历里有些日历本身就是课表性质。这只是个展示用的标记，
# 不影响类型——按约定，来自交我办的一律记作「日程」。
COURSE_LIKE = re.compile(r"课程|课表|course", re.I)


def pick(d, key, default=None):
    """按候选名列表从一个 dict 里取值。"""
    for name in FIELD_ALIASES[key]:
        if name in d and d[name] not in (None, ""):
            return d[name]
    return default


def _truthy(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "y", "是")


def _calendar_names(payload):
    """calendarId → 日历名。"""
    cals = payload.get("calendars") or {}
    out = {}
    for group in ("my", "share"):
        for c in (cals.get(group) or []):
            if isinstance(c, dict) and c.get("id") is not None:
                out[str(c["id"])] = c.get("name") or ""
    return out


def to_schedule_items(payload):
    """把 payload 里的 events 变成日程条目。

    返回 (items, stats)。stats 记录用了哪些字段名、跳过了几条，
    便于导入后确认"接口没改版、数据没丢"。
    """
    names = _calendar_names(payload)
    items, skipped = [], []
    used_keys = set()
    seen = set()

    for ev in (payload.get("events") or []):
        if not isinstance(ev, dict):
            continue

        eid = pick(ev, "id")
        title = pick(ev, "title")
        start = parse_dt(pick(ev, "start"))
        end = parse_dt(pick(ev, "end"))
        if eid is None and not title and start is None:
            continue

        # 出现在事件里的已知字段名记下来，方便核对接口版本
        for key in FIELD_ALIASES:
            for name in FIELD_ALIASES[key]:
                if name in ev:
                    used_keys.add(name)

        if start is None and end is None:
            skipped.append({"id": eid, "title": title, "why": "没有时间字段"})
            continue

        key = str(eid) if eid is not None else f"{title}|{start}|{end}"
        if key in seen:
            continue
        seen.add(key)

        cal_id = pick(ev, "calendar_id")
        cal_name = names.get(str(cal_id), "") if cal_id is not None else ""

        items.append(make_schedule(
            f"cal-{key}",
            title or "(无标题)",
            start, end, SRC_CALENDAR,
            all_day=_truthy(pick(ev, "all_day")),
            location=pick(ev, "location") or "",
            description=pick(ev, "description") or "",
            calendar=cal_name,
            course_like=bool(COURSE_LIKE.search(cal_name or "")),
            sources=["交我办"],
            extra={"calendar_id": cal_id},
        ))

    stats = {
        "events": len(payload.get("events") or []),
        "parsed": len(items),
        "skipped": skipped,
        "field_names_seen": sorted(used_keys),
        "calendars": sorted(n for n in names.values() if n),
    }
    return sort_items(items), stats


def load_payload(raw):
    """校验并规整一个导入 payload。

    返回 (payload, exported_at, error)。error 非空表示这份文件不能收。
    """
    if not isinstance(raw, dict):
        return None, None, "文件内容不是一个 JSON 对象"

    events = raw.get("events")
    if events is None:
        return None, None, "文件里没有 events 字段，不像是交我办导出文件"
    if not isinstance(events, list):
        return None, None, "events 字段不是列表"

    exported_at = parse_dt(raw.get("exported_at")) or parse_dt(raw.get("exportedAt"))
    if exported_at is None:
        # 没有时间戳就无法判断新旧，退一步用文件的 mtime 由调用方补；
        # 两者都没有时，仍然接受，但要求调用方显式给时间戳。
        return raw, None, None

    return raw, exported_at, None


def inspect(raw):
    """给命令行用的：看一眼这份文件里到底有什么。"""
    payload, exported_at, err = load_payload(raw)
    if err:
        return {"ok": False, "error": err}
    items, stats = to_schedule_items(payload)
    return {
        "ok": True,
        "exported_at": exported_at.isoformat() if isinstance(exported_at, datetime) else None,
        "count": len(items),
        "stats": stats,
        "sample": items[:5],
    }

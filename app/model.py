# -*- coding: utf-8 -*-
"""两种日期类型的数据模型。

这个项目里有两类时间，它们的语义不一样，所以存法也不一样：

  · ``deadline`` —— 有"必须完成/提交"的性质，真正关心的只有一个截止时刻。
  · ``日程``     —— 有起止时间的一段安排，出席即可。

类型由来源决定，不靠猜：

  Canvas 的数据         → deadline
  交我办日历的数据      → 日程
  网页 / 语音自行录入的 → **必须由用户说明是哪一种**（见 store.add_manual）

网页版和后面的语音版共用这一份模型，避免同一个概念在两处各写一遍。
"""
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))   # 交大在东八区

KIND_DEADLINE = "deadline"
KIND_SCHEDULE = "日程"
KINDS = (KIND_DEADLINE, KIND_SCHEDULE)

# 来源标识
SRC_CANVAS = "canvas"
SRC_CALENDAR = "交我办"
SRC_MANUAL = "手动录入"

# 给人看的类型名
KIND_LABEL = {KIND_DEADLINE: "截止", KIND_SCHEDULE: "日程"}

# 派生字段：它们是从时间字段**现算**出来的，不是事实。
#
# 存盘和同步包里都不该有它们——"还有 3 天"这种东西一旦落盘，第二天就是错的。
# 所以在写盘前 strip_derived，读出来后再 decorate。（真机上报过：导入后过一周，
# 页面上 75 条条目的徽章文字全是导入那天算出来的。）
DERIVED = ("remaining", "overdue", "when_local", "due_local",
           "start_local", "end_local")


# ---------------------------------------------------------------- 时间工具
def parse_dt(s):
    """把各种写法的时间转成带时区的 datetime，转不了就返回 None。

    交我办返回的时间格式没有公开文档，所以这里写得宽容一点：
    常见的几种字符串写法都试一遍，纯数字则按"毫秒时间戳"理解。
    """
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=CST)
    if isinstance(s, (int, float)):
        try:
            secs = s / 1000.0 if s > 1e11 else float(s)
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except Exception:
            return None

    txt = str(s).strip()
    # 纯数字字符串
    if txt.isdigit():
        return parse_dt(int(txt))

    # ISO8601（Canvas 用的是 2026-10-10T15:59:59Z）
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        # 表单里传来的 "2026-10-08T14:00" 是不带时区的，按东八区补上
        return dt if dt.tzinfo else dt.replace(tzinfo=CST)
    except Exception:
        pass

    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
              "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
              "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(txt, f).replace(tzinfo=CST)
        except Exception:
            continue
    return None


def fmt(dt):
    """时刻 → “10-08 14:00”"""
    if dt is None:
        return "未设时间"
    return dt.astimezone(CST).strftime("%m-%d %H:%M")


def remaining(dt):
    """返回 (人类可读剩余时间, 是否已过期)。"""
    if dt is None:
        return "—", False
    delta = dt - datetime.now(timezone.utc)
    secs = delta.total_seconds()
    if secs < 0:
        over = -secs
        if over < 3600:
            return f"已过期 {int(over // 60)} 分钟", True
        if over < 86400:
            return f"已过期 {int(over // 3600)} 小时", True
        return f"已过期 {int(over // 86400)} 天", True
    if secs < 3600:
        return f"还有 {int(secs // 60)} 分钟", False
    if secs < 86400:
        return f"还有 {int(secs // 3600)} 小时", False
    return f"还有 {int(secs // 86400)} 天", False


def schedule_state(start, end):
    """日程的状态文案：还有 x 天 / 进行中 / 已结束。"""
    now = datetime.now(timezone.utc)
    if end is not None and now > end:
        return "已结束", True
    if start is not None and now < start:
        return remaining(start)[0], False
    return "进行中", False


def when_local(start, end, all_day=False):
    """把一段起止时间排成人看的一行字。"""
    if start is None:
        return "未设时间"
    s = start.astimezone(CST)
    e = end.astimezone(CST) if end else None

    if all_day:
        if e and e.date() != s.date():
            return f"{s:%m-%d} ~ {e:%m-%d}"
        return f"{s:%m-%d}（全天）"
    if e and e.date() != s.date():
        return f"{s:%m-%d %H:%M} ~ {e:%m-%d %H:%M}"
    if e:
        return f"{s:%m-%d %H:%M}–{e:%H:%M}"
    return f"{s:%m-%d %H:%M}"


# ---------------------------------------------------------------- 构造条目
def make_deadline(item_id, title, due_at, source, *, course="", html_url="",
                  sources=None, extra=None):
    """造一条 deadline。"""
    rem, overdue = remaining(due_at)
    rec = {
        "kind": KIND_DEADLINE,
        "id": item_id,
        "title": title or "(无标题)",
        "source": source,
        "course": course,
        "due_at": due_at.isoformat() if due_at else None,
        "due_local": fmt(due_at),
        "when_local": fmt(due_at),
        "remaining": rem,
        "overdue": overdue,
        "html_url": html_url or "",
        "sources": sorted(sources or []),
    }
    if extra:
        rec.update(extra)
    return rec


def make_schedule(item_id, title, start_at, end_at, source, *, all_day=False,
                  location="", description="", calendar="", course_like=False,
                  sources=None, extra=None):
    """造一条日程。"""
    state, ended = schedule_state(start_at, end_at)
    rec = {
        "kind": KIND_SCHEDULE,
        "id": item_id,
        "title": title or "(无标题)",
        "source": source,
        "start_at": start_at.isoformat() if start_at else None,
        "end_at": end_at.isoformat() if end_at else None,
        "start_local": fmt(start_at),
        "end_local": fmt(end_at),
        "when_local": when_local(start_at, end_at, all_day),
        "all_day": bool(all_day),
        "remaining": state,
        "overdue": ended,
        "location": location or "",
        "description": description or "",
        "calendar": calendar or "",
        "course_like": bool(course_like),
        "sources": sorted(sources or []),
    }
    if extra:
        rec.update(extra)
    return rec


# ------------------------------------------------- 事实层 / 显示层
def strip_derived(item):
    """去掉派生字段，只留下事实。写盘和放进同步包之前用它。"""
    if not isinstance(item, dict):
        return item
    return {k: v for k, v in item.items() if k not in DERIVED}


def decorate(item):
    """补齐派生字段。从磁盘或同步包读出来的条目，出门前都要过这一道。

    重算一遍、不信存着的值，是因为"还剩多久"取决于**现在**。
    """
    if not isinstance(item, dict):
        return item
    rec = dict(item)

    if rec.get("kind") == KIND_DEADLINE:
        due = parse_dt(rec.get("due_at"))
        rem, over = remaining(due)
        rec["remaining"] = rem
        rec["overdue"] = over
        rec["due_local"] = fmt(due)
        rec["when_local"] = fmt(due)
        return rec

    if rec.get("kind") == KIND_SCHEDULE:
        start = parse_dt(rec.get("start_at"))
        end = parse_dt(rec.get("end_at"))
        state, ended = schedule_state(start, end)
        rec["remaining"] = state
        rec["overdue"] = ended
        rec["start_local"] = fmt(start)
        rec["end_local"] = fmt(end)
        rec["when_local"] = when_local(start, end, bool(rec.get("all_day")))
        return rec

    # 不认识的类型：原样返回，不替它编造字段
    return rec


def rec_key(rec):
    """同步裁决用的全序键：(更新时间, 修订号, 设备号)。

    时间戳只当第一顺位：两台机器的时钟一旦不一致，光看时间会静默丢数据。
    rev 在同一台设备内单调递增，device_id 让"完全相同"也有确定答案，
    所以合并结果不依赖两边谁先算——这是"一次往返就收敛"的前提。
    """
    if not isinstance(rec, dict):
        return (datetime.min.replace(tzinfo=timezone.utc), 0, "")
    t = parse_dt(rec.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc)
    try:
        rev = int(rec.get("rev") or 0)
    except (TypeError, ValueError):
        rev = 0
    return (t, rev, str(rec.get("device_id") or ""))


# ---------------------------------------------------------------- 排序
def sort_key(item):
    """按时间顺序排，没有时间的排到最后。"""
    t = item.get("due_at") or item.get("start_at")
    return (t is None, t or "")


def sort_items(items):
    return sorted(items, key=sort_key)

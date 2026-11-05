# -*- coding: utf-8 -*-
"""到点提醒：算「现在该念哪几条」。

分工是刻意的：**判定在这里，念的时机在终端。**

  · 只有这边知道条目、隐藏、已交状态、时间窗；
  · 只有终端有喇叭、有「现在闲不闲」这件事；
  · 电脑端不做主动推送——推送要常驻长连接，电脑一关就断，
    而且「推什么」和「什么时候推」会散在两台机器上。

三个提醒位：

    morning  每日 08:00 起 hold_min 内 —— 今日日程 + 今日截止 + 已过期未交
    evening  每日 22:00 起 hold_min 内 —— 明日日程 + 明日截止
    soon     日程开始前 before_min 内 —— 一条一条提醒，不带总览

几处刻意的选择：

  · **只用本机已存的数据，不发任何网络请求**。早上八点网还没连上的时候
    也得能开口；Canvas 截止用的是上次抓取的缓存。
  · **一小时内就要开始的日程不进总览**，改由 soon 单独念。否则 8:00 的
    总览和 8:00 的「一小时后」会把同一件事说两遍。
  · **每条都带 remind_id**，终端按它去重；同一件事换个说法还是同一个 id，
    不用终端自己拼字符串。
  · **时间是窗口，不是闹钟**：8:00–12:00 之间任何时刻问都算「早间」，
    十点才开机也还来得及听见今天的安排。
"""
from datetime import datetime, timedelta, timezone

import config
import model
from model import CST, KIND_DEADLINE, KIND_SCHEDULE, parse_dt

MORNING = "morning"
EVENING = "evening"
SOON = "soon"
STAGES = (MORNING, EVENING, SOON)

# 念的时候按这个顺序：眼前的事先说，再是今天的，最后是明天的。
PRIORITY = {SOON: 0, MORNING: 1, EVENING: 2}

STAGE_LABEL = {
    MORNING: "今天的安排",
    EVENING: "明天的安排",
    SOON: "即将开始",
}

# 默认值；改 config.local.json 或环境变量都能覆盖（见 config.load）。
DEFAULTS = {
    "morning": "08:00",
    "evening": "22:00",
    "before_min": 60,      # 日程开始前多少分钟提醒
    "hold_min": 240,       # 一个提醒位的窗口有多长（分钟）
}


# ---------------------------------------------------------------- 小工具
def _cst(dt):
    return dt.astimezone(CST) if dt.tzinfo else dt.replace(tzinfo=CST)


def _hhmm(text):
    """把 "8:00" / "08:00" / "8" 归一成 "HH:MM"，认不出来返回 None。"""
    if text is None:
        return None
    txt = str(text).strip()
    if not txt:
        return None
    parts = txt.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _at(day, hhmm):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CST)


def settings(cfg=None):
    """提醒设置。缺什么给默认值，调用方不用判空。"""
    if cfg is None:
        cfg = config.load()
    raw = cfg.get("remind") if isinstance(cfg, dict) else None
    raw = raw if isinstance(raw, dict) else {}

    out = dict(DEFAULTS)
    for key in ("morning", "evening"):
        val = _hhmm(raw.get(key))
        if val:
            out[key] = val
    for key in ("before_min", "hold_min"):
        try:
            out[key] = max(0, int(raw.get(key, DEFAULTS[key])))
        except (TypeError, ValueError):
            pass
    return out


def windows(now, st=None):
    """现在落在哪些提醒位的窗口里。

    返回 [{"stage", "ref_date", "start", "end"}]。ref_date 是这个窗口
    「以哪一天为准」：22:00 之后过了零点还算前一天的晚间，
    这时「明天」仍然是昨天看的那个明天。
    """
    st = st or settings()
    now = _cst(now)
    today = now.date()
    hold = timedelta(minutes=st["hold_min"])

    found = []
    for stage, key in ((MORNING, "morning"), (EVENING, "evening")):
        for day in (today - timedelta(days=1), today):
            start = _at(day, st[key])
            end = start + hold
            if start <= now < end:
                found.append({"stage": stage, "ref_date": day,
                              "start": start, "end": end})
    return found


# ---------------------------------------------------------------- 条目判定
def _time_of(item):
    """条目「那个时刻」：日程看开始，截止看到期。没有时间的返回 None。"""
    if item.get("kind") == KIND_SCHEDULE:
        return parse_dt(item.get("start_at"))
    return parse_dt(item.get("due_at"))


def _day_of(item):
    t = _time_of(item)
    return t.astimezone(CST).date() if t else None


def _ended(item, now):
    """日程是否已经结束。截止没有「结束」这回事（过期也算还在欠着）。

    全天日程单独处理：交我办导出的全天事件，「结束时间」有时等于开始
    （都是当天 00:00），照直比会在零点就被判成"已结束"，当天再也提不到。
    这种就按"当天 23:59 结束"算。
    """
    if item.get("kind") != KIND_SCHEDULE:
        return False
    start = parse_dt(item.get("start_at"))
    end = parse_dt(item.get("end_at")) or start
    if item.get("all_day") and start is not None and (end is None or end <= start):
        s = start.astimezone(CST)
        end = datetime(s.year, s.month, s.day, 23, 59, 59, tzinfo=CST)
    return end is not None and end <= now


def _soon_ready(item, now, st):
    """这条日程是不是「马上就开始了」，该由 soon 单独念。"""
    if item.get("kind") != KIND_SCHEDULE:
        return False
    start = parse_dt(item.get("start_at"))
    if start is None or start <= now or _ended(item, now):
        return False
    return start - now <= timedelta(minutes=st["before_min"])


def _entry(item, stage, reason, ref, now):
    """一条要念的东西。字段只留终端用得着的，别把整条记录抛出去。"""
    t = _time_of(item)
    item_id = str(item.get("id"))
    rec = {
        "stage": stage,
        "reason": reason,
        "id": item_id,
        "kind": item.get("kind"),
        "title": item.get("title") or "(无标题)",
        "when_local": item.get("when_local") or "",
        "at": t.isoformat() if t else None,
        "all_day": bool(item.get("all_day")),
        "course": item.get("course") or "",
        "source": item.get("source") or "",
        "location": item.get("location") or "",
    }
    if stage == SOON:
        rec["remind_id"] = f"soon:{item_id}:{item.get('start_at')}"
        rec["minutes_left"] = max(0, int((t - now).total_seconds() // 60)) if t else 0
    else:
        rec["remind_id"] = f"{stage}:{ref.isoformat()}:{item_id}"
    return rec


def _stage(stage, ref, item_list, now, limit):
    """一个提醒位的成品。顺序按时间，多的截掉并标出来。"""
    entries = sorted(item_list, key=lambda e: e.get("at") or "")
    dropped = max(0, len(entries) - max(0, limit))
    entries = entries[:max(0, limit)]
    stage_id = (f"soon:{now.strftime('%Y-%m-%dT%H:%M')}" if stage == SOON
                else f"{stage}:{ref.isoformat()}")
    return {
        "stage": stage,
        "label": STAGE_LABEL[stage],
        "remind_id": stage_id,
        "due": True,
        "items": entries,
        "count": len(entries),
        "truncated": dropped > 0,
        "dropped": dropped,
    }


# ---------------------------------------------------------------- 入口
def collect(items, now=None, cfg=None, preview=False, limit=8):
    """现在该念什么。

    items 传 ``store.effective_items()``（已经剔掉隐藏的）；
    now 传 None 用当前时刻，测试传固定时刻；
    preview=True 时三个位都算一遍，不管窗口（给调试和演示用）。
    """
    st = settings(cfg)
    now = _cst(now or datetime.now(timezone.utc))
    pool = [i for i in (items or []) if isinstance(i, dict)]
    today = now.date()

    stages = []

    # ---- soon：眼前的事，优先级最高，永远参与判定
    soon_items = []
    for item in pool:
        if not _soon_ready(item, now, st):
            continue
        soon_items.append(_entry(item, SOON, "一小时内开始", today, now))
    if soon_items:
        stages.append(_stage(SOON, today, soon_items, now, limit))
    elif preview:
        stages.append(_stage(SOON, today, [], now, limit))

    # ---- 总览：落在哪个窗口就算哪个
    due_windows = windows(now, st)
    if preview:
        for stage, ref in ((MORNING, today), (EVENING, today)):
            if not any(w["stage"] == stage for w in due_windows):
                due_windows.append({"stage": stage, "ref_date": ref,
                                    "start": now, "end": now})

    for w in sorted(due_windows, key=lambda x: PRIORITY[x["stage"]]):
        stage, ref = w["stage"], w["ref_date"]
        target = ref + timedelta(days=1) if stage == EVENING else ref
        picked = []
        for item in pool:
            if _ended(item, now):
                continue
            if _day_of(item) != target:
                continue
            # 一小时内就要开始的日程交给 soon，总览里不再重复
            if _soon_ready(item, now, st):
                continue
            kind = item.get("kind")
            if kind == KIND_SCHEDULE:
                picked.append(_entry(item, stage, "今日日程" if stage == MORNING
                                     else "明日日程", ref, now))
            elif kind == KIND_DEADLINE:
                picked.append(_entry(item, stage, "今日截止" if stage == MORNING
                                     else "明日截止", ref, now))

        # 已过期没交的只在早间提一次：那正是该补作业的时候
        if stage == MORNING:
            overdue = []
            for item in pool:
                if item.get("kind") != KIND_DEADLINE:
                    continue
                t = _time_of(item)
                if t is not None and t < now:
                    overdue.append(_entry(item, MORNING, "已过期未交", ref, now))
            # 过期的排前面：欠着的事比今天的安排更该先说
            picked = sorted(overdue, key=lambda e: e.get("at") or "")[:5] + picked

        stages.append(_stage(stage, ref, picked, now, limit))

    # 空的总览不必返回：终端拿到空列表就当「没到点」
    stages = [s for s in stages if s["items"] or preview]
    for s in stages:
        s["due"] = True
    stages.sort(key=lambda s: PRIORITY[s["stage"]])

    return {
        "now": now.isoformat(timespec="seconds"),
        "stages": stages,
        # preview 会把三个位都列出来（可能是空的），due_count 只数真的有内容的
        "due_count": len([s for s in stages if s["items"]]),
        "count": sum(s["count"] for s in stages),
        "settings": st,
        "preview": bool(preview),
    }

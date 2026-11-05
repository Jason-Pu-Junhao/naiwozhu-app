#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""到点提醒（remind.py）的验证。

这一套全是纯函数，不联网、不建 Store、不碰你的真实数据：
时间靠参数传进去（``now=...``），所以 8:00 / 22:00 / 跨零点这些
一天里只会发生一次的时刻，在这里想测多少次都行。

覆盖的重点：
  · 三个提醒位的窗口边界（08:00 开始、12:00 结束，22:00 跨零点）
  · 「一小时内开始」的日程不进总览——否则同一件事会被念两遍
  · 已过期未交只在早间提，且排在前面
  · remind_id 稳定（终端靠它去重）
  · 关掉的（已结束）、不在窗口里的、没时间的条目都不出现

运行：
    .venv/bin/python app/test/test_remind.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))          # app/test/
APP = os.path.dirname(HERE)                                # app/
sys.path.insert(0, APP)

# config 会读 app/config.local.json（里面是你真实的令牌），指到临时目录去。
_TMP = tempfile.mkdtemp(prefix="naiwozhu-remind-")
os.environ["NAIWOZHU_CONFIG"] = os.path.join(_TMP, "config.local.json")
for _env in ("NAIWOZHU_REMIND_MORNING", "NAIWOZHU_REMIND_EVENING",
             "NAIWOZHU_REMIND_BEFORE_MIN", "NAIWOZHU_REMIND_HOLD_MIN"):
    os.environ.pop(_env, None)

import config                                   # noqa: E402
import remind                                   # noqa: E402
from model import CST, make_deadline, make_schedule   # noqa: E402

OK, BAD = 0, 0


def check(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        BAD += 1
        print(f"  ✗ {name}  {extra}")


# ---------------------------------------------------------------- 造数据
BASE = datetime(2026, 11, 6, 8, 0, tzinfo=CST)      # 星期五早上八点


def sch(item_id, title, hour, day=0, *, minute=0, hours=1, all_day=False,
        source="交我办", location=""):
    start = (BASE + timedelta(days=day)).replace(hour=hour, minute=minute)
    return make_schedule(item_id, title, start, start + timedelta(hours=hours),
                         source, all_day=all_day, location=location)


def ddl(item_id, title, hour, day=0, *, minute=0, source="canvas", course=""):
    due = (BASE + timedelta(days=day)).replace(hour=hour, minute=minute)
    return make_deadline(item_id, title, due, source, course=course)


ITEMS = [
    sch("cal-1", "高等数学", 10),                # 今天 10:00 → 早间总览
    sch("cal-2", "大学物理", 9),                 # 今天 09:00 → 8:00 时正好差 1h → soon
    sch("cal-3", "英语", 14, day=1),             # 明天 → 晚间总览
    sch("cal-4", "晨跑", 6),                     # 今天 06:00–07:00 已结束 → 不出现
    sch("cal-5", "全天活动", 0, all_day=True, hours=0),  # 起止都是 00:00 → 早间总览
    ddl("canvas-1", "第五章作业", 23),           # 今天 23:00 截止 → 今日截止
    ddl("canvas-2", "旧作业", 12, day=-3),       # 过期未交 → 只在早间
    ddl("manual-1", "报告", 20, day=1, source="手动录入"),   # 明天截止
    sch("cal-7", "晚课", 18),                    # 今天 18:00 → 下午预览时还在
]


def stages(res):
    return {s["stage"]: s for s in res["stages"]}


def ids(stage):
    return [i["id"] for i in stage["items"]]


# ---------------------------------------------------------------- 用例
def main():
    print("设置：默认值与归一")
    st = remind.settings({})
    check("默认 08:00 / 22:00", st["morning"] == "08:00" and st["evening"] == "22:00", st)
    check("默认提前 60 分钟", st["before_min"] == 60, st)
    check("默认窗口 240 分钟", st["hold_min"] == 240, st)

    st = remind.settings({"remind": {"morning": "7:5", "evening": "21:30",
                                     "before_min": "30", "hold_min": 120}})
    check("★7:5 归一成 07:05", st["morning"] == "07:05", st)
    check("21:30 原样保留", st["evening"] == "21:30", st)
    check("字符串数字能变整数", st["before_min"] == 30 and st["hold_min"] == 120, st)

    st = remind.settings({"remind": {"morning": "25:00", "evening": "abc"}})
    check("非法时刻退回默认", st["morning"] == "08:00" and st["evening"] == "22:00", st)

    print("\n设置：环境变量与配置文件")
    os.environ["NAIWOZHU_REMIND_MORNING"] = "06:40"
    os.environ["NAIWOZHU_REMIND_BEFORE_MIN"] = "15"
    cfg = config.load()
    check("★环境变量覆盖早间时刻", cfg["remind"]["morning"] == "06:40", cfg["remind"])
    check("★环境变量覆盖提前量", cfg["remind"]["before_min"] == 15, cfg["remind"])
    check("没设的那项还是默认", cfg["remind"]["evening"] == "22:00", cfg["remind"])
    for k in ("NAIWOZHU_REMIND_MORNING", "NAIWOZHU_REMIND_BEFORE_MIN"):
        os.environ.pop(k, None)
    check("拿掉环境变量后回到默认", config.load()["remind"]["morning"] == "08:00")

    print("\n窗口：什么时候算「到点」")
    def win(now):
        return [w["stage"] for w in remind.windows(now)]

    check("08:00 正是早间", win(BASE) == ["morning"], win(BASE))
    check("★11:59 还在早间（不是闹钟，是窗口）",
          win(BASE.replace(hour=11, minute=59)) == ["morning"])
    check("★12:00 早间结束", win(BASE.replace(hour=12)) == [], win(BASE.replace(hour=12)))
    check("13:00 什么都不该提醒", win(BASE.replace(hour=13)) == [])
    check("22:00 晚间开始", win(BASE.replace(hour=22)) == ["evening"])
    late = BASE + timedelta(hours=18, minutes=30)          # 次日 02:30
    check("★次日 02:30 晚间已结束", win(late) == [], win(late))
    just_after = BASE.replace(hour=22) + timedelta(hours=2)  # 次日 00:00
    check("★跨零点仍算前一晚的晚间",
          [w["stage"] for w in remind.windows(just_after)] == ["evening"])
    check("跨零点时 ref_date 是前一天",
          remind.windows(just_after)[0]["ref_date"] == BASE.date(),
          remind.windows(just_after)[0]["ref_date"])

    print("\n早间总览（08:00）")
    res = remind.collect(ITEMS, now=BASE)
    got = stages(res)
    check("有两个位：soon 在前、morning 在后",
          [s["stage"] for s in res["stages"]] == ["soon", "morning"],
          [s["stage"] for s in res["stages"]])
    check("soon 里有 8 点后 1 小时的课", ids(got["soon"]) == ["cal-2"], ids(got["soon"]))
    check("soon 给了还剩多少分钟", got["soon"]["items"][0]["minutes_left"] == 60,
          got["soon"]["items"][0])
    check("★soon 里的课不再出现在总览里（不重复念）",
          "cal-2" not in ids(got["morning"]), ids(got["morning"]))
    check("今日日程在总览里", "cal-1" in ids(got["morning"]), ids(got["morning"]))
    check("★全天日程进总览、不进 soon",
          "cal-5" in ids(got["morning"]) and "cal-5" not in ids(got.get("soon", {"items": []})),
          ids(got["morning"]))
    check("今日截止在总览里", "canvas-1" in ids(got["morning"]), ids(got["morning"]))
    check("★已过期未交在总览里，且排最前",
          ids(got["morning"])[0] == "canvas-2", ids(got["morning"]))
    check("已结束的课不出现",
          "cal-4" not in ids(got["morning"]) and "cal-4" not in ids(got.get("soon", {"items": []})))
    check("明天的条目不进早间总览",
          "cal-3" not in ids(got["morning"]) and "manual-1" not in ids(got["morning"]))
    reasons = {i["id"]: i["reason"] for i in got["morning"]["items"]}
    check("reason 分得清：今日日程 / 今日截止 / 已过期未交",
          reasons.get("cal-1") == "今日日程" and reasons.get("canvas-1") == "今日截止"
          and reasons.get("canvas-2") == "已过期未交", reasons)
    check("总数与各段之和一致",
          res["count"] == sum(s["count"] for s in res["stages"]), res["count"])

    print("\n晚间总览（22:00）")
    res = remind.collect(ITEMS, now=BASE.replace(hour=22))
    got = stages(res)
    check("只有晚间这一个位", [s["stage"] for s in res["stages"]] == ["evening"],
          [s["stage"] for s in res["stages"]])
    check("明天的日程在里面", "cal-3" in ids(got["evening"]), ids(got["evening"]))
    check("明天的截止在里面", "manual-1" in ids(got["evening"]), ids(got["evening"]))
    check("★晚间不重复报已过期未交", "canvas-2" not in ids(got["evening"]),
          ids(got["evening"]))
    check("今天的安排不进晚间", "cal-1" not in ids(got["evening"]), ids(got["evening"]))
    ev = {i["id"]: i["reason"] for i in got["evening"]["items"]}
    check("reason 是 明日日程 / 明日截止",
          ev.get("cal-3") == "明日日程" and ev.get("manual-1") == "明日截止", ev)

    print("\n提前提醒（soon）的边界")
    def soon_ids(now, items=None):
        r = remind.collect(items or ITEMS, now=now)
        s = stages(r).get("soon")
        return ids(s) if s else []

    at_9 = BASE.replace(hour=8, minute=0)
    check("整整 60 分钟前提醒（含边界）", soon_ids(at_9) == ["cal-2"], soon_ids(at_9))
    just_out = ITEMS + [sch("cal-6", "两小时后", 11, minute=59)]
    check("★两小时后的课现在不提醒", "cal-6" not in soon_ids(at_9, just_out),
          soon_ids(at_9, just_out))
    during = BASE.replace(hour=9, minute=30)
    check("已经开始的不算「即将开始」", "cal-2" not in soon_ids(during), soon_ids(during))
    check("★截止没有提前一小时提醒", "canvas-1" not in soon_ids(BASE.replace(hour=22, minute=30)),
          soon_ids(BASE.replace(hour=22, minute=30)))

    print("\nremind_id：去重靠它")
    r1 = remind.collect(ITEMS, now=BASE)
    r2 = remind.collect(ITEMS, now=BASE)
    check("同一时刻两次调用，id 完全一致",
          [i["remind_id"] for s in r1["stages"] for i in s["items"]]
          == [i["remind_id"] for s in r2["stages"] for i in s["items"]])
    mid = [i for s in r1["stages"] for i in s["items"] if i["id"] == "canvas-1"][0]
    check("★总览条目的 id 带日期：morning:2026-11-06:canvas-1",
          mid["remind_id"] == "morning:2026-11-06:canvas-1", mid["remind_id"])
    sid = stages(r1)["soon"]["items"][0]["remind_id"]
    check("soon 条目的 id 带开始时刻", sid.startswith("soon:cal-2:2026-11-06T09:00"),
          sid)
    check("位本身也有 id（当天只念一次总览）",
          stages(r1)["morning"]["remind_id"] == "morning:2026-11-06",
          stages(r1)["morning"]["remind_id"])

    print("\n截断与预览")
    many = [sch(f"cal-x{i}", f"课{i}", 10, minute=i * 5) for i in range(5)]
    r = remind.collect(many, now=BASE, limit=3)
    m = stages(r)["morning"]
    check("limit 起作用", m["count"] == 3, m["count"])
    check("★截断有标记，终端好说「还有几条」",
          m["truncated"] is True and m["dropped"] == 2, (m["truncated"], m["dropped"]))
    check("按时间排序", [i["at"] for i in m["items"]] == sorted(i["at"] for i in m["items"]))

    r = remind.collect(ITEMS, now=BASE.replace(hour=13), preview=True)
    got = stages(r)
    check("★preview 三个位都算", set(got) == {"morning", "evening", "soon"}, sorted(got))
    check("★preview 里也有今天的安排（没结束的才算）",
          "cal-7" in ids(got["morning"]) and "cal-1" not in ids(got["morning"]),
          ids(got["morning"]))
    check("preview 里也有明天的安排", "cal-3" in ids(got["evening"]), ids(got["evening"]))
    check("preview 标记为真", r["preview"] is True)

    print("\n空与边界")
    r = remind.collect([], now=BASE)
    check("★没有条目就什么都不念", r["stages"] == [] and r["due_count"] == 0, r)
    r = remind.collect(ITEMS, now=BASE.replace(hour=13))
    check("★没到窗口也不勉强念", r["stages"] == [] and r["count"] == 0, r["stages"])
    check("没到窗口也照样报出现在几点", r["now"].startswith("2026-11-06T13:00"), r["now"])
    weird = [{"id": "x-1", "title": "没有时间", "kind": "日程"},
             {"id": "x-2", "kind": "deadline"}]
    r = remind.collect(weird, now=BASE)
    check("没时间的条目不会炸", r["stages"] == [] or all(
        i["id"] not in ("x-1", "x-2") for s in r["stages"] for i in s["items"]), r)

    print("\n" + "=" * 50)
    if BAD:
        print(f"失败 {BAD} 项，通过 {OK} 项")
        return 1
    print(f"全部通过 ✓（{OK} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

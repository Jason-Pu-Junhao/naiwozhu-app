#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交我办导入 + 两种日期类型的验证。

不联网：这里用一份模拟的交我办返回结构，重点验证
  · 字段名容错（接口没有公开文档，字段名可能变）
  · 时间戳新旧判断（旧文件不许覆盖新数据）
  · 手动录入必须显式说明类型
运行：
    .venv/bin/python app/test_store.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import model
from store import Store
import calendar_import

OK, BAD = 0, 0


def check(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        BAD += 1
        print(f"  ✗ {name}  {extra}")


PAYLOAD = {
    "tool": "奶我助 · 交我办日历导出",
    "version": 1,
    "exported_at": "2026-10-08T14:23:11+08:00",
    "source": "https://calendar.sjtu.edu.cn/ui/calendar",
    "range": {"start": "2026-10-01", "end": "2027-01-06"},
    "calendars": {
        "my": [{"id": 11, "name": "我的日程"},
               {"id": 12, "name": "2026秋 课程表"}],
        "share": [{"id": 21, "name": "班级活动"}],
    },
    "events": [
        # 常规：startTime / endTime
        {"eventId": 1001, "calendarId": 11, "title": "小组会议",
         "startTime": "2026-10-09 14:00", "endTime": "2026-10-09 15:30",
         "location": "闵行校区", "description": "讨论分工", "allDay": False},
        # 秒级写法 + 课程类日历
        {"eventId": 1002, "calendarId": 12, "title": "高等数学",
         "startTime": "2026-10-09 08:00:00", "endTime": "2026-10-09 09:40:00",
         "location": "上院100", "allDay": False},
        # 换了字段名：start_time / all_day，而且是全天
        {"eventId": 1003, "calendarId": 21, "title": "班级团建",
         "start_time": "2026-10-11", "end_time": "2026-10-11", "all_day": True},
        # 没有时间字段，应该被跳过并记下来
        {"eventId": 1004, "calendarId": 11, "title": "没有时间的记录"},
    ],
    "keys_seen": ["allDay", "calendarId", "description", "endTime",
                  "eventId", "location", "startTime", "title"],
}


def main():
    tmp = tempfile.mkdtemp(prefix="naiwozhu-test-")
    try:
        st = Store(data_dir=tmp)

        print("解析交我办导出数据")
        items, stats = calendar_import.to_schedule_items(PAYLOAD)
        check("解析出 3 条日程（第 4 条没有时间）", len(items) == 3,
              f"实际 {len(items)}")
        check("没有时间的记录被记入 skipped", len(stats["skipped"]) == 1,
              str(stats["skipped"]))
        check("类型全部是「日程」",
              all(i["kind"] == model.KIND_SCHEDULE for i in items))
        check("来源标成「交我办」",
              all(i["source"] == model.SRC_CALENDAR for i in items))
        check("按时间顺序排列",
              [i["start_at"] for i in items] == sorted(i["start_at"] for i in items))

        by_id = {i["title"]: i for i in items}
        check("startTime/endTime 被认出来",
              by_id["小组会议"]["start_local"] == "10-09 14:00",
              by_id["小组会议"]["start_local"])
        check("地点被带过来", by_id["小组会议"]["location"] == "闵行校区")
        check("同一天的日程显示成 14:00–15:30",
              by_id["小组会议"]["when_local"] == "10-09 14:00–15:30",
              by_id["小组会议"]["when_local"])
        check("备用字段名 start_time 也被认出来",
              by_id["班级团建"]["start_local"].startswith("10-11"))
        check("全天日程显示成（全天）",
              by_id["班级团建"]["when_local"] == "10-11（全天）",
              by_id["班级团建"]["when_local"])
        check("日历名被接上", by_id["高等数学"]["calendar"] == "2026秋 课程表")
        check("课程类日历被打上 course_like 标记",
              by_id["高等数学"]["course_like"] is True
              and by_id["小组会议"]["course_like"] is False)

        print("\n时间戳新旧判断")
        r1 = st.import_payload(PAYLOAD, filename="sjtu-calendar-20261008-142311.json")
        check("第一次导入成功", r1["ok"] and not r1.get("stale"), str(r1))
        check("存下了 3 条", len(st.calendar_items()) == 3)
        check("时间戳来源是文件里的 exported_at",
              st.calendar_snapshot()["timestamp_origin"].startswith("导出文件"))

        older = dict(PAYLOAD, exported_at="2026-10-07T09:00:00+08:00",
                     events=PAYLOAD["events"][:1])
        r2 = st.import_payload(older, filename="sjtu-calendar-20261007-090000.json")
        check("更旧的文件被跳过", r2["ok"] and r2.get("stale") is True, str(r2))
        check("旧文件没有覆盖掉新数据", len(st.calendar_items()) == 3)

        r3 = st.import_payload(older, filename="old.json", force=True)
        check("force 可以强制收下旧文件",
              r3["ok"] and not r3.get("stale") and len(st.calendar_items()) == 1)

        newer = dict(PAYLOAD, exported_at="2026-10-09T08:00:00+08:00")
        r4 = st.import_payload(newer, filename="new.json")
        check("更新的文件正常覆盖", r4["ok"] and not r4.get("stale"))
        check("覆盖后条数正确", len(st.calendar_items()) == 3)

        print("\n缺时间戳时退用文件时间")
        no_ts = {k: v for k, v in PAYLOAD.items() if k != "exported_at"}
        r5 = st.import_payload(no_ts, filename="no-ts.json",
                               fallback_mtime=4102444800)   # 2100 年
        check("没有 exported_at 也能收下", r5["ok"] and not r5.get("stale"), str(r5))
        check("时间戳来源标注为文件时间",
              st.calendar_snapshot()["timestamp_origin"].startswith("文件时间"))

        print("\n收件箱")
        import json
        with open(os.path.join(st.inbox, "sjtu-calendar-20261231-235959.json"),
                  "w", encoding="utf-8") as f:
            json.dump(dict(PAYLOAD, exported_at="2026-12-31T23:59:59+08:00"), f)
        check("能列出收件箱文件", len(st.inbox_files()) == 1)
        r6 = st.scan_inbox()
        check("从收件箱导入成功", r6["ok"] and r6["count"] == 3, str(r6))

        print("\n手动录入必须显式说明类型")
        try:
            st.add_manual("", "没写类型")
            check("空类型被拒绝", False)
        except ValueError:
            check("空类型被拒绝", True)
        try:
            st.add_manual("作业", "类型写错了")
            check("非法类型被拒绝", False)
        except ValueError:
            check("非法类型被拒绝", True)
        try:
            st.add_manual(model.KIND_DEADLINE, "忘了写截止时间")
            check("deadline 缺时间被拒绝", False)
        except ValueError:
            check("deadline 缺时间被拒绝", True)
        try:
            st.add_manual(model.KIND_SCHEDULE, "忘了写开始时间")
            check("日程缺时间被拒绝", False)
        except ValueError:
            check("日程缺时间被拒绝", True)

        d = st.add_manual(model.KIND_DEADLINE, "高数第三章作业",
                          due="2026-10-20T23:59", note="高等数学")
        check("手动 deadline 记为 deadline", d["kind"] == model.KIND_DEADLINE)
        check("手动 deadline 有截止时间", d["due_local"] == "10-20 23:59",
              d["due_local"])
        check("手动 deadline 带课程名", d["course"] == "高等数学")
        check("手动条目 id 有前缀", d["id"].startswith("manual-"))

        s = st.add_manual(model.KIND_SCHEDULE, "导师组会",
                          start="2026-10-22T15:00", end="2026-10-22T16:00")
        check("手动日程记为「日程」", s["kind"] == model.KIND_SCHEDULE)
        check("手动日程显示起止", s["when_local"] == "10-22 15:00–16:00",
              s["when_local"])

        print("\n汇总与排序")
        allitems = st.all_items()
        check("三种来源合在一起", len(allitems) == 3 + 2, f"实际 {len(allitems)}")
        keys = [(i.get("due_at") or i.get("start_at")) for i in allitems]
        check("整体按时间顺序", keys == sorted(keys))
        only_dl = st.all_items(kind=model.KIND_DEADLINE)
        check("能按类型过滤（只剩 deadline）",
              len(only_dl) == 1 and only_dl[0]["kind"] == model.KIND_DEADLINE)
        only_sc = st.all_items(kind=model.KIND_SCHEDULE)
        check("能按类型过滤（只剩日程）", len(only_sc) == 4)

        print("\n删除手动条目")
        check("删得掉", st.delete_manual(d["id"])["ok"] is True)
        check("删完只剩 1 条", len(st.manual_items()) == 1)
        check("删不存在的报错", st.delete_manual("manual-999")["ok"] is False)

        print("\n坏文件")
        check("不是 JSON 对象会被拒",
              st.import_payload([1, 2, 3])["ok"] is False)
        check("没有 events 字段会被拒",
              st.import_payload({"hello": "world"})["ok"] is False)
        check("events 不是列表会被拒",
              st.import_payload({"events": "oops"})["ok"] is False)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 50)
    if BAD:
        print(f"失败 {BAD} 项，通过 {OK} 项")
        return 1
    print(f"全部通过 ✓（{OK} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

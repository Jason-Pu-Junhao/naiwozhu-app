#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交我办导入 + 两种日期类型的验证。

不联网：这里用一份模拟的交我办返回结构，重点验证
  · 字段名容错（接口没有公开文档，字段名可能变）
  · 时间戳新旧判断（旧文件不许覆盖新数据）
  · 手动录入必须显式说明类型
运行：
    .venv/bin/python app/test/test_store.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model
from store import Store
import calendar_import

import console        # noqa: E402

console.setup()

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

        # 两类各自多出来的信息：截止带课程名，日程带地点
        d2 = st.add_manual(model.KIND_DEADLINE, "线代第五章",
                           due="2026-10-21T23:59", course="线性代数")
        check("课程名可以用 course 传", d2["course"] == "线性代数", d2["course"])

        s2 = st.add_manual(model.KIND_SCHEDULE, "班会",
                           start="2026-10-23T18:00", end="2026-10-23T19:00",
                           location="东中院1-101")
        check("日程的地点写得进去", s2["location"] == "东中院1-101",
              s2["location"])

        print("\n汇总与排序")
        allitems = st.all_items()
        # 3 条来自交我办 + 4 条手动（d / d2 / s / s2）
        check("三种来源合在一起", len(allitems) == 3 + 4, f"实际 {len(allitems)}")
        keys = [(i.get("due_at") or i.get("start_at")) for i in allitems]
        check("整体按时间顺序", keys == sorted(keys))
        only_dl = st.all_items(kind=model.KIND_DEADLINE)
        check("能按类型过滤（只剩 deadline）",
              len(only_dl) == 2 and only_dl[0]["kind"] == model.KIND_DEADLINE)
        only_sc = st.all_items(kind=model.KIND_SCHEDULE)
        check("能按类型过滤（只剩日程）", len(only_sc) == 5)

        print("\n删除手动条目")
        check("删得掉", st.delete_manual(d["id"])["ok"] is True)
        check("删完只剩 3 条", len(st.manual_items()) == 3)
        check("删不存在的报错", st.delete_manual("manual-999")["ok"] is False)

        print("\n设备号与同步用的元信息")
        did = st.device_id()
        check("设备号是 8 位十六进制",
              len(did) == 8 and all(c in "0123456789abcdef" for c in did), did)
        check("设备号会落盘并复用", Store(data_dir=tmp).device_id() == did)
        check("手动条目 id 里带设备号", d2["id"] == f"manual-{did}-3", d2["id"])
        check("手动条目带 updated_at / rev / device_id / deleted",
              all(k in d2 for k in ("updated_at", "rev", "device_id", "deleted")),
              sorted(d2))

        print("\n派生字段不落盘、读的时候现算")
        raw = st._read(st.cal_path) or {}
        check("★磁盘上的日历条目没有 remaining",
              all("remaining" not in i for i in (raw.get("items") or [])))
        check("★读出来又有（按现在重算）",
              all(i.get("remaining") for i in st.calendar_items()))
        past = {
            "kind": model.KIND_DEADLINE, "id": f"manual-{did}-99", "title": "上周该交的",
            "source": model.SRC_MANUAL, "course": "", "html_url": "",
            "due_at": "2020-01-01T00:00:00+08:00", "sources": ["手动录入"],
            "updated_at": "2020-01-01T00:00:00+08:00", "rev": 1,
            "device_id": did, "deleted": False,
            # 故意写一个旧值，看读的时候会不会照抄
            "remaining": "还有 99 天", "overdue": False,
        }
        st.upsert_manual_records([past])
        got = [i for i in st.manual_items() if i["id"] == past["id"]][0]
        check("★存着旧值也会按现在重算", got["remaining"].startswith("已过期"),
              got["remaining"])
        check("★过期标记也跟着重算", got["overdue"] is True)

        print("\n删除留墓碑")
        graves = st.manual_items(include_deleted=True)
        grave = [i for i in graves if i["id"] == d["id"]][0]
        check("墓碑还在文件里", grave.get("deleted") is True)
        check("墓碑的 rev 比原来大", int(grave["rev"]) == 2, grave["rev"])
        check("★默认列表里看不到它",
              d["id"] not in [i["id"] for i in st.manual_items()])

        print("\n合并条目时逐条取新")
        newer = dict(past, updated_at="2027-01-01T00:00:00+08:00", title="新标题")
        older = dict(past, updated_at="2019-01-01T00:00:00+08:00", title="旧标题")
        st.upsert_manual_records([newer])
        st.upsert_manual_records([older])
        got = [i for i in st.manual_items() if i["id"] == past["id"]][0]
        check("★更旧的不会覆盖更新的", got["title"] == "新标题", got["title"])

        print("\nCanvas 缓存")
        cv_item = model.make_deadline("cv-1", "缓存里的作业",
                                      model.parse_dt("2026-12-01T23:59"),
                                      model.SRC_CANVAS, sources=["测试"])
        st.save_canvas("2026-10-20T10:00:00+08:00", [cv_item],
                       fetch_ok=True, origin_device=did)
        check("canvas.json 写下去了", st.canvas_meta()["count"] == 1)
        check("抓取时间记下来了",
              st.canvas_meta()["fetched_at"] == "2026-10-20T10:00:00+08:00")
        check("来源设备记下来了", st.canvas_meta()["origin_device"] == did)
        check("读出来带派生字段", bool(st.canvas_items()[0].get("remaining")))
        raw_cv = st._read(st.canvas_path) or {}
        check("★canvas 写盘也没有派生字段",
              all(k not in raw_cv["items"][0] for k in model.DERIVED))

        print("\n整份替换日历（同步用）")
        st.replace_calendar("2026-12-31T00:00:00+08:00", [
            {"kind": model.KIND_SCHEDULE, "id": "cal-sync-1", "title": "同步来的日程",
             "source": model.SRC_CALENDAR, "start_at": "2026-12-30T10:00:00+08:00",
             "end_at": "2026-12-30T11:00:00+08:00", "all_day": False},
        ], source_device="abcd1234")
        check("整份换掉了", len(st.calendar_items()) == 1)
        check("标注了来源设备", st.calendar_meta()["source_device"] == "abcd1234")
        check("换完还是能算出派生字段",
              bool(st.calendar_items()[0].get("when_local")))

        print("\n旧格式 id 会被迁移")
        legacy = Store(data_dir=os.path.join(tmp, "legacy"))
        legacy._write(legacy.manual_path, {"seq": 2, "items": [
            {"kind": model.KIND_DEADLINE, "id": "manual-1", "title": "老条目",
             "source": model.SRC_MANUAL, "due_at": "2026-12-01T23:59:00+08:00",
             "created_at": "2026-10-01T00:00:00+08:00"},
        ]})
        migrated = legacy.manual_items()
        check("★旧 id 被改成带设备号的",
              migrated[0]["id"].startswith(f"manual-{legacy.device_id()}-"),
              migrated[0]["id"])
        check("迁移后补上了同步用的字段",
              all(k in migrated[0] for k in ("device_id", "rev")), sorted(migrated[0]))

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

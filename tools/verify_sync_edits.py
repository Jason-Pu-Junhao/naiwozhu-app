#!/usr/bin/env python3
"""双向同步下的「删改合并」离线验证。

不联网、不碰真实数据：用两个临时数据目录模拟两台机器（A=电脑端、B=树莓派），
直接驱动 app/sync.py 的 build_bundle / handle_incoming / merge / apply_bundle，
模拟「一端主动推、对端合并落盘并回包、本端再合并落盘」这一完整往返。

覆盖的场景正是「有日程删改功能」时最容易出错的地方：
  改 / 删 / 恢复手动条目、隐藏 / 恢复交我办日程、隐藏之后又导入新日历、
  两端同时改同一条、一端改另一端删、幂等与可交换、id 不撞车、
  快照替换后清理孤儿隐藏记录时的边界，以及对端时钟偏慢时的恢复回归。

用法：  python3 tools/verify_sync_edits.py
退出码：0 = 全部通过；1 = 有失败项。
"""
import os
import sys
import json
import shutil
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import store as store_mod          # noqa: E402
import sync as sync_mod            # noqa: E402
import model                       # noqa: E402

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok)))
    print(("  ✅" if ok else "  ❌"), name,
          ("| " + str(detail)[:220] if (detail and not ok) else ""))


def section(title):
    print("\n" + title)


# ---------------------------------------------------------------- 同步模拟
def push(a, b):
    """a 主动推给 b：b 合并落盘并回包，a 再合并落盘（等价于 run_once 的一次往返）。"""
    bundle_a = sync_mod.build_bundle(a)
    merged_b, _ = sync_mod.handle_incoming(b, bundle_a)
    merged_a, _ = sync_mod.merge(sync_mod.build_bundle(a), merged_b)
    sync_mod.apply_bundle(a, merged_a)


def sync_both(a, b, rounds=1):
    for _ in range(rounds):
        push(a, b)
        push(b, a)


# ---------------------------------------------------------------- 构造数据
def cal_event(eid, title, start, end=None, location=""):
    return model.make_schedule(
        "cal-%s" % eid, title, model.parse_dt(start),
        model.parse_dt(end) if end else None,
        model.SRC_CALENDAR, location=location)


def base_calendar(st, exported_at, with_extra=False):
    items = [
        cal_event(1001, "高等数学", "2026-11-20 08:00", "2026-11-20 09:40", "上院100"),
        cal_event(1002, "小组会议", "2026-11-21 14:00", "2026-11-21 15:30", "闵行校区"),
    ]
    if with_extra:
        items.append(cal_event(1003, "班级团建", "2026-11-22 09:00",
                               "2026-11-22 12:00", "集合点"))
    st.replace_calendar(exported_at, items)


def add_schedule(st, title, start="2026-12-01 14:00", end="2026-12-01 15:30"):
    return st.add_manual("日程", title, start=start, end=end, location="测试地点")


def facts(st):
    """只取事实层，用于两端一致性比较（派生字段与 imported_at 之类的元信息不算）。"""
    man = {str(i["id"]): (i.get("title"), bool(i.get("deleted")),
                          i.get("rev"), i.get("updated_at"), i.get("location"))
           for i in st.manual_items(include_deleted=True)}
    hid = {str(i["id"]): (bool(i.get("deleted")), i.get("rev"), i.get("updated_at"))
           for i in st.hide_items(include_deleted=True)}
    cal = st.calendar_snapshot()
    return {
        "manual": man,
        "hides": hid,
        "cal_ids": sorted(str(i["id"]) for i in (cal.get("items") or [])),
        "cal_exported": cal.get("exported_at"),
    }


def visible_manual_titles(st):
    return sorted(i["title"] for i in st.manual_items())


def set_manual_time(st, item_id, updated_at):
    """把某条的 updated_at 改成指定值，用来精确模拟「谁在什么时间改的」。"""
    doc = st._read(st.manual_path)
    for it in doc["items"]:
        if str(it.get("id")) == str(item_id):
            it["updated_at"] = updated_at
    st._write(st.manual_path, doc)


def iso(dt):
    return dt.astimezone(model.CST).isoformat()


T0 = datetime(2026, 11, 14, 20, 0, 0, tzinfo=model.CST)


def main():
    tmp = tempfile.mkdtemp(prefix="naiwozhu-sync-edits-")
    try:
        # ============================================================ S1 改
        section("S1 手动条目「改」双向合并")
        a = store_mod.Store(data_dir=os.path.join(tmp, "s1a"))
        b = store_mod.Store(data_dir=os.path.join(tmp, "s1b"))
        mid = add_schedule(a, "原始标题")["id"]
        sync_both(a, b)
        check("初始同步后两端都有这条", visible_manual_titles(a) == visible_manual_titles(b)
              == ["原始标题"], facts(b)["manual"])
        a.update_manual(mid, title="电脑端改过的标题")
        push(a, b)
        check("A 改标题 → B 也改", visible_manual_titles(b) == ["电脑端改过的标题"],
              facts(b)["manual"])
        b.update_manual(mid, location="树莓派改的地点")
        push(b, a)
        check("B 改地点 → A 也改",
              facts(a)["manual"][mid][4] == "树莓派改的地点", facts(a)["manual"])
        check("两端事实层一致", facts(a) == facts(b))

        # ============================================================ S2 删
        section("S2 手动条目「删」：墓碑传播且不复活")
        a = store_mod.Store(data_dir=os.path.join(tmp, "s2a"))
        b = store_mod.Store(data_dir=os.path.join(tmp, "s2b"))
        mid = add_schedule(a, "要删的条目")["id"]
        sync_both(a, b)
        a.delete_manual(mid)
        push(a, b)
        check("A 删 → B 也删（列表里看不到）", visible_manual_titles(b) == [],
              facts(b)["manual"])
        check("B 上留着墓碑而不是物理删除",
              facts(b)["manual"].get(mid, (None, False))[1] is True, facts(b)["manual"])
        try:
            b.update_manual(mid, title="删了还想改")
            check("★B 上删掉之后再改应报错", False, "没抛异常")
        except Exception as e:
            check("★B 上删掉之后再改应报错", getattr(e, "status", None) == 404, str(e))
        push(b, a)
        check("反向同步不会把它复活", visible_manual_titles(a) == [], facts(a)["manual"])

        # ============================================================ S3 恢复
        section("S3 手动条目「恢复」：rev 递增，能赢过对端的墓碑")
        b.restore_manual(mid)
        push(b, a)
        check("B 恢复 → A 也恢复", visible_manual_titles(a) == ["要删的条目"],
              facts(a)["manual"])
        check("恢复后两端一致", facts(a) == facts(b))

        # ============================================================ S4 隐藏/恢复
        section("S4 交我办日程「隐藏 / 恢复」双向合并")
        a = store_mod.Store(data_dir=os.path.join(tmp, "s4a"))
        b = store_mod.Store(data_dir=os.path.join(tmp, "s4b"))
        base_calendar(a, "2026-11-14T10:00:00+08:00")
        sync_both(a, b)
        check("日历快照同步到 B", facts(b)["cal_ids"] == ["cal-1001", "cal-1002"],
              facts(b)["cal_ids"])
        a.set_hidden("cal-1001")
        push(a, b)
        check("A 隐藏 cal-1001 → B 也隐藏", b.hidden_ids() == {"cal-1001"}, b.hidden_ids())
        check("B 的可见日历条目只剩 cal-1002",
              [i["id"] for i in b.effective_items()] == ["cal-1002"],
              [i["id"] for i in b.effective_items()])
        b.clear_hidden("cal-1001")
        push(b, a)
        check("B 恢复 → A 也恢复", a.hidden_ids() == set(), a.hidden_ids())
        check("恢复记录本身也同步过去（deleted=False 仍在 hides 里）",
              facts(a)["hides"].get("cal-1001", (None,))[0] is False, facts(a)["hides"])

        # ============================================================ S5 隐藏后导入新日历
        section("S5 隐藏之后，对端导入新日历：隐藏状态不能丢")
        a = store_mod.Store(data_dir=os.path.join(tmp, "s5a"))
        b = store_mod.Store(data_dir=os.path.join(tmp, "s5b"))
        base_calendar(a, "2026-11-14T10:00:00+08:00")
        sync_both(a, b)
        a.set_hidden("cal-1001")
        push(a, b)
        # B 导入一份更新的日历：cal-1001 还在，新增 cal-1003
        base_calendar(b, "2026-11-14T12:00:00+08:00", with_extra=True)
        sync_both(a, b)
        check("两端日历都换成新快照（含 cal-1003）",
              facts(a)["cal_ids"] == facts(b)["cal_ids"] == ["cal-1001", "cal-1002", "cal-1003"],
              (facts(a)["cal_ids"], facts(b)["cal_ids"]))
        check("新 exported_at 在两端一致",
              facts(a)["cal_exported"] == facts(b)["cal_exported"] == "2026-11-14T12:00:00+08:00")
        check("★cal-1001 在两端仍然是隐藏状态",
              a.hidden_ids() == b.hidden_ids() == {"cal-1001"},
              (a.hidden_ids(), b.hidden_ids()))

        # ============================================================ S6 并发改同一条
        section("S6 两端同时改同一条：rev 相同再按 updated_at 裁决，后写赢")
        a = store_mod.Store(data_dir=os.path.join(tmp, "s6a"))
        b = store_mod.Store(data_dir=os.path.join(tmp, "s6b"))
        mid = add_schedule(a, "并发修改")["id"]
        sync_both(a, b)
        a.update_manual(mid, title="A 在 20:00 改的")
        set_manual_time(a, mid, iso(T0))
        b.update_manual(mid, title="B 在 20:05 改的")
        set_manual_time(b, mid, iso(T0 + timedelta(minutes=5)))
        sync_both(a, b)
        check("后写的（B 20:05）在两端都赢",
              visible_manual_titles(a) == visible_manual_titles(b) == ["B 在 20:05 改的"],
              (visible_manual_titles(a), visible_manual_titles(b)))
        a.update_manual(mid, title="A 在 20:10 又改")
        set_manual_time(a, mid, iso(T0 + timedelta(minutes=10)))
        sync_both(a, b)
        check("再后写的（A 20:10）又在两端都赢",
              visible_manual_titles(a) == visible_manual_titles(b) == ["A 在 20:10 又改"])

        # ============================================================ S7 一端改、另一端删
        section("S7 一端改、另一端删：删除不复活，恢复能再生")
        a = store_mod.Store(data_dir=os.path.join(tmp, "s7a"))
        b = store_mod.Store(data_dir=os.path.join(tmp, "s7b"))
        mid = add_schedule(a, "改删冲突")["id"]
        sync_both(a, b)
        a.update_manual(mid, title="A 在 20:00 改的")
        set_manual_time(a, mid, iso(T0))
        b.delete_manual(mid)
        set_manual_time(b, mid, iso(T0 + timedelta(minutes=5)))
        sync_both(a, b)
        check("删除（更晚）赢，两端都看不到",
              visible_manual_titles(a) == visible_manual_titles(b) == [],
              (visible_manual_titles(a), visible_manual_titles(b)))
        a.restore_manual(mid)
        set_manual_time(a, mid, iso(T0 + timedelta(minutes=10)))
        sync_both(a, b)
        check("再恢复（更晚）赢，两端都回来且一致",
              len(visible_manual_titles(a)) == 1
              and visible_manual_titles(a) == visible_manual_titles(b)
              and facts(a) == facts(b),
              (visible_manual_titles(a), visible_manual_titles(b)))
        # 这里暴露的是**设计语义**而不是 bug：合并是「记录级」最后写入获胜，
        # 不是字段级合并。B 删除时还没收到 A 的标题修改，所以整条记录以 B 那份为准，
        # 标题随删除一起回到 B 当时的值；只要修改先同步过，就不会这样（见 S7b）。
        check("冲突采用记录级最后写入获胜：恢复后展示的是删除方当时那份",
              visible_manual_titles(a) == ["改删冲突"], visible_manual_titles(a))

        # ============================================================ S7b 常见路径
        section("S7b 常见路径：修改先同步、再删除，恢复后标题保持新值")
        a = store_mod.Store(data_dir=os.path.join(tmp, "s7ba"))
        b = store_mod.Store(data_dir=os.path.join(tmp, "s7bb"))
        mid = add_schedule(a, "原始标题")["id"]
        sync_both(a, b)
        a.update_manual(mid, title="改好的标题")
        sync_both(a, b)                     # 修改先同步到对端
        b.delete_manual(mid)
        sync_both(a, b)
        a.restore_manual(mid)
        sync_both(a, b)
        check("恢复后两端标题都是改好的新值（不会无故回退）",
              visible_manual_titles(a) == visible_manual_titles(b) == ["改好的标题"],
              (visible_manual_titles(a), visible_manual_titles(b)))

        # ============================================================ S8 幂等可交换
        section("S8 幂等 / 可交换：反复双向同步结果不变")
        before_a, before_b = facts(a), facts(b)
        sync_both(a, b, rounds=3)
        check("再同步 3 轮，两端数据不变",
              facts(a) == before_a and facts(b) == before_b)
        check("两端完全一致", facts(a) == facts(b))

        # ============================================================ S9 id 不撞车
        section("S9 两端各自新增手动条目：id 靠 device_id 区分，不互相吞掉")
        a = store_mod.Store(data_dir=os.path.join(tmp, "s9a"))
        b = store_mod.Store(data_dir=os.path.join(tmp, "s9b"))
        ia = add_schedule(a, "电脑端新增的")
        ib = add_schedule(b, "树莓派新增的")
        check("两端各自的第一条 id 不同", ia["id"] != ib["id"], (ia["id"], ib["id"]))
        sync_both(a, b)
        check("两条在两端都在",
              visible_manual_titles(a) == visible_manual_titles(b)
              == ["树莓派新增的", "电脑端新增的"],
              (visible_manual_titles(a), visible_manual_titles(b)))

        # ============================================================ S10 日历与手动互不覆盖
        section("S10 一端改手动、另一端更新日历：两类数据各走各的，互不覆盖")
        a = store_mod.Store(data_dir=os.path.join(tmp, "s10a"))
        b = store_mod.Store(data_dir=os.path.join(tmp, "s10b"))
        base_calendar(a, "2026-11-14T10:00:00+08:00")
        mid = add_schedule(a, "手动的那条")["id"]
        sync_both(a, b)
        a.update_manual(mid, title="手动的那条（改过）")
        base_calendar(b, "2026-11-14T13:00:00+08:00", with_extra=True)
        sync_both(a, b)
        check("手动改动保留", visible_manual_titles(a) == visible_manual_titles(b)
              == ["手动的那条（改过）"])
        check("日历更新也生效",
              facts(a)["cal_ids"] == facts(b)["cal_ids"] == ["cal-1001", "cal-1002", "cal-1003"])

        # ============================================================ S11 清理孤儿隐藏的边界
        section("S11 快照替换后清理孤儿隐藏：有效隐藏不误删、太新的孤儿不急着删")
        a = store_mod.Store(data_dir=os.path.join(tmp, "s11a"))
        base_calendar(a, "2026-11-14T10:00:00+08:00")
        a.set_hidden("cal-1001")
        alive = [i["id"] for i in a.calendar_snapshot().get("items") or []]
        dropped = a.prune_hides(alive)
        check("条目还在日历里，隐藏记录不会被清",
              dropped == [] and a.hidden_ids() == {"cal-1001"}, dropped)
        a.set_hidden("cal-ghost")            # 日历里根本没有这条
        dropped = a.prune_hides(alive)
        check("刚产生的孤儿不急着清（keep_days 保护，避免对端还没收到就复活）",
              "cal-ghost" not in dropped, dropped)
        doc = a._read(a.hides_path)          # 把孤儿伪装成 40 天前的
        doc["items"]["cal-ghost"]["deleted_at"] = iso(T0 - timedelta(days=40))
        doc["items"]["cal-ghost"]["updated_at"] = iso(T0 - timedelta(days=40))
        a._write(a.hides_path, doc)
        dropped = a.prune_hides(alive)
        check("放够时间的孤儿才会被清掉", "cal-ghost" in dropped, dropped)

        # ============================================================ S12 时钟偏差回归
        section("S12 对端时钟偏慢：后续恢复必须靠 rev 赢过旧删除，不看本机墙钟")
        a = store_mod.Store(data_dir=os.path.join(tmp, "s12a"))
        b = store_mod.Store(data_dir=os.path.join(tmp, "s12b"))
        mid = add_schedule(a, "时钟偏差恢复")["id"]
        sync_both(a, b)
        a.delete_manual(mid)
        set_manual_time(a, mid, iso(T0 + timedelta(minutes=5)))
        push(a, b)
        b.restore_manual(mid)
        set_manual_time(b, mid, iso(T0))  # B 的动作更晚发生，但墙钟时间更早
        push(b, a)
        check("恢复 rev=3 赢过删除 rev=2，即使恢复时间戳更早",
              visible_manual_titles(a) == visible_manual_titles(b) == ["时钟偏差恢复"],
              (facts(a)["manual"], facts(b)["manual"]))
        check("时钟偏差下恢复后两端事实层一致", facts(a) == facts(b))

        base_calendar(a, "2026-11-14T10:00:00+08:00")
        sync_both(a, b)
        a.set_hidden("cal-1001")
        doc = a._read(a.hides_path)
        doc["items"]["cal-1001"]["updated_at"] = iso(T0 + timedelta(minutes=5))
        a._write(a.hides_path, doc)
        push(a, b)
        b.clear_hidden("cal-1001")
        doc = b._read(b.hides_path)
        doc["items"]["cal-1001"]["updated_at"] = iso(T0)
        b._write(b.hides_path, doc)
        push(b, a)
        check("日历恢复同样不受对端时钟偏慢影响",
              a.hidden_ids() == b.hidden_ids() == set(),
              (facts(a)["hides"], facts(b)["hides"]))

        # ============================================================ 汇总
        passed = sum(1 for _, ok in CHECKS if ok)
        total = len(CHECKS)
        print("\n" + "=" * 56)
        print("删改合并离线验证：%d / %d 项通过" % (passed, total))
        if passed != total:
            print("失败项：")
            for name, ok in CHECKS:
                if not ok:
                    print("  -", name)
        print("=" * 56)
        return 0 if passed == total else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

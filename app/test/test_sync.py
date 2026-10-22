#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两台机器之间同步的验证。

不联网：合并逻辑（build_bundle / merge / apply_bundle）是纯函数，
这里直接拿两台临时 Store 对撞；HTTP 那一层用假响应测错误映射。

重点验证四件事：
  · 合并是幂等、可交换的（所以一次往返就收敛，重发多少次都一样）
  · 三类数据各按各的规则取新（日历看 exported_at，Canvas 看抓成功没，手动条目逐条）
  · 删除留墓碑，不会被对方那份复活
  · 同步包里没有令牌、没有口令、没有本机路径，也没有会过期的派生字段

运行：
    .venv/bin/python app/test/test_sync.py
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model
import sync
from store import Store

import console        # noqa: E402

console.setup()

OK, BAD = 0, 0
DIRS = []


def check(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        BAD += 1
        print(f"  ✗ {name}  {extra}")


def new_store(prefix="nwz-sync-"):
    d = tempfile.mkdtemp(prefix=prefix)
    DIRS.append(d)
    return Store(data_dir=d)


def iso(dt):
    return dt.astimezone(model.CST).isoformat()


def deadline(item_id, title, days, source=model.SRC_CANVAS):
    return model.make_deadline(item_id, title,
                               datetime.now(timezone.utc) + timedelta(days=days),
                               source, sources=["测试"])


def calendar_payload(exported_at, titles):
    now = datetime.now(model.CST)
    f = lambda d: d.strftime("%Y-%m-%d %H:%M")            # noqa: E731
    return {
        "exported_at": exported_at,
        "calendars": {"my": [{"id": 11, "name": "我的日程"}]},
        "events": [{"eventId": 1000 + i, "calendarId": 11, "title": t,
                    "startTime": f(now + timedelta(days=2, hours=i)),
                    "endTime": f(now + timedelta(days=2, hours=i + 1))}
                   for i, t in enumerate(titles)],
    }


def data_of(bundle):
    """只比数据部分：信封里的 device_id/generated_at 不参与合并的身份。"""
    return json.dumps({k: bundle.get(k) for k in ("calendar", "canvas", "manual")},
                      sort_keys=True, ensure_ascii=False)


def main():
    A, B = new_store("nwz-sync-a-"), new_store("nwz-sync-b-")

    print("设备号与条目 id")
    did_a, did_b = A.device_id(), B.device_id()
    check("两台设备号不一样", did_a != did_b, f"{did_a} / {did_b}")
    check("设备号是 8 位十六进制", len(did_a) == 8 and all(c in "0123456789abcdef" for c in did_a),
          did_a)
    check("设备号会落盘并复用", A.device_id() == did_a and Store(data_dir=A.dir).device_id() == did_a)

    ia = A.add_manual(model.KIND_SCHEDULE, "A 的会",
                      start="2026-11-01T10:00", end="2026-11-01T11:00")
    ib = B.add_manual(model.KIND_SCHEDULE, "B 的会",
                      start="2026-11-02T10:00", end="2026-11-02T11:00")
    check("A 的 id 带 A 的设备号", ia["id"] == f"manual-{did_a}-1", ia["id"])
    check("B 的 id 带 B 的设备号", ib["id"] == f"manual-{did_b}-1", ib["id"])
    check("★两端各自的第 1 条不会撞 id", ia["id"] != ib["id"])
    check("条目带 updated_at / rev / device_id / deleted",
          all(k in ia for k in ("updated_at", "rev", "device_id", "deleted")),
          sorted(ia))

    print("\n同步包的内容")
    bundle = sync.build_bundle(A)
    check("包里有版本号", bundle.get("schema") == sync.SCHEMA)
    check("包里有设备号", bundle.get("device_id") == did_a)
    blob = json.dumps(bundle, ensure_ascii=False)
    check("★包里没有令牌", "canvas_token" not in blob and "token" not in blob)
    check("★包里没有访问口令", "access_key" not in blob)
    check("★包里没有本机文件路径", A.dir not in blob and "/tmp/" not in blob)
    check("★包里没有会过期的派生字段",
          all(k not in json.dumps(bundle["manual"], ensure_ascii=False)
              for k in model.DERIVED), list(model.DERIVED))

    print("\n手动条目：逐条取新")
    la, lb = sync.build_bundle(A), sync.build_bundle(B)
    merged, changes = sync.merge(la, lb)
    ids = {r["id"] for r in merged["manual"]}
    check("合并后两条都在", ids == {ia["id"], ib["id"]}, ids)
    check("变化说明记了新增", changes["manual"]["added"] == 1, changes["manual"])

    again, ch2 = sync.merge(la, lb)
    check("★同一个包再合一次，结果一模一样", data_of(again) == data_of(merged))
    comp, _ = sync.merge(lb, la)
    check("★换一边合并，数据部分相同（可交换）", data_of(comp) == data_of(merged))

    # 改一条再合
    A.add_manual(model.KIND_DEADLINE, "A 的作业", due="2026-12-01T23:59")
    la2 = sync.build_bundle(A)
    m2, ch = sync.merge(la, la2)          # la 里没有这条新条目
    check("新条目能被合进来", ch["manual"]["added"] == 1, ch["manual"])

    # 手工构造"对端更晚改过同一条"
    newer = [dict(r) for r in la2["manual"]]
    for r in newer:
        if r["id"] == ia["id"]:
            r["title"] = "A 的会（改过）"
            r["updated_at"] = iso(datetime.now(timezone.utc) + timedelta(minutes=5))
            r["rev"] = 2
    m3, ch3 = sync.merge(la2, dict(la2, manual=newer))
    got = [r for r in m3["manual"] if r["id"] == ia["id"]][0]
    check("更晚改过的那条赢", got["title"] == "A 的会（改过）", got.get("title"))
    check("并且记成一次更新", ch3["manual"]["updated"] == 1, ch3["manual"])

    base = dict(la2, manual=[r for r in la2["manual"] if r["id"] == ia["id"]])
    tie_old = dict(base["manual"][0], device_id="00000000", rev=1)
    tie_new = dict(base["manual"][0], device_id="ffffffff", rev=1)
    m4, _ = sync.merge(dict(base, manual=[tie_old]), dict(base, manual=[tie_new]))
    check("★时间戳与 rev 都相同时按设备号定胜负（结果确定）",
          m4["manual"][0]["device_id"] == "ffffffff", m4["manual"][0].get("device_id"))

    print("\n删除：墓碑不会被复活")
    # 先让 A 也拿到这条（模拟之前同步过一次），"删除计数"才有意义
    A.upsert_manual_records(sync.build_bundle(B)["manual"])
    check("A 也有 B 那条了", ib["id"] in [i["id"] for i in A.manual_items()])
    tomb = B.delete_manual(ib["id"])
    check("删得掉", tomb.get("ok") is True, tomb)
    check("删完本机就看不见了", [i["id"] for i in B.manual_items()] == [] or
          ib["id"] not in [i["id"] for i in B.manual_items()])
    check("墓碑还留在文件里", any(i["id"] == ib["id"] and i.get("deleted")
                                  for i in B.manual_items(include_deleted=True)))
    check("墓碑的 rev 变大了", [i for i in B.manual_items(include_deleted=True)
                               if i["id"] == ib["id"]][0]["rev"] == 2)

    mb, chb = sync.merge(sync.build_bundle(A), sync.build_bundle(B))
    check("合并后墓碑赢（对方那份旧数据不复活）",
          [r for r in mb["manual"] if r["id"] == ib["id"]][0].get("deleted") is True)
    check("变化说明记了一次删除", chb["manual"]["deleted"] == 1, chb["manual"])

    A.upsert_manual_records(mb["manual"])
    check("★把合并结果写回 A，B 删掉的那条也没了",
          ib["id"] not in [i["id"] for i in A.manual_items()])
    check("A 里墓碑也在（下次同步还能带上）",
          any(i["id"] == ib["id"] and i.get("deleted")
              for i in A.manual_items(include_deleted=True)))

    print("\n交我办快照：整份取新")
    A.import_payload(calendar_payload("2026-10-10T10:00:00+08:00", ["旧日程"]),
                     filename="old.json", force=True)
    B.replace_calendar("2026-10-12T10:00:00+08:00", [
        {"kind": model.KIND_SCHEDULE, "id": "cal-1", "title": "对方的新日程",
         "source": model.SRC_CALENDAR, "start_at": "2026-11-05T10:00:00+08:00",
         "end_at": "2026-11-05T11:00:00+08:00", "all_day": False},
    ], source_device=did_b)

    mc, chc = sync.merge(sync.build_bundle(A), sync.build_bundle(B))
    check("更晚导出的那份赢", chc["calendar"] == "replaced", chc)
    check("赢的那份条数对", len(mc["calendar"]["items"]) == 1)
    check("★快照里没有派生字段",
          all(k not in mc["calendar"]["items"][0] for k in model.DERIVED))

    same_time = dict(sync.build_bundle(B), calendar=dict(mc["calendar"]))
    m5, ch5 = sync.merge(mc, same_time)
    check("导出时间相同时保留本机的（幂等）", ch5["calendar"] == "kept", ch5["calendar"])

    m6, ch6 = sync.merge({"schema": sync.SCHEMA, "device_id": did_a,
                          "calendar": None, "canvas": None, "manual": []}, mc)
    check("本地没有快照时直接收下", ch6["calendar"] == "replaced", ch6["calendar"])
    m7, ch7 = sync.merge(mc, {"schema": sync.SCHEMA, "device_id": did_b,
                              "calendar": None, "canvas": None, "manual": []})
    check("对端没有快照时保留本机的", ch7["calendar"] == "kept", ch7["calendar"])

    print("\nCanvas 缓存：抓成功的优先")
    ok_items = [deadline("cv-1", "抓到的作业", 3)]
    bad_items = []
    A.save_canvas(datetime.now(timezone.utc) - timedelta(minutes=30), ok_items,
                  fetch_ok=True, origin_device=did_a)
    B.save_canvas(datetime.now(timezone.utc), bad_items, fetch_ok=False,
                  origin_device=did_b)
    mv, chv = sync.merge(sync.build_bundle(A), sync.build_bundle(B))
    check("★新的失败抓取不覆盖旧的成果", chv["canvas"] == "kept", chv["canvas"])
    check("保住的是抓到东西的那份", len(mv["canvas"]["items"]) == 1)
    check("那份写着抓取成功", mv["canvas"]["fetch_ok"] is True)

    C = new_store("nwz-sync-c-")
    m8, ch8 = sync.merge(sync.build_bundle(C), sync.build_bundle(B))
    check("本机什么都没有时，失败的那份也收下（好过没有）",
          ch8["canvas"] == "replaced" and m8["canvas"]["fetch_ok"] is False, ch8)

    B.save_canvas(datetime.now(timezone.utc) + timedelta(seconds=1), ok_items,
                  fetch_ok=True, origin_device=did_b)
    m9, ch9 = sync.merge(sync.build_bundle(A), sync.build_bundle(B))
    check("抓成功且更新的那份赢", ch9["canvas"] == "replaced", ch9["canvas"])
    check("来源设备记的是对端", m9["canvas"]["origin_device"] == did_b,
          m9["canvas"].get("origin_device"))

    print("\n落地：apply_bundle")
    D = new_store("nwz-sync-d-")
    wrote = sync.apply_bundle(D, sync.build_bundle(A))
    check("第一次落地写了日历", wrote["calendar"] is True, wrote)
    check("落地后日历条数对", len(D.calendar_items()) == 1)
    check("落地后能按现在重算徽章",
          D.calendar_items()[0].get("remaining") not in (None, ""),
          D.calendar_items()[0].get("remaining"))
    wrote2 = sync.apply_bundle(D, sync.build_bundle(A))
    check("★同样的包再落一次，一点都没写（幂等）",
          wrote2["calendar"] is False and wrote2["manual"] == 0, wrote2)

    print("\n一次往返")
    E, F = new_store("nwz-sync-e-"), new_store("nwz-sync-f-")
    E.add_manual(model.KIND_SCHEDULE, "E 的会", start="2026-11-11T09:00",
                 end="2026-11-11T10:00")
    F.add_manual(model.KIND_SCHEDULE, "F 的会", start="2026-11-12T09:00",
                 end="2026-11-12T10:00")
    merged_ef, _ = sync.handle_incoming(F, sync.build_bundle(E))
    sync.apply_bundle(E, merged_ef)
    check("★E 推给 F 之后，F 拿到并集",
          {i["title"] for i in F.manual_items()} == {"E 的会", "F 的会"},
          [i["title"] for i in F.manual_items()])
    check("★回包之后 E 也是并集",
          {i["title"] for i in E.manual_items()} == {"E 的会", "F 的会"},
          [i["title"] for i in E.manual_items()])
    check("两边最终一模一样",
          {i["id"] for i in E.manual_items()} == {i["id"] for i in F.manual_items()})

    print("\n包的版本与坏包")
    for bad, why in (({}, "空包"), ({"schema": 99}, "版本不认识"),
                     ({"schema": sync.SCHEMA, "manual": "oops"}, "manual 不是列表")):
        try:
            sync.validate(bad)
            check(f"拒绝{why}", False)
        except sync.SyncError:
            check(f"拒绝{why}", True)

    print("\n时钟")
    far = dict(sync.build_bundle(F),
               generated_at=iso(datetime.now(timezone.utc) + timedelta(hours=2)))
    skew = sync.clock_skew_seconds(far)
    check("能算出对端快了两个小时", skew is not None and skew > 6900, skew)
    before_stamps = {i["id"]: i["updated_at"]
                     for i in E.manual_items(include_deleted=True)}
    m10, _ = sync.merge(sync.build_bundle(E), far)
    sync.apply_bundle(E, m10)
    after_stamps = {i["id"]: i["updated_at"]
                    for i in E.manual_items(include_deleted=True)}
    check("★时钟差只提示，不把时间戳钳到本机时间", before_stamps == after_stamps,
          f"{before_stamps} → {after_stamps}")
    check("★时间戳原样保留（下次同步还认得出谁更新）",
          all(i.get("updated_at") for i in E.manual_items(include_deleted=True)))

    print("\n网络那一层（假响应）")
    real_post = sync.requests.post

    class FakeResp:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body
            self.text = json.dumps(body, ensure_ascii=False) if body is not None else ""

        def json(self):
            if self._body is None:
                raise ValueError("not json")
            return self._body

    try:
        sync.requests.post = lambda *a, **k: FakeResp(200, {
            "ok": True, "bundle": sync.build_bundle(F)})
        res = sync.run_once(E, "http://127.0.0.1:1", "k")
        check("run_once 能把对端的数据拿回来", res["peer_device"] == F.device_id(),
              res.get("peer_device"))

        sync.requests.post = lambda *a, **k: FakeResp(401, {"ok": False})
        try:
            sync.run_once(E, "http://127.0.0.1:1", "k")
            check("401 会报错", False)
        except sync.SyncError as e:
            check("401 提示口令不对", "口令" in str(e), str(e))

        sync.requests.post = lambda *a, **k: FakeResp(403, {"ok": False})
        try:
            sync.run_once(E, "http://127.0.0.1:1", "k")
            check("403 会报错", False)
        except sync.SyncError as e:
            check("403 提示对端没设口令", "口令" in str(e), str(e))

        sync.requests.post = lambda *a, **k: FakeResp(200, None)
        try:
            sync.run_once(E, "http://127.0.0.1:1", "k")
            check("非 JSON 会报错", False)
        except sync.SyncError as e:
            check("非 JSON 说清楚", "JSON" in str(e), str(e))

        def boom(*a, **k):
            raise sync.requests.RequestException("connection refused")
        sync.requests.post = boom
        try:
            sync.run_once(E, "http://127.0.0.1:1", "k")
            check("连不上会报错", False)
        except sync.SyncError as e:
            check("连不上说清楚是连不上", "连不上" in str(e), str(e))
    finally:
        sync.requests.post = real_post

    print("\n版本不一致时明确拒绝")
    try:
        sync.handle_incoming(E, {"schema": 999, "device_id": "x", "manual": []})
        check("handle_incoming 拒绝不认识的版本", False)
    except sync.SyncError:
        check("handle_incoming 拒绝不认识的版本", True)

    print("\n收尾")
    for d in DIRS:
        shutil.rmtree(d, ignore_errors=True)

    print("\n" + "=" * 50)
    if BAD:
        print(f"失败 {BAD} 项，通过 {OK} 项")
        return 1
    print(f"全部通过 ✓（{OK} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

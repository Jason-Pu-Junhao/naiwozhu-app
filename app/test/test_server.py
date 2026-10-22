#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页服务层（server.py）的验证。

前面三套测试都绕开了 server.py：查询解析、取数、存储各自测得挺细，
但"网页点下去到底返回什么"一直没人管。这一套补上这一层。

不联网：Canvas 客户端和取数服务都被换成假的，
重点是验证
  · 接口的形状（字段齐不齐、状态码对不对）
  · 演示模式真的**一次网络都不打**
  · Canvas 缓存确实在省流量，refresh 确实能穿透它
  · 手动录入的课程名 / 地点真的写进去了

数据目录用 NAIWOZHU_DATA_DIR 指到临时目录，
所以这套测试**不会碰到你真实的日历和手动记录**。

运行：
    .venv/bin/python app/test/test_server.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))          # app/test/
APP = os.path.dirname(HERE)                                # app/
sys.path.insert(0, APP)

# 必须在 import server 之前设：server 一被导入就会建 Store()
_TMP = tempfile.mkdtemp(prefix="naiwozhu-test-")
os.environ["NAIWOZHU_DATA_DIR"] = _TMP
os.environ["CANVAS_TOKEN"] = "fake-token-for-test"

import model                                   # noqa: E402
from model import CST, make_deadline            # noqa: E402
import query as query_mod                       # noqa: E402
import server                                  # noqa: E402

import console        # noqa: E402

console.setup()

# /api/token 会往 app/config.local.json 里写字。
# 那里面是你真实的 Canvas 令牌，测试**绝对不能**碰，
# 所以把配置文件的位置也一起指到临时目录。
server.config.LOCAL_CFG = os.path.join(_TMP, "config.local.json")

OK, BAD = 0, 0


def check(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        BAD += 1
        print(f"  ✗ {name}  {extra}")


# ---------------------------------------------------------------- 假 Canvas
class FakeCanvasClient:
    """占位客户端。只为了拦住真实的 requests.Session。"""

    def __init__(self, base=None, token=None, verbose=False):
        self.base, self.token = base, token

    def me(self):
        return {"name": "测试用户", "login_id": "000000"}


class FakeDDLService:
    """假的取数服务：数它被调用了几次，这正是缓存的观察点。"""

    calls = 0

    def __init__(self, client, verbose=False):
        self.c = client

    def pending(self, *a, **kw):
        FakeDDLService.calls += 1
        now = datetime.now(CST)
        return [
            make_deadline("fake-1", "假的第五章作业", now + timedelta(days=1),
                          model.SRC_CANVAS, course="假的课程",
                          sources=["逐课程查询"]),
            make_deadline("fake-2", "假的远期报告", now + timedelta(days=30),
                          model.SRC_CANVAS, course="假的课程",
                          sources=["逐课程查询"]),
        ]


def cal_payload(days=2):
    """一份日期相对"现在"的交我办导出数据（写死日期会过期）。"""
    now = datetime.now(CST)
    f = lambda d: d.strftime("%Y-%m-%d %H:%M")           # noqa: E731
    return {
        "exported_at": now.isoformat(),
        "calendars": {"my": [{"id": 11, "name": "我的日程"}]},
        "events": [{
            "eventId": 9001, "calendarId": 11, "title": "导入进来的测试日程",
            "startTime": f(now + timedelta(days=days)),
            "endTime": f(now + timedelta(days=days, hours=1)),
            "location": "电院3号楼",
        }],
    }


def main():
    server.CanvasClient = FakeCanvasClient
    server.DDLService = FakeDDLService
    server.app.config["TESTING"] = True
    c = server.app.test_client()

    def q(text, **kw):
        r = c.post("/api/query", json=dict(text=text, **kw))
        return r.status_code, r.get_json()

    try:
        print("状态接口")
        resp = c.get("/api/status")
        code, s = resp.status_code, resp.get_json()
        check("/api/status 返回 200", code == 200, str(code))
        for k in ("demo", "demo_banner", "demo_count", "cache_ttl", "cache_age",
                  "has_token", "kinds", "inbox", "configured"):
            check(f"status 里有 {k}", k in s, sorted(s.keys()))
        check("默认演示模式是关的", s["demo"] is False)
        check("还没导入过交我办 → 未完成首次配置", s["configured"] is False)
        check("测试用的是临时数据目录", s["inbox"].startswith(_TMP), s["inbox"])

        print("\n演示模式")
        d = c.post("/api/demo", json={"on": True}).get_json()
        check("能打开演示模式", d["demo"] is True and d["ok"])
        check("演示条目数 > 0", d["count"] >= 8, d["count"])
        check("打开后有横幅文案", bool(d["banner"]))
        check("状态接口同步显示开着",
              c.get("/api/status").get_json()["demo"] is True)

        # 演示数据每次现生成：两次拿到的条目 id 集合一致，说明不是随机丢的
        ids1 = [i["id"] for i in server.demo.items()]
        ids2 = [i["id"] for i in server.demo.items()]
        check("演示条目 id 稳定", ids1 == ids2)

        FakeDDLService.calls = 0
        server.reset_cache()
        code, r = q("这周有什么安排")
        check("演示模式下查询成功", code == 200 and r["ok"])
        check("★演示模式下一次 Canvas 都没打", FakeDDLService.calls == 0,
              f"打了 {FakeDDLService.calls} 次")
        check("结果里标了是演示数据", r.get("demo") is True)
        check("结果带演示横幅", bool(r.get("demo_banner")))
        kinds = {i["kind"] for i in r["items"]}
        check("截止和日程都出现了",
              kinds == {model.KIND_DEADLINE, model.KIND_SCHEDULE}, kinds)
        check("来源写着演示数据",
              all("演示数据" in i["source"] for i in r["items"]))
        check("演示模式不产生缓存", r["cache_age"] is None)

        titles = [i["title"] for i in r["items"]]
        check("「这周」窗口滤掉了 9 天后的那条",
              "实验报告 3" not in titles, titles)
        check("已结束的日程不再列出", "已结束的组会" not in titles, titles)
        check("过期的截止仍然列出", "第五章作业" in titles, titles)

        _, r2 = q("我还有哪些作业没交")
        check("演示模式下也能只问截止",
              all(i["kind"] == model.KIND_DEADLINE for i in r2["items"]))

        print("\n手动录入")
        resp = c.post("/api/manual", json={"title": "没写类型"})
        code, r = resp.status_code, resp.get_json()
        check("缺类型被拒（400）", code == 400, code)
        check("缺类型有说明", "必须说明" in (r.get("error") or ""), r)

        code = c.post("/api/manual",
                      json={"kind": "作业", "title": "x", "due": "2026-12-01T10:00"}
                      ).status_code
        check("非法类型被拒（400）", code == 400, code)

        code = c.post("/api/manual",
                      json={"kind": "deadline", "title": "忘了时间"}).status_code
        check("截止缺时间被拒（400）", code == 400, code)

        r = c.post("/api/manual", json={
            "kind": "deadline", "title": "手动截止", "due": "2026-12-01T23:59",
            "course": "高等数学",
        }).get_json()
        check("手动截止加成功", r["ok"])
        md = r["item"]
        check("★课程名写进去了", md["course"] == "高等数学", md.get("course"))
        check("类型是 deadline", md["kind"] == model.KIND_DEADLINE)

        r = c.post("/api/manual", json={
            "kind": "日程", "title": "手动日程", "start": "2026-12-02T14:00",
            "end": "2026-12-02T15:00", "location": "上院100",
        }).get_json()
        check("手动日程加成功", r["ok"])
        ms = r["item"]
        check("★地点写进去了", ms["location"] == "上院100", ms.get("location"))
        check("类型是日程", ms["kind"] == model.KIND_SCHEDULE)

        code = c.delete(f"/api/manual/{md['id']}").status_code
        check("删得掉", code == 200, code)
        code = c.delete("/api/manual/manual-9999").status_code
        check("删不存在的给 404", code == 404, code)

        print("\n交我办导入")
        r = c.post("/api/import", json={"payload": cal_payload(),
                                        "filename": "test.json"}).get_json()
        check("导入成功", r["ok"], r)
        check("导入到 1 条", r["count"] == 1, r)
        check("状态里能看到导入时间",
              c.get("/api/status").get_json()["calendar"]["count"] == 1)
        check("★导入后首次配置就算完成了",
              c.get("/api/status").get_json()["configured"] is True)

        print("\nCanvas 缓存")
        c.post("/api/demo", json={"on": False})     # 先关掉演示模式
        server.reset_cache()
        FakeDDLService.calls = 0

        _, r = q("我还有哪些作业没交")
        check("关掉演示模式后走真 Canvas", FakeDDLService.calls == 1,
              f"打了 {FakeDDLService.calls} 次")
        check("拿到了假 Canvas 的数据",
              any(i["id"] == "fake-1" for i in r["items"]))
        check("缓存年龄是个数", isinstance(r["cache_age"], (int, float)),
              r["cache_age"])

        _, r = q("高数有什么作业")
        check("★第二次查询命中缓存（没再打 Canvas）",
              FakeDDLService.calls == 1, f"打了 {FakeDDLService.calls} 次")

        _, r = q("这周有什么安排", refresh=True)
        check("★refresh 能穿透缓存", FakeDDLService.calls == 2,
              f"打了 {FakeDDLService.calls} 次")

        _, r = q("我明天有什么日程")
        check("只问日程时不打 Canvas", FakeDDLService.calls == 2,
              f"打了 {FakeDDLService.calls} 次")
        check("只问日程时只剩日程",
              all(i["kind"] == model.KIND_SCHEDULE for i in r["items"]))
        check("只问日程时 cache_age 为空（这次没碰 Canvas）",
              r["cache_age"] is None, r["cache_age"])

        check("窗口过滤：30 天后的假作业不在这周里",
              "假的远期报告" not in [i["title"] for i in
                                     q("这周有什么安排")[1]["items"]])

        print("\n按条件取数（不带文字，只给 kind / range）")
        # 网页上两个下拉列表走的就是这条路径，
        # 和"问一句话"是同一个接口，只是不带 text。
        code, r = q("", kind=model.KIND_SCHEDULE)
        check("只给 kind 也能查", code == 200 and r["ok"], r)
        check("kind=日程 时只剩日程",
              all(i["kind"] == model.KIND_SCHEDULE for i in r["items"]))
        check("导入的那条日程出现在日程里",
              any(i["title"] == "导入进来的测试日程" for i in r["items"]))

        code, r = q("", kind=model.KIND_DEADLINE)
        check("kind=deadline 时只剩截止",
              all(i["kind"] == model.KIND_DEADLINE for i in r["items"]))
        check("kind=deadline 会去拿 Canvas 数据",
              any(i["id"] == "fake-1" for i in r["items"]))

        code, r = q("", range="7d")
        check("只给时间范围也能查", code == 200 and r["ok"])
        check("「一周内」滤掉了 30 天后那条",
              "假的远期报告" not in [i["title"] for i in r["items"]])
        check("返回里带上范围的名字", r["range_label"] == "一周内", r["range_label"])

        print("\n否定句与听不出来的话")
        _, r = q("明天没什么要交的")
        check("否定句不当查询", r["parsed"]["is_query"] is False)
        check("否定句不返回条目", r["items"] == [])
        check("否定句给出理由", bool(r.get("note")))

        _, r = q("今天天气不错")
        check("听不出来的话不硬猜", r["parsed"]["is_query"] is False)

        print("\n缓存清空的时机")
        c.post("/api/demo", json={"on": True})
        _, r = q("这周有什么安排")
        check("重新打开演示模式后又离线了", r["cache_age"] is None)
        c.post("/api/demo", json={"on": False})
        check("关掉演示模式会清缓存",
              c.get("/api/status").get_json()["cache_age"] is None)

        print("\n时间范围（下拉列表）")
        # 时间窗必须**相对"现在"**构造：写死日期过几天就跑不过了。
        #
        # 但"正在进行"的一段日程在**临近午夜**时会跨过午夜——那时它确实同时落在
        # 今天和明天的窗口里（in_window 判得对，是"今天只有它"这个假设不对）。
        # 所以把锚点日程**夹在当天之内**；离午夜不足一分钟时它已经结束，那条断言跳过。
        now = datetime.now(CST)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = now.replace(hour=23, minute=59, second=0, microsecond=0)
        anchor_end = min(now + timedelta(minutes=55), day_end)
        anchor_start = max(min(now - timedelta(minutes=5), anchor_end - timedelta(minutes=1)),
                           day_start)
        anchor_ongoing = anchor_end > now      # 23:59 之后跑，它已经结束了

        def at(days, hour=12):
            d = (now + timedelta(days=days)).replace(hour=hour, minute=0,
                                                     second=0, microsecond=0)
            return d.strftime("%Y-%m-%d %H:%M")

        payload = {
            "exported_at": now.isoformat(),
            "calendars": {"my": [{"id": 11, "name": "我的日程"}]},
            "events": [
                {"eventId": 21, "calendarId": 11, "title": "正在进行的日程",
                 "startTime": anchor_start.strftime("%Y-%m-%d %H:%M"),
                 "endTime": anchor_end.strftime("%Y-%m-%d %H:%M")},
                {"eventId": 22, "calendarId": 11, "title": "明天的日程",
                 "startTime": at(1), "endTime": at(1, 13)},
                {"eventId": 23, "calendarId": 11, "title": "十天后的日程",
                 "startTime": at(10), "endTime": at(10, 13)},
            ],
        }
        r = c.post("/api/import", json={"payload": payload,
                                        "filename": "range.json"}).get_json()
        check("导入了三条不同日期的日程", r["ok"] and r["count"] == 3, r)

        od = c.post("/api/manual", json={
            "kind": "deadline", "title": "逾期未交的作业",
            "due": (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
        }).get_json()
        check("再加一条两天前就该交的作业", od["ok"], od)

        # 演示模式在上一步已经关掉，这里走真 Canvas（假客户端）
        t = lambda r: [i["title"] for i in r["items"]]        # noqa: E731

        _, r = q("", range="today")
        if anchor_ongoing:
            check("「今天」带上正在进行的日程", "正在进行的日程" in t(r), t(r))
        else:
            print("      （离午夜不足一分钟，锚点日程已经结束，这条跳过）")
        check("「今天」挡掉明天的日程", "明天的日程" not in t(r), t(r))
        check("「今天」挡掉十天后的日程", "十天后的日程" not in t(r), t(r))
        check("★「今天」仍然列出已经过期还没交的作业",
              "逾期未交的作业" in t(r), t(r))
        check("「今天」的区间就是今天一整天",
              r["range_label"] == "今天" and r["window"] == now.strftime("%m-%d"),
              (r["range_label"], r["window"]))

        _, r = q("", range="tomorrow")
        check("「明天」只带明天的日程",
              "明天的日程" in t(r) and "正在进行的日程" not in t(r), t(r))

        _, r = q("", range="3d")
        check("「近 3 天」挡掉十天后的", "十天后的日程" not in t(r), t(r))

        _, r = q("", range="all")
        check("「不限时间」把十天后的也带上", "十天后的日程" in t(r), t(r))
        check("不限时间时没有区间文字", r["window"] == "", r["window"])

        # 两个下拉一起用：问题=有什么日程、时间=明天
        _, r = q("", kind=model.KIND_SCHEDULE, range="tomorrow")
        check("★两个下拉一起用：明天的日程只剩日程一条",
              t(r) == ["明天的日程"], t(r))

        _, r = q("", range="custom", start=now.strftime("%Y-%m-%d"),
                 end=(now + timedelta(days=1)).strftime("%Y-%m-%d"))
        check("自定义区间含首尾两天",
              "正在进行的日程" in t(r) and "明天的日程" in t(r)
              and "十天后的日程" not in t(r), t(r))
        check("自定义区间把范围写出来",
              "~" in (r["window"] or ""), r["window"])

        code, r = q("", range="custom", start="2025-10-07", end="2025-10-01")
        check("自定义区间反过来填 → 400", code == 400, code)
        check("并且说明原因", "不能早于" in (r.get("error") or ""), r)

        code, r = q("", range="custom")
        check("自定义区间没填日期 → 400", code == 400, code)

        code, r = q("", range="下个月")
        check("不在列表里的时间范围 → 400", code == 400, code)

        code, r = q("", kind="作业")
        check("★不认识的问题类型 → 400（不悄悄返回空列表）", code == 400, code)

        print("\nCanvas 挂了的时候（回归：这里曾经返回 500 的 HTML）")
        import requests

        class BoomDDL:
            """模拟 Canvas 连不上：抛的是 requests 的异常，不是 CanvasError。"""

            def __init__(self, client, verbose=False):
                pass

            def pending(self, *a, **kw):
                raise requests.exceptions.ConnectionError(
                    "Failed to establish a new connection")

        server.DDLService = BoomDDL
        server.reset_cache()
        c.post("/api/demo", json={"on": False})
        resp = c.post("/api/query", json={"kind": model.KIND_DEADLINE})
        check("★Canvas 连不上时不再返回 500", resp.status_code == 200, resp.status_code)
        r = resp.get_json()
        # 行为说明：Canvas 抓不到时，现在会拿"上次成功抓到的缓存"顶上，
        # 但必须把这件事说清楚。这条断言以前是"没有任何一条来自 Canvas"——
        # 那时候 Canvas 一点都不落盘，抓不到就真的什么都没有；加了磁盘缓存之后，
        # "有数据、但标明是多久前的"比"什么都没有"更有用，断言跟着改成新契约。
        check("★而是 200 + 一句警告",
              r is not None and bool(r.get("warning")), str(r)[:100])
        check("警告里说明是 Canvas 取不到",
              "Canvas" in (r or {}).get("warning", ""), (r or {}).get("warning"))
        check("★抓不到时顶上的是上次成功的缓存",
              (r or {}).get("canvas_source") == "disk_local",
              (r or {}).get("canvas_source"))
        check("★并且标出缓存有多旧（秒）",
              isinstance((r or {}).get("canvas_stale_seconds"), int),
              (r or {}).get("canvas_stale_seconds"))
        check("★缓存里的作业仍然列出来（不因为一次失败就消失）",
              any(i.get("source") == model.SRC_CANVAS for i in (r.get("items") or [])),
              [i.get("id") for i in (r.get("items") or [])])
        check("出错不写进内存缓存",
              c.get("/api/status").get_json()["cache_age"] is None)
        server.DDLService = FakeDDLService
        server.reset_cache()

        print("\n抓不到之后先安静一会儿（别每次查询都重新等超时）")
        # 树莓派连着没有出口的热点时，每问一次都重新去连 = 每问一次卡 5–25 秒。
        # 失败之后短时间内直接用缓存回答，这是实机上能不能用的关键。
        class CountingBoom:
            calls = 0

            def __init__(self, client, verbose=False):
                pass

            def pending(self, *a, **kw):
                CountingBoom.calls += 1
                raise requests.exceptions.ConnectionError("boom")

        server.DDLService = CountingBoom
        server.reset_cache()
        outs = [c.post("/api/query", json={"kind": model.KIND_DEADLINE}).get_json()
                for _ in range(3)]
        check("★连着问三次，只去打了一次网络", CountingBoom.calls == 1,
              CountingBoom.calls)
        check("★另外两次照样有结果（用缓存）",
              all(o.get("canvas_source") == "disk_local" for o in outs),
              [o.get("canvas_source") for o in outs])
        check("★三次都带着警告，没有假装成功",
              all(bool(o.get("warning")) for o in outs))

        c.post("/api/query", json={"kind": model.KIND_DEADLINE, "refresh": True})
        check("★点「刷新」会立刻重试一次（不被退让挡住）",
              CountingBoom.calls == 2, CountingBoom.calls)

        server.DDLService = FakeDDLService
        server.reset_cache()

        print("\n网页上的下拉列表和服务端是同一份选项")
        # 选项清单在 query.RANGES 里，网页上照着写了一遍。
        # 这条检查就是防两边改歪：多一个少一个、或者文字不一样都算失败。
        html = c.get("/").get_data(as_text=True)
        for rid, label in query_mod.RANGES:
            check(f"下拉列表里有「{label}」",
                  f'<option value="{rid}">{label}</option>' in html, rid)

        print("\n浏览器端的分发")
        resp = c.get("/naiwozhu.user.js")
        check("能取到脚本", resp.status_code == 200, resp.status_code)
        ct = resp.headers.get("Content-Type") or ""
        check("Content-Type 是 javascript", "javascript" in ct, ct)
        body = resp.get_data(as_text=True)
        check("真的是用户脚本（==UserScript== 头）",
              body.startswith("// ==UserScript=="), body[:40])
        check("支持 GM_xmlhttpRequest", "GM_xmlhttpRequest" in body)
        check("支持扩展后台这条通路", "chrome.runtime.sendMessage" in body)
        check("只允许连本机（@connect 127.0.0.1）",
              "@connect      127.0.0.1" in body)
        check("匹配交我办日历页",
              "@match        https://calendar.sjtu.edu.cn/" in body)
        check("推的是本机 /api/import", "/api/import" in body)
        check("没有把数据发往第三方",
              "api.github" not in body and "gist" not in body.lower())
        r = c.get("/api/export-script").get_json()
        check("复制用的那份是同一个文件",
              bool(r.get("ok")) and "GM_xmlhttpRequest" in r["code"])

        s = c.get("/api/status").get_json()
        check("状态里给出扩展目录", bool(s.get("extension_dir")), s.get("extension_dir"))
        check("状态里标明扩展存在", s.get("has_extension") is True)

        print("\n自查扩展的打包")
        resp = c.get("/naiwozhu-extension.zip")
        check("能下到扩展 zip", resp.status_code == 200, resp.status_code)
        z = zipfile.ZipFile(io.BytesIO(resp.data))
        names = z.namelist()
        check("zip 里有 manifest.json",
              any(n.endswith("/manifest.json") for n in names), names)
        check("zip 里有后台脚本",
              any(n.endswith("/background.js") for n in names), names)
        check("zip 里有内容脚本",
              any(n.endswith("/naiwozhu.user.js") for n in names), names)
        man = json.loads(z.read([n for n in names if n.endswith("manifest.json")][0]))
        check("manifest 是 MV3", man.get("manifest_version") == 3, man.get("manifest_version"))
        check("只匹配交我办日历页",
              man["content_scripts"][0]["matches"] == ["https://calendar.sjtu.edu.cn/*"],
              man["content_scripts"][0]["matches"])
        check("只申请本机 host 权限",
              all(h.startswith("http://127.0.0.1") or h.startswith("http://localhost")
                  for h in man["host_permissions"]), man["host_permissions"])
        check("权限里没有多余的（只要 storage）",
              man.get("permissions") == ["storage"], man.get("permissions"))
        bg = z.read([n for n in names if n.endswith("background.js")][0]).decode()
        check("跨域那一跳放在后台文件里",
              "fetch(" in bg and "onMessage" in bg)

        print("\n令牌流程")
        # 注意：配置文件的路径已经被指到临时目录，这里不会动你真实的令牌。
        r = c.post("/api/token", json={"token": ""}).get_json()
        check("空令牌被拒", r["ok"] is False and "空" in r["error"])
        resp = c.post("/api/token", json={"token": "whatever"})
        r = resp.get_json()
        check("假客户端下能保存令牌", resp.status_code == 200 and r["ok"], r)
        check("返回了姓名", r.get("name") == "测试用户", r)
        check("令牌写进的是临时配置",
              os.path.exists(server.config.LOCAL_CFG))

        print("\n局域网访问与访问口令")
        # environ_base 换掉 REMOTE_ADDR，就能模拟"从另一台机器访问"。
        # 注意：上面的请求都是回环来的，所以配置里一直没有口令。
        LAN = {"REMOTE_ADDR": "192.168.50.7"}
        r = c.get("/api/status", environ_base=LAN)
        check("★局域网上还没设口令 → 403（宁可不服务）", r.status_code == 403,
              r.status_code)
        check("并且说明原因", "口令" in (r.get_json() or {}).get("error", ""),
              r.get_json())

        check("页面本身不拦（否则没机会提示输口令）",
              c.get("/", environ_base=LAN).status_code == 200)

        key = server.config.save_access_key("test-access-key-0123456789")
        check("能生成并保存访问口令", key == "test-access-key-0123456789", key)

        r = c.get("/api/status", environ_base=LAN)
        check("★有口令但没带 → 401", r.status_code == 401, r.status_code)
        check("并且告诉前端要口令",
              (r.get_json() or {}).get("need_key") is True, r.get_json())

        r = c.get("/api/status", environ_base=LAN, headers={"X-Access-Key": "wrong"})
        check("口令不对 → 401", r.status_code == 401, r.status_code)

        r = c.get("/api/status", environ_base=LAN, headers={"X-Access-Key": key})
        check("★口令对了 → 200", r.status_code == 200, r.status_code)
        s2 = r.get_json()
        check("状态里说清这台机器怎么被访问的",
              (s2.get("bind") or {}).get("loopback_only") is True
              and (s2.get("access") or {}).get("has_key") is True, s2.get("bind"))

        check("口令也能用查询参数带",
              c.get("/api/status?key=" + key, environ_base=LAN).status_code == 200)
        check("本机访问不需要口令", c.get("/api/status").status_code == 200)
        check("静态的脚本也不拦",
              c.get("/naiwozhu.user.js", environ_base=LAN).status_code == 200)

        print("\n同步接口")
        import sync as sync_mod
        bundle = sync_mod.build_bundle(server.store)
        blob = json.dumps(bundle, ensure_ascii=False)
        check("★同步包里没有 Canvas 令牌",
              "whatever" not in blob, blob[:80])
        check("★同步包里没有访问口令", key not in blob)

        r = c.post("/api/sync", json=bundle)
        d = r.get_json()
        check("自环同步返回 200", r.status_code == 200, r.status_code)
        flat = [v if isinstance(v, str) else v.get("added", 0) + v.get("updated", 0)
                + v.get("deleted", 0) for v in d["changes"].values()]
        check("★自环同步什么都没变（幂等）",
              all(v in ("kept", 0) for v in flat), d["changes"])
        check("回包里带着合并后的包", isinstance(d.get("bundle"), dict))

        check("坏包被拒（400）",
              c.post("/api/sync", json={"hi": "there"}).status_code == 400)
        check("版本不认识被拒（400）",
              c.post("/api/sync", json={"schema": 999, "manual": []}).status_code == 400)

        r = c.post("/api/sync/now", json={})
        d = r.get_json()
        check("没配对端地址时明确报错",
              r.status_code == 400 and "对端地址" in (d.get("error") or ""), d)

        check("地址没写 http:// 被拒",
              c.post("/api/sync/settings",
                     json={"peer": "192.168.1.9:5173"}).status_code == 400)

        # 保存地址会顺便起后台同步线程。测试里把它换成假的：
        # 不真的去连 192.168.1.9，但能验证"调用了它"。
        real_start = server.start_sync_thread
        started = []
        server.start_sync_thread = lambda *a, **k: started.append((a, k))
        try:
            r = c.post("/api/sync/settings", json={"peer": "http://192.168.1.9:5173"})
            d = r.get_json()
            check("能保存对端地址",
                  d.get("ok") and d.get("peer") == "http://192.168.1.9:5173", d)
            check("状态里能看到对端地址",
                  c.get("/api/status").get_json()["sync"]["peer"]
                  == "http://192.168.1.9:5173")
            check("★保存地址后会顺手开始自动同步（不用重启）",
                  bool(started), started)
            check("间隔不是整数被拒",
                  c.post("/api/sync/settings",
                         json={"interval": "abc"}).status_code == 400)
            check("能保存间隔",
                  c.post("/api/sync/settings",
                         json={"interval": 120}).get_json().get("interval") == 120)
        finally:
            server.start_sync_thread = real_start

        print("\n接口契约（协作者依赖的东西不许悄悄变）")
        # 新增字段可以，删字段/改形状不行。这一段就是那道闸。
        got = server.canvas_deadlines()
        check("★canvas_deadlines() 仍然返回 (items, warning) 两个值",
              isinstance(got, tuple) and len(got) == 2, type(got))

        r = c.post("/api/query", json={"kind": model.KIND_DEADLINE}).get_json()
        for k in ("ok", "items", "parsed", "count", "kind", "range",
                  "range_label", "window", "demo", "cache_age"):
            check(f"/api/query 仍然有 {k}", k in r, sorted(r))
        check("/api/query 的 items 仍是条目列表", isinstance(r["items"], list))
        if r["items"]:
            it = r["items"][0]
            for k in ("kind", "id", "title", "source", "remaining", "overdue",
                      "when_local"):
                check(f"条目仍然有 {k}", k in it, sorted(it))

        s = c.get("/api/status").get_json()
        for k in ("has_token", "base", "token_source", "help", "kinds",
                  "calendar", "inbox", "manual_count", "has_export_script",
                  "extension_dir", "has_extension", "extension_zip",
                  "configured", "demo", "demo_banner", "demo_count",
                  "cache_ttl", "cache_age"):
            check(f"/api/status 仍然有 {k}", k in s, sorted(s))
        for k in ("imported_at", "exported_at", "filename", "count", "stats"):
            check(f"calendar 状态里仍然有 {k}", k in s["calendar"], sorted(s["calendar"]))

        check("★store.manual_items() 不带参数照样能用",
              isinstance(server.store.manual_items(), list))
        check("store.add_manual 的参数没变（kind/title + 关键字）",
              "kind" in server.store.add_manual.__code__.co_varnames
              and "title" in server.store.add_manual.__code__.co_varnames)

        print("\n首次配置的判定（树莓派那种情况）")
        os.remove(server.store.cal_path)
        s3 = c.get("/api/status").get_json()
        check("★没有交我办快照、但有令牌且有对端 → 也算配置好了",
              s3["configured"] is True, s3["configured"])

        print("\nCanvas 落到磁盘、没令牌时顶上")
        now = datetime.now(CST)
        mine = server.store.device_id()
        item = make_deadline("cv-disk-1", "缓存里的作业", now + timedelta(days=2),
                             model.SRC_CANVAS, sources=["测试"])
        server.store.save_canvas(now, [item], fetch_ok=True, origin_device=mine)
        check("canvas.json 写下去了",
              c.get("/api/status").get_json()["canvas"]["meta"]["count"] == 1)

        real_load = server.config.load
        server.config.load = lambda: dict(real_load(), token="", source="测试")
        try:
            server.reset_cache()
            r = c.post("/api/query", json={"kind": model.KIND_DEADLINE}).get_json()
            check("★本机没令牌时用磁盘缓存回答",
                  r.get("canvas_source") == "disk_local", r.get("canvas_source"))
            check("并且说清是缓存",
                  "缓存" in (r.get("warning") or ""), r.get("warning"))
            check("缓存里的作业列出来了",
                  "缓存里的作业" in [i.get("title") for i in r.get("items") or []],
                  [i.get("title") for i in r.get("items") or []])
            check("还报出缓存有多旧",
                  isinstance(r.get("canvas_stale_seconds"), int),
                  r.get("canvas_stale_seconds"))

            server.store.save_canvas(now, [item], fetch_ok=True,
                                     origin_device="ffffffff")
            server.reset_cache()
            r = c.post("/api/query", json={"kind": model.KIND_DEADLINE}).get_json()
            check("★缓存来自另一台机器时标明来源",
                  r.get("canvas_source") == "disk_peer", r.get("canvas_source"))
        finally:
            server.config.load = real_load
            server.reset_cache()

        print("\n只拿到一部分课程时（别把没取到的课从缓存里抹掉）")
        # 某门课取不到（403 / 网络抖一下）时，整次抓取并不是"干净的成功"。
        # 这种结果可以拿来回答，但不该覆盖上次完整的那份快照——
        # 否则那门课的作业会凭空消失，看起来像"交了"。
        class PartialDDL:
            def __init__(self, client, verbose=False):
                self.skipped = [{"course_id": 11, "course": "高等数学",
                                 "error": "CanvasError: 403 无权限"}]

            def pending(self, *a, **kw):
                return [make_deadline("cv-partial-1", "只拿到这门课的作业",
                                      now + timedelta(days=1), model.SRC_CANVAS,
                                      sources=["测试"])]

        real_ddl = server.DDLService
        server.DDLService = PartialDDL
        try:
            server.reset_cache()
            r = c.post("/api/query", json={"kind": model.KIND_DEADLINE}).get_json()
            check("部分成功也照样给出结果",
                  "只拿到这门课的作业" in [i.get("title") for i in r.get("items") or []],
                  [i.get("title") for i in r.get("items") or []])
            check("★并说明可能有课没取到",
                  "可能不全" in (r.get("warning") or ""), r.get("warning"))
            snap = server.store.canvas_snapshot()
            check("★没有覆盖磁盘上那份完整的缓存",
                  [i.get("title") for i in snap.get("items") or []]
                  == ["缓存里的作业"],
                  [i.get("title") for i in snap.get("items") or []])
        finally:
            server.DDLService = real_ddl
            server.reset_cache()

    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    print("\n" + "=" * 50)
    if BAD:
        print(f"失败 {BAD} 项，通过 {OK} 项")
        return 1
    print(f"全部通过 ✓（{OK} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

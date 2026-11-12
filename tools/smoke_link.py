#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""链路冒烟：语音终端（terminal/backend.py）↔ 电脑端（app/server.py）。

只验**契约**这一段：HTTP + JSON + 访问口令 + 字段。
麦克风、唤醒、云端识别、大模型要硬件和密钥，不在这个脚本范围内。

用法（先起电脑端，再跑这个）：

    # 同一个终端里没起服务时
    .venv/bin/python app/server.py --no-browser          # 默认 127.0.0.1:5173，本机不要口令

    # 另一个终端
    .venv/bin/python tools/smoke_link.py                 # 默认连 127.0.0.1:5173

    # 连别的机器 / 要口令
    BACKEND_URL=http://192.168.1.23:5173 NAIWOZHU_ACCESS_KEY=那串口令 \\
        .venv/bin/python tools/smoke_link.py

    # 只读模式：跳过加、改、删、隐藏（不想动数据时用）
    .venv/bin/python tools/smoke_link.py --read-only

注意：默认会造一条 `manual-*` 日程验证写操作，结束时删掉；隐藏过的 cal-* 条目会恢复。
"""
import argparse
import os
import sys
import urllib.parse
import traceback
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TERMINAL = os.path.join(ROOT, "terminal")
sys.path.insert(0, TERMINAL)

os.environ.setdefault("BACKEND_URL", "http://127.0.0.1:5173")
os.environ.setdefault("BACKEND_TOKEN", os.environ.get("NAIWOZHU_ACCESS_KEY", ""))

import backend                       # noqa: E402  （config 在导入时读环境变量）

OK, BAD = 0, 0
CST = timezone(timedelta(hours=8))


def check(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        BAD += 1
        print(f"  ✗ {name}  {extra}")


def main():
    ap = argparse.ArgumentParser(description="终端 ↔ 电脑端 链路冒烟")
    ap.add_argument("--read-only", action="store_true",
                    help="只测查询与提醒，不造数据、不改不删")
    args = ap.parse_args()

    print(f"电脑端：{backend.config.BACKEND_URL}")
    print(f"口令：{'已带' if backend.config.BACKEND_TOKEN else '未带（本机访问不需要）'}")
    if not args.read_only:
        print("写操作：开（结束时会清掉造的条目）")
    print()

    print("1. 自检接口 /api/status")
    st = backend.status()
    check("status 返回 ok", st.get("ok") is not False, st)
    check("能看到演示模式开关", "demo" in st)
    check("能看到交我办快照", "calendar" in st)
    check("能看到本机设备号", bool(st.get("device_id")))

    print("\n2. 查询 /api/query（语音那一路给结构化参数，不传 text）")
    q = backend.query(kind="deadline", range_id="all")
    check("返回 ok 与条目数组", q.get("ok") and isinstance(q.get("items"), list), q.keys())
    check("count 与 items 长度一致", q.get("count") == len(q.get("items") or []), q.get("count"))
    q2 = backend.query(kind="日程", range_id="all")
    check("日程也能查", q2.get("ok"), q2)
    check("日程条目的 kind 是「日程」",
          all(i.get("kind") == "日程" for i in q2.get("items") or []),
          {i.get("kind") for i in q2.get("items") or []})

    print("\n3. 条目列表 /api/items（改删隐藏那一路）")
    lst = backend.items()
    check("返回条目数组", isinstance(lst.get("items"), list), lst.keys())
    check("每条都带 editable / hideable",
          all("editable" in i and "hideable" in i for i in lst.get("items") or []))
    cal = [i for i in lst.get("items") or [] if str(i.get("id", "")).startswith("cal-")]
    canvas = [i for i in lst.get("items") or [] if str(i.get("id", "")).startswith("canvas-")]
    if cal:
        check("cal-* 只能隐藏、不能改", cal[0]["hideable"] and not cal[0]["editable"], cal[0])
    if canvas:
        check("canvas-* 两种都不行",
              not canvas[0]["hideable"] and not canvas[0]["editable"], canvas[0])

    print("\n4. 到点提醒 /api/remind")
    r = backend.remind(preview=True, limit=3)
    check("preview 拿到结果", isinstance(r, dict) and r.get("ok"), r)
    stg = [s.get("stage") for s in (r or {}).get("stages") or []]
    check("三个提醒位都在", set(stg) == {"morning", "evening", "soon"}, stg)
    items = [i for s in (r or {}).get("stages") or [] for i in s.get("items") or []]
    if items:
        one = items[0]
        check("条目带 remind_id / reason / title",
              bool(one.get("remind_id")) and bool(one.get("reason"))
              and bool(one.get("title")), one)
        check("位本身也有 remind_id",
              all(s.get("remind_id") for s in r["stages"]), r["stages"])
    else:
        print("  · 当前数据里没有落在窗口内的条目，跳过条目字段检查")
    real = backend.remind(limit=3)
    check("非 preview 也能调（此刻没到点就是空）",
          isinstance(real, dict) and isinstance(real.get("stages"), list), real)

    made = []
    if not args.read_only:
        print("\n5. 写操作往返（手动条目：加 → 改 → 删 → 恢复）")
        now = datetime.now(CST).replace(microsecond=0)
        start = (now + timedelta(days=30)).replace(hour=14, minute=0, second=0)
        end = start + timedelta(minutes=90)
        fmt = "%Y-%m-%dT%H:%M"
        added = backend.manual_add(kind="日程", title="链路冒烟·可删",
                                   start=start.strftime(fmt), end=end.strftime(fmt),
                                   location="测试地点", note="冒烟脚本生成")
        mid = (added.get("item") or {}).get("id")
        made.append(mid)
        check("新增成功并拿到 manual-* id",
              added.get("ok") and str(mid).startswith("manual-"), added)

        upd = backend.item_update(mid, {"title": "链路冒烟·改过了"})
        check("改标题成功", (upd.get("item") or {}).get("title") == "链路冒烟·改过了", upd)
        check("写操作标明只在本机生效",
              (upd.get("effective") or {}).get("local_only") is True, upd.get("effective"))

        check("删除成功", backend.item_delete(mid).get("ok"))
        try:
            backend.item_update(mid, {"title": "删了还想改"})
            check("★删掉之后再改应该 404", False, "没抛异常")
        except backend.ItemError as e:
            check("★删掉之后再改给 404", e.status == 404, (e.status, str(e)))

        check("能恢复", backend.item_restore(mid).get("ok"))
        got = [i for i in backend.items().get("items") or [] if i.get("id") == mid]
        check("★恢复后回到列表", len(got) == 1, got)

        print("\n6. 交我办条目：只能隐藏 / 恢复")
        cal = [i for i in backend.items().get("items") or []
               if str(i.get("id", "")).startswith("cal-")]
        if cal:
            cid = cal[0]["id"]
            try:
                backend.item_update(cid, {"start": start.strftime(fmt),
                                          "end": end.strftime(fmt)})
                check("★改 cal-* 应该 409", False, "没抛异常")
            except backend.ItemError as e:
                check("★改 cal-* 返回 409 readonly_source", e.readonly, (e.status, e.code))
            check("隐藏成功", backend.item_delete(cid).get("ok"))
            hidden_ids = [i.get("id") for i in backend.items(hidden=True).get("items") or []]
            check("★隐藏后出现在 hidden=1 里", cid in hidden_ids, hidden_ids)
            backend.item_restore(cid)
            check("恢复后回到列表",
                  cid in [i.get("id") for i in backend.items().get("items") or []])
        else:
            print("  · 这份数据里没有 cal-* 条目，跳过")

        for item_id in made:
            try:
                backend.item_delete(item_id)
            except Exception:
                pass

    print("\n7. 失败路径：口令不对 / 电脑端挂了")
    host = (urllib.parse.urlparse(backend.config.BACKEND_URL).hostname or "").lower()
    loopback = host in ("127.0.0.1", "localhost", "::1", "")
    if loopback:
        # 电脑端对「本机发起的请求」一律放行、不要口令，所以这里验不出 401。
        # 要验口令，得从另一台机器连（或用 --host 0.0.0.0 起服务再用局域网 IP 连）。
        print("  · 连的是本机，电脑端不校验口令，跳过口令检查")
    else:
        old = backend.config.BACKEND_TOKEN
        backend.config.BACKEND_TOKEN = "wrong-key"
        try:
            backend.status()
            check("★口令错要抛 AuthError", False, "没抛异常")
        except backend.AuthError:
            check("★口令错抛 AuthError", True)
        except backend.BackendError as e:
            check("★口令错抛 AuthError", False, e)
        finally:
            backend.config.BACKEND_TOKEN = old

    old_url = backend.config.BACKEND_URL
    backend.config.BACKEND_URL = "http://127.0.0.1:1"
    check("★电脑端连不上时 remind 静默返回 None", backend.remind() is None)
    try:
        backend.query(kind="deadline")
        check("查询连不上要抛 BackendError", False, "没抛异常")
    except backend.BackendError:
        check("查询连不上抛 BackendError", True)
    finally:
        backend.config.BACKEND_URL = old_url

    print("\n" + "=" * 52)
    if BAD:
        print(f"失败 {BAD} 项，通过 {OK} 项")
        return 1
    print(f"全部通过 ✓（{OK} 项）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)

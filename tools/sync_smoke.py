#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两台机器同步的验收脚本。

默认在**同一台机器上**起两个互不干扰的实例（各用各的数据目录和配置文件），
跑一遍完整流程：各自新增 → 自动同步 → 删除 → 反向同步不许复活。

真机上也可以直接指向已经跑着的两台（比如电脑 + 树莓派）：

    # 本机模拟两台
    .venv/bin/python tools/sync_smoke.py

    # 指向真机（两边都要已经 --host 0.0.0.0 起来，且口令一致）
    .venv/bin/python tools/sync_smoke.py \\
        --a http://192.168.1.10:5173 --b http://192.168.1.23:5173 --key 你的口令

跑通了就说明地址、口令、合并规则三样都对。退出码 0 表示全过。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")

OK, BAD = 0, 0


def check(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        BAD += 1
        print(f"  ✗ {name}  {extra}")
    return cond


# ---------------------------------------------------------------- HTTP
def http(url, method="GET", body=None, key=None, timeout=20):
    """返回 (状态码, 解析后的 JSON)。连不上时状态码是 0。"""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-Access-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw or "{}")
            except ValueError:
                return resp.status, {"_raw": raw[:200]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, {"_raw": raw[:200]}
    except Exception:                            # noqa: BLE001
        return 0, {}


def status(base, key):
    return http(base + "/api/status", key=key)[1]


def titles(base, key):
    code, d = http(base + "/api/query", "POST", {"text": ""}, key=key)
    if code != 200:
        return None
    return [i.get("title") for i in (d.get("items") or [])
            if str(i.get("id", "")).startswith("manual-")]


def wait_until(fn, timeout=20.0, step=0.3):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
        except Exception:                        # noqa: BLE001
            last = None
        if last:
            return last
        time.sleep(step)
    return last


# ---------------------------------------------------------------- 起实例
def spawn(port, workdir, key):
    env = dict(os.environ)
    env["NAIWOZHU_DATA_DIR"] = os.path.join(workdir, "data")
    env["NAIWOZHU_CONFIG"] = os.path.join(workdir, "config.local.json")
    env["NAIWOZHU_ACCESS_KEY"] = key
    # 别拿真实令牌去联网：这两个实例只测同步
    env.pop("CANVAS_TOKEN", None)
    os.makedirs(env["NAIWOZHU_DATA_DIR"], exist_ok=True)
    log = open(os.path.join(workdir, "server.log"), "wb")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(APP, "server.py"),
         "--port", str(port), "--no-browser"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    return proc, log


def main():
    ap = argparse.ArgumentParser(description="两台机器同步验收")
    ap.add_argument("--a", help="A 的地址；给了就不在本机起实例")
    ap.add_argument("--b", help="B 的地址；给了就不在本机起实例")
    ap.add_argument("--key", default="", help="真机模式下的访问口令")
    ap.add_argument("--port-a", type=int, default=5311)
    ap.add_argument("--port-b", type=int, default=5312)
    ap.add_argument("--keep", action="store_true", help="保留临时目录，方便看日志")
    args = ap.parse_args()

    remote = bool(args.a and args.b)
    workdirs, procs = [], []

    if remote:
        base_a, base_b = args.a.rstrip("/"), args.b.rstrip("/")
        key = args.key
    else:
        key = args.key or "smoke-key-0123456789abcdef"
        base_a = f"http://127.0.0.1:{args.port_a}"
        base_b = f"http://127.0.0.1:{args.port_b}"
        for port in (args.port_a, args.port_b):
            wd = tempfile.mkdtemp(prefix=f"nwz-smoke-{port}-")
            workdirs.append(wd)
            procs.append(spawn(port, wd, key))

    try:
        print("起两个实例" if not remote else "连上两台机器")
        a_up = wait_until(lambda: status(base_a, key).get("device_id") or None)
        b_up = wait_until(lambda: status(base_b, key).get("device_id") or None)
        if not check("A 起来了", bool(a_up), base_a):
            return 1
        if not check("B 起来了", bool(b_up), base_b):
            return 1

        sa, sb = status(base_a, key), status(base_b, key)
        dev_a, dev_b = sa.get("device_id"), sb.get("device_id")
        check("两台的设备号不一样", dev_a != dev_b, f"{dev_a} / {dev_b}")

        print("\n配对上（地址 + 口令 + 自动同步）")
        code, d = http(base_a + "/api/sync/settings", "POST",
                       {"peer": base_b, "interval": 0}, key=key)
        check("A 记住了 B 的地址", code == 200 and d.get("peer") == base_b, d)
        code, d = http(base_b + "/api/sync/settings", "POST",
                       {"peer": base_a, "interval": 0}, key=key)
        check("B 记住了 A 的地址", code == 200 and d.get("peer") == base_a, d)

        print("\n各自新增，另一端要看得见")
        code, d = http(base_a + "/api/manual", "POST",
                       {"kind": "日程", "title": "A 加的会",
                        "start": "2026-11-01T10:00", "end": "2026-11-01T11:00"},
                       key=key)
        check("A 加成功", code == 200 and d.get("ok"), d)
        got = wait_until(lambda: "A 加的会" in (titles(base_b, key) or []))
        check("★B 自动看到了 A 加的", bool(got), titles(base_b, key))

        code, d = http(base_b + "/api/manual", "POST",
                       {"kind": "日程", "title": "B 加的会",
                        "start": "2026-11-02T10:00", "end": "2026-11-02T11:00"},
                       key=key)
        check("B 加成功", code == 200 and d.get("ok"), d)
        got = wait_until(lambda: "B 加的会" in (titles(base_a, key) or []))
        check("★A 自动看到了 B 加的", bool(got), titles(base_a, key))

        print("\n重复同步（幂等）")
        before = sorted(titles(base_a, key) or [])
        for _ in range(3):
            http(base_a + "/api/sync/now", "POST", {}, key=key)
            http(base_b + "/api/sync/now", "POST", {}, key=key)
        after = sorted(titles(base_a, key) or [])
        check("★同步三次，数据一点没变", before == after, f"{before} → {after}")
        check("两边内容一致", sorted(titles(base_a, key) or [])
              == sorted(titles(base_b, key) or []),
              f"{titles(base_a, key)} / {titles(base_b, key)}")

        print("\n删除：不能被对方复活")
        items = http(base_a + "/api/query", "POST", {"text": ""}, key=key)[1].get("items") or []
        target = [i for i in items if i.get("title") == "A 加的会"]
        check("找到要删的那条", bool(target))
        if target:
            code, _ = http(base_a + "/api/manual/" + target[0]["id"], "DELETE", key=key)
            check("A 删成功", code == 200, code)
            gone = wait_until(lambda: "A 加的会" not in (titles(base_b, key) or []))
            check("★B 那边也消失了", bool(gone), titles(base_b, key))

            # 反向同步：B 手里曾经有这条，看会不会复活
            http(base_b + "/api/sync/now", "POST", {}, key=key)
            http(base_a + "/api/sync/now", "POST", {}, key=key)
            check("★反向同步也不会复活",
                  "A 加的会" not in (titles(base_a, key) or []),
                  titles(base_a, key))

        print("\n凭据不外泄")
        code, d = http(base_a + "/api/status", key=key)
        blob = json.dumps(d, ensure_ascii=False)
        check("★状态接口里不回显访问口令", key not in blob, blob[:120])
        check("状态里只说有没有口令", (d.get("access") or {}).get("has_key") is True, d.get("access"))

        st = status(base_a, key)
        check("状态里记着上次同步成功",
              ((st.get("sync") or {}).get("state") or {}).get("last_ok") is True,
              (st.get("sync") or {}).get("state"))

    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:                    # noqa: BLE001
                try:
                    p.kill()
                except Exception:                # noqa: BLE001
                    pass
        for wd in workdirs:
            if args.keep:
                print(f"\n日志和数据留着：{wd}")
            else:
                shutil.rmtree(wd, ignore_errors=True)

    print("\n" + "=" * 50)
    if BAD:
        print(f"失败 {BAD} 项，通过 {OK} 项")
        return 1
    print(f"全部通过 ✓（{OK} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

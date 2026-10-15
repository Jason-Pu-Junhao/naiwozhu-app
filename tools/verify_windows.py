#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 兼容性体检：把生成的报告贴回来，就能判定这个项目在 Windows 上能不能跑。

用法（Windows）：
    powershell -ExecutionPolicy Bypass -File tools\\verify_windows.ps1
已经有 .venv 了也可以直接跑：
    .venv\\Scripts\\python.exe tools\\verify_windows.py

它在真机上量四件事：

  ① 环境事实：系统版本、控制台代码页、locale、换行符、解释器路径、路径里有没有中文
  ② 四套测试在**三种输出编码环境下各跑一遍**，看哪一套会崩
     （默认 / 强制 GBK / UTF-8 修法）——这样"是不是编码问题、修法有没有用"
     是同一次运行里对比出来的，不靠推理
  ③ 起一次真服务、打几次真接口，确认网页那条链路在 Windows 上通
  ④ 查中文字体有没有缺字形（图上中文变方框就是这里出的问题），并列出这台机器上
     **哪些字体真的能打汉字**

不动仓库里已入库的东西：

  · 数据目录指向 out/win-verify/data，不碰你真实的 app/data/
  · 报告、原始日志、样例图全在 out/win-verify/
  · 只有跑 md2docx 那一步会往 out/ 写一个 docx（那本来就是它的输出目录）

退出码：0 = 没有失败项；1 = 有失败项。
"""
import argparse
import json
import locale
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
OUT = os.path.join(ROOT, "out", "win-verify")
RAW = os.path.join(OUT, "raw")

# 复用 app/console.py 里那份修复：本脚本自己也要打印中文，
# 重定向到文件时同样会踩 GBK 编不出字符的坑
try:
    sys.path.insert(0, APP)
    import console                                    # noqa: E402
    console.setup()
except Exception:
    pass

SUITES = ["test_query.py", "test_ddl.py", "test_store.py", "test_server.py"]

# 三种输出编码环境。第一种是"什么都不设"，也就是 Windows 上默认的样子。
#
# 第三个数是"模拟项"标记：cp936 那一列是**故意**把编码按死成 GBK 来复现机制，
# 在任何系统上都会崩，所以它不计入失败总数，也不影响退出码。
# 真正要看的是"默认"那一列在 Windows 上的结果。
MODES = [
    ("默认", {}, False,
     "什么都不设：重定向到文件或管道时就是 Windows 默认的样子。"
     "app/console.py 修好之后，这里**应该**全过"),
    ("关掉修复", {"PYTHONIOENCODING": "gbk", "NAIWOZHU_NO_UTF8": "1"}, True,
     "模拟项：按死 GBK 且关掉 app/console.py 的自动修复，用来复现原始问题"),
    ("utf8", {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}, False,
     "另一条路：直接设 PYTHONUTF8=1"),
]

# 会干扰结论的变量，跑测试前一律清掉
SCRUB = ["PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO",
         "PYTHONSTARTUP", "PYTHONWARNINGS"]

RESULTS = []
FACTS = {}


# ---------------------------------------------------------------- 记录
def add(rid, name, status, detail=""):
    """status: PASS / FAIL / WARN / SKIP"""
    mark = {"PASS": "通过", "FAIL": "失败", "WARN": "注意", "SKIP": "跳过"}[status]
    RESULTS.append({"id": rid, "name": name, "status": status, "detail": detail})
    line = "  [%s] %-28s %s" % (mark, name, detail)
    print(line.rstrip())
    return status


def hr(title):
    print("\n" + title)
    print("-" * 68)


# ---------------------------------------------------------------- 跑子进程
def run(args, env=None, tag="cmd", timeout=600):
    """跑一个子进程，stdout/stderr 重定向到文件。

    这里的"重定向"是重点：Windows 上只有当输出不是控制台时才会退回到
    代码页编码，也就是问题真正会暴露的场合。
    """
    e = dict(os.environ)
    for k in SCRUB:
        e.pop(k, None)
    if env:
        e.update(env)
    o = os.path.join(RAW, tag + ".out")
    er = os.path.join(RAW, tag + ".err")
    t0 = time.time()
    with open(o, "wb") as fo, open(er, "wb") as fe:
        p = subprocess.Popen(args, cwd=ROOT, env=e, stdout=fo, stderr=fe)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            rc = -9
    return {
        "rc": rc, "seconds": round(time.time() - t0, 2),
        "out": read(o), "err": read(er), "out_path": o, "err_path": er,
    }


def read(path, limit=None):
    """读日志。子进程可能按 GBK 写（正是我们要复现的情形），所以两种都试。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return ""
    txt = None
    for enc in ("utf-8", "gbk"):
        try:
            txt = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if txt is None:
        txt = data.decode("utf-8", "replace")
    if limit and len(txt) > limit:
        return txt[:limit] + "\n…（截断）"
    return txt


def tail(txt, lines=12):
    ls = [x for x in txt.strip().splitlines() if x.strip()]
    return "\n".join(ls[-lines:])


# ---------------------------------------------------------------- ① 环境事实
def step_env():
    hr("① 环境事实")
    FACTS["platform"] = platform.platform()
    FACTS["machine"] = platform.machine()
    FACTS["python"] = sys.version.replace("\n", " ")
    FACTS["python_exe"] = sys.executable
    FACTS["prefix"] = sys.prefix
    FACTS["in_venv"] = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    FACTS["is_windows"] = os.name == "nt"
    FACTS["preferred_encoding"] = locale.getpreferredencoding(False)
    FACTS["filesystem_encoding"] = sys.getfilesystemencoding()
    FACTS["stdout_encoding"] = getattr(sys.stdout, "encoding", None)
    FACTS["repo"] = ROOT
    FACTS["repo_has_non_ascii"] = not all(ord(c) < 128 for c in ROOT)
    FACTS["bash"] = shutil.which("bash") or ""
    FACTS["soffice"] = shutil.which("soffice") or ""

    print("  系统            %s" % FACTS["platform"])
    print("  Python          %s" % sys.version.split()[0])
    print("  解释器           %s" % sys.executable)
    print("  在 venv 里       %s" % ("是" if FACTS["in_venv"] else "否"))
    print("  默认文本编码     %s" % FACTS["preferred_encoding"])
    print("  文件系统编码     %s" % FACTS["filesystem_encoding"])
    print("  仓库路径         %s" % ROOT)
    print("  路径含非 ASCII   %s" % ("是" if FACTS["repo_has_non_ascii"] else "否"))
    print("  bash             %s" % (FACTS["bash"] or "没找到（.sh 不能直接跑）"))
    print("  LibreOffice      %s" % (FACTS["soffice"] or "没找到（排版预览用不了）"))

    if FACTS["is_windows"]:
        try:
            import ctypes
            k = ctypes.windll.kernel32
            FACTS["console_cp"] = int(k.GetConsoleOutputCP())
            FACTS["ansi_cp"] = int(k.GetACP())
            print("  控制台代码页     %d" % FACTS["console_cp"])
            print("  系统 ANSI 代码页 %d" % FACTS["ansi_cp"])
        except Exception as ex:
            print("  代码页          读不到（%s）" % ex)

    # 解释器路径：这是 Windows 上第一个会踩的坑
    win_py = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    nix_py = os.path.join(ROOT, ".venv", "bin", "python")
    add("P1", ".venv 解释器路径",
        "PASS" if os.path.exists(win_py) or os.path.exists(nix_py) else "WARN",
        "Windows 版在 %s；POSIX 版在 %s" % (
            "有" if os.path.exists(win_py) else "无",
            "有" if os.path.exists(nix_py) else "无"))

    # 换行符：Git for Windows 默认 autocrlf=true，会把 .sh 检出成 CRLF
    sh = os.path.join(ROOT, "app", "run.sh")
    crlf = None
    if os.path.exists(sh):
        raw = open(sh, "rb").read()
        crlf = b"\r\n" in raw
    FACTS["run_sh_crlf"] = crlf
    add("E3", "run.sh 换行符",
        "WARN" if crlf else "PASS",
        "是 CRLF（bash 会报 $'\\r': command not found）" if crlf
        else "是 LF（正常）" if crlf is not None else "文件不在")

    for key, label in (("core.autocrlf", "git autocrlf"), ("core.quotepath", "git quotepath")):
        git = shutil.which("git")
        val = "（没装 git）"
        if git:
            try:
                val = subprocess.check_output([git, "config", "--get", key],
                                              cwd=ROOT, stderr=subprocess.DEVNULL
                                              ).decode("utf-8", "replace").strip() or "（未设置）"
            except Exception:
                val = "（未设置）"
        FACTS[key] = val
        print("  %-15s %s" % (label, val))
    return True


# ---------------------------------------------------------------- ② 测试 × 编码
def step_tests():
    hr("② 四套测试 × 三种输出编码环境")
    table = {}
    for mode, env, simulated, why in MODES:
        print("\n  ── %s：%s" % (mode, why))
        for suite in SUITES:
            path = os.path.join(APP, suite)
            if not os.path.exists(path):
                add("T-%s-%s" % (mode, suite), suite, "SKIP", "文件不在")
                continue
            r = run([sys.executable, path], env=env,
                    tag="%s_%s" % (mode, suite))
            enc = "UnicodeEncodeError" in (r["err"] or "")
            ok = r["rc"] == 0 and not enc
            table.setdefault(suite, {})[mode] = {"rc": r["rc"], "enc": enc}
            detail = "退出码 %s，耗时 %ss" % (r["rc"], r["seconds"])
            if enc:
                detail += "，★输出编码崩了：" + (tail(r["err"], 2).replace("\n", " ")[:160])
            print("      %-16s %s" % (suite, detail))
            RESULTS.append({"id": "T-%s-%s" % (mode, suite),
                            "name": "%s @ %s" % (suite, mode),
                            "status": "PASS" if ok else "FAIL",
                            "simulated": simulated,
                            "detail": detail,
                            "err_path": r["err_path"], "out_path": r["out_path"]})

    FACTS["tests"] = table

    # 结论：三种环境横向比
    hr("② 的结论")
    def all_ok(mode):
        return all(v["rc"] == 0 and not v["enc"]
                   for s in table.values() for m, v in s.items() if m == mode)
    default_ok = all_ok("默认")
    utf8_ok = all_ok("utf8")
    gbk_broken = not all_ok("cp936")
    if default_ok and utf8_ok:
        add("T-sum", "默认环境下测试就能跑", "PASS",
            "重定向时的编码问题已经由 app/console.py 自己解决，不需要设 PYTHONUTF8")
    elif utf8_ok and not default_ok:
        add("T-sum", "★默认环境会崩、UTF-8 环境正常", "FAIL",
            "结论：要设 PYTHONUTF8=1，或者补上 app/console.py 那种自动修复")
    elif not utf8_ok:
        add("T-sum", "UTF-8 环境下仍有失败", "FAIL",
            "不是编码问题，看 out/win-verify/raw/ 里的原始日志")
    if gbk_broken:
        add("T-gbk", "关掉修复 + 强制 GBK 会崩", "PASS",
            "机制复现：✓/✗ 在 GBK 下编不出来；关掉修复就复现，说明修复确实是它")
    return True


# ---------------------------------------------------------------- ③ 真服务 + 真接口
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(url, payload=None, timeout=20):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def step_server(port):
    hr("③ 起一次真服务，打几次真接口")
    data_dir = os.path.join(OUT, "data")
    env = {"NAIWOZHU_DATA_DIR": data_dir, "NO_BROWSER": "1",
           "PORT": str(port), "NAIWOZHU_CACHE_TTL": "0"}
    log = os.path.join(RAW, "server.log")
    proc = None
    try:
        with open(log, "wb") as f:
            proc = subprocess.Popen(
                [sys.executable, os.path.join(APP, "server.py"),
                 "--port", str(port), "--no-browser"],
                cwd=ROOT, env=dict(os.environ, **env), stdout=f, stderr=f)
        base = "http://127.0.0.1:%d" % port
        up = False
        for _ in range(60):                      # 最多等 30 秒
            time.sleep(0.5)
            if proc.poll() is not None:
                break
            try:
                code, body = http(base + "/api/status", timeout=3)
                if code == 200:
                    up = True
                    break
            except Exception:
                pass
        if not up:
            add("S0", "服务起得来", "FAIL",
                "启动失败或超时，见 " + os.path.relpath(log, ROOT) + "：" +
                tail(read(log), 4).replace("\n", " ")[:200])
            return False
        st = json.loads(body)
        FACTS["status"] = {k: st.get(k) for k in
                           ("demo", "configured", "has_token", "cache_ttl", "cache_age")}
        add("S0", "服务起得来", "PASS",
            "/api/status 200；configured=%s has_token=%s" %
            (st.get("configured"), st.get("has_token")))

        code, html = http(base + "/")
        new_ui = 'id="rangeSel"' in html and 'value="today"' in html
        add("S1", "网页是新版查询条", "PASS" if (code == 200 and new_ui) else "FAIL",
            "首页 %s，%s" % (code, "含时间下拉列表" if new_ui else "没有找到 rangeSel"))

        # 打开演示模式：这样下面的查询一次网络都不打，结果也稳定
        http(base + "/api/demo", {"on": True})

        code, body = http(base + "/api/query", {})
        d = json.loads(body)
        ok = code == 200 and d.get("ok") and "items" in d
        add("S2", "查全部（不限时间）", "PASS" if ok else "FAIL",
            "共 %s 项，range_label=%r" % (d.get("count"), d.get("range_label")))

        code, body = http(base + "/api/query", {"range": "today", "kind": "deadline"})
        d = json.loads(body)
        has_win = "range_label" in d and "window" in d
        ok = code == 200 and has_win and d.get("kind") == "deadline"
        add("S3", "★时间下拉列表真的在筛", "PASS" if ok else "FAIL",
            "range_label=%r window=%r 共 %s 项" %
            (d.get("range_label"), d.get("window"), d.get("count")))

        code, body = http(base + "/api/query",
                          {"range": "custom", "start": "2026-10-15", "end": "2026-10-21"})
        d = json.loads(body)
        add("S4", "自定义区间", "PASS" if code == 200 and d.get("window") else "FAIL",
            "window=%r" % d.get("window"))

        code, body = http(base + "/api/query", {"range": "custom",
                                               "start": "2026-10-21", "end": "2026-10-15"})
        add("S5", "反着填日期会被挡下", "PASS" if code == 400 else "FAIL",
            "HTTP %s %s" % (code, json.loads(body).get("error") if body else ""))

        code, body = http(base + "/api/query", {"kind": "作业"})
        add("S6", "不认识的问题类型被挡下", "PASS" if code == 400 else "FAIL",
            "HTTP %s" % code)

        code, body = http(base + "/api/query", {"text": "我还有哪些作业没交"})
        d = json.loads(body)
        add("S7", "一句话那条路还能用", "PASS" if code == 200 and d.get("parsed") else "FAIL",
            "解析=%s，共 %s 项" % ((d.get("parsed") or {}).get("reason"), d.get("count")))

        http(base + "/api/demo", {"on": False})
        return True
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()


# ---------------------------------------------------------------- ④ 中文字体
FONT_PROBE = r'''
import os, sys, json, warnings
import matplotlib
matplotlib.use("Agg")
from matplotlib import font_manager, ft2font

# 优先用项目自己的挑字体逻辑（src/fonts.py）——那才是真正生效的那份。
# 发布出去的仓库只有 app/ 和 tools/，没有 src/，这时退回一份等价实现。
SRC = os.path.join(os.getcwd(), "src")
F = None
if os.path.isdir(SRC):
    sys.path.insert(0, SRC)
    try:
        import fonts as F
    except Exception:
        F = None

CHARS = "奶我助工程学导论"

def covers(path):
    if F is not None:
        return F.covers(path, CHARS)
    try:
        f = ft2font.FT2Font(str(path))
        idx = [f.get_char_index(ord(c)) for c in CHARS]
    except Exception:
        return False
    if not all(idx):
        return False
    return len(set(idx)) >= max(2, len(CHARS) // 2)

def resolve(fam):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return font_manager.findfont(fam, fallback_to_default=True)

cands = list(F.CANDIDATES_SANS) if F is not None else [
    "Noto Sans CJK SC", "Microsoft YaHei", "SimHei", "SimSun"]
out = {
    "source": "src/fonts.py" if F is not None else "内置默认（这个仓库里没有 src/）",
    "configured": F.CJK if F is not None else "",
    "resolved": {}, "usable": [],
}
for fam in cands[:9]:
    p = resolve(fam)
    out["resolved"][fam] = {"path": os.path.basename(p), "covers": covers(p)}

if F is not None:
    cfg = resolve(out["configured"])
    out["configured_path"] = os.path.basename(cfg)
    out["configured_covers"] = covers(cfg)

seen = set()
for f in font_manager.fontManager.ttflist:
    if f.fname in seen:
        continue
    seen.add(f.fname)
    if covers(f.fname):
        out["usable"].append(os.path.basename(f.fname))
out["usable"] = sorted(set(out["usable"]))[:40]
print(json.dumps(out, ensure_ascii=False))
'''





def step_fonts():
    hr("④ 中文字体（图上中文变方框就是这里的问题）")
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        add("F1", "中文字体", "SKIP",
            "没装 matplotlib。装一次再跑：python -m pip install matplotlib")
        return False

    probe = os.path.join(RAW, "font_probe.py")
    with open(probe, "w", encoding="utf-8") as f:
        f.write(FONT_PROBE)
    r = run([sys.executable, probe], tag="font_probe")
    try:
        info = json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        add("F1", "中文字体探测", "FAIL",
            "探测脚本没输出可解析的结果：" + tail(r["err"] or r["out"], 4))
        return False

    FACTS["fonts"] = info
    have_src = info.get("source") == "src/fonts.py"

    if not have_src:
        # 只发布了 app/ + tools/ 的仓库里没有配图脚本，这项无从谈起
        add("F1", "★项目配置的字体能打汉字", "SKIP",
            "这份仓库里没有 src/（配图脚本不在这里），字体这项跳过；"
            "下面列出本机真能打汉字的字体供参考")
    else:
        cjk_ok = info.get("configured_covers")
        add("F1", "★项目配置的字体能打汉字", "PASS" if cjk_ok else "FAIL",
            "src/fonts.py 选中 %r（挑法：先按名字找、再验字形），实到 %s，汉字字形%s" %
            (info.get("configured"), info.get("configured_path"),
             "齐全" if cjk_ok else "★缺失（图上中文会变成方框）"))

    print("      各个候选字体在这台机器上实际落到哪个文件、能不能打汉字：")
    for fam, v in info["resolved"].items():
        print("        %-20s -> %-34s %s" % (fam, v["path"], "能" if v["covers"] else "缺字形"))

    usable = info.get("usable") or []
    add("F2", "这台机器上能打汉字的字体", "PASS" if usable else "WARN",
        "、".join(usable[:12]) + ("…" if len(usable) > 12 else "") if usable
        else "一个都没找到，说明系统没装中文字体")

    if not have_src or not info.get("configured"):
        add("F3", "样例配图生成", "SKIP", "同上：这份仓库里没有配图脚本，先不画")
        return True

    # 生成一张样例图，让肉眼看一眼
    png = os.path.join(OUT, "sample-figure.png")
    gen = os.path.join(RAW, "make_sample.py")
    with open(gen, "w", encoding="utf-8") as f:
        f.write(
            "import matplotlib\nmatplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\n"
            "plt.rcParams['font.family'] = %r\n"
            "plt.rcParams['axes.unicode_minus'] = False\n"
            "fig, ax = plt.subplots(figsize=(6, 2.2))\n"
            "ax.axis('off')\n"
            "ax.text(0.5, 0.68, '奶我助 · 第五章作业', ha='center', fontsize=22)\n"
            "ax.text(0.5, 0.30, '明天有什么日程 / 近 3 天 / 一周内', ha='center', fontsize=13)\n"
            "fig.savefig(%r, dpi=150, bbox_inches='tight')\n"
            "print('ok')\n" % (info["configured"], png))
    r2 = run([sys.executable, gen], tag="make_sample")
    ok = os.path.exists(png)
    add("F3", "样例配图生成", "PASS" if ok else "FAIL",
        "out/win-verify/sample-figure.png（打开看一眼中文是不是方框）" if ok
        else tail(r2["err"], 3))
    return True


# ---------------------------------------------------------------- ⑤ 文档生成
def step_docs():
    hr("⑤ 生成一份 Word（验证 python-docx 与中文路径）")
    try:
        import docx  # noqa: F401
    except ImportError:
        add("D1", "生成 Word", "SKIP",
            "没装 python-docx。装一次再跑：python -m pip install python-docx")
        return False
    src = os.path.join(ROOT, "assets", "文档", "奶我助_技术难点分析.md")
    if not os.path.exists(src):
        add("D1", "生成 Word", "SKIP", "输入文件不在：assets/文档/奶我助_技术难点分析.md")
        return False
    r = run([sys.executable, os.path.join(ROOT, "src", "md2docx.py")], tag="md2docx")
    dst = os.path.join(ROOT, "out", "奶我助_技术难点分析.docx")
    ok = r["rc"] == 0 and os.path.exists(dst) and os.path.getsize(dst) > 10000
    size = os.path.getsize(dst) if os.path.exists(dst) else 0
    add("D1", "★生成 Word", "PASS" if ok else "FAIL",
        "out/奶我助_技术难点分析.docx，%d 字节" % size if ok
        else "退出码 %s：%s" % (r["rc"], tail(r["err"], 3).replace("\n", " ")[:200]))
    if ok:
        try:
            import docx as _d
            doc = _d.Document(dst)
            texts = [p.text for p in doc.paragraphs if p.text.strip()]
            n_cjk = sum(1 for t in texts if any("\u4e00" <= c <= "\u9fff" for c in t))
            add("D2", "docx 里的中文读得回来", "PASS" if n_cjk else "WARN",
                "%d 段有文字，其中 %d 段含中文" % (len(texts), n_cjk))
        except Exception as e:
            add("D2", "docx 里的中文读得回来", "FAIL", str(e)[:120])
    return True


# ---------------------------------------------------------------- 汇总
def _bad(prefixes):
    return any(r["status"] == "FAIL" and r["id"].startswith(prefixes)
               for r in RESULTS if not r.get("simulated"))


def _skipped(rid):
    return any(r["id"] == rid and r["status"] == "SKIP" for r in RESULTS)


def _text(v):
    """判定文案可以是字符串，也可以是个函数（按前面的结果动态给话术）。"""
    return v() if callable(v) else v


# (名字, 有没有问题, 有问题的说法, 没问题的说法, 这一步在测什么)
VERDICTS = [
    ("能不能跑起来",
     lambda: _bad(("S",)),
     "跑不起来（看③的失败项）", "能跑（起服务/出页面/查数据都通）",
     "网页版核心链路在 Windows 上通不通"),
    ("要不要设 PYTHONUTF8",
     lambda: any(r["id"] == "T-sum" and r["status"] == "FAIL" for r in RESULTS),
     "要设：默认输出编码下测试会崩", "不用设：代码自己会切 UTF-8",
     "四套测试在默认（重定向）编码环境下会不会崩"),
    ("中文会不会变方框",
     lambda: any(r["id"] == "F1" and r["status"] == "FAIL" for r in RESULTS),
     "会：项目配置的字体缺汉字字形",
     lambda: ("未测：这份仓库里没有配图脚本（src/），字体这项跳过了"
              if _skipped("F1") else "不会：字体有汉字字形"),
     "配图里的中文会不会渲染成方框"),
]


def summary():
    hr("汇总")
    out = {}
    for name, fn, bad_txt, good_txt, why in VERDICTS:
        bad = fn()
        text = _text(bad_txt) if bad else _text(good_txt)
        print("  %-16s %s" % (name, text))
        print("      %s" % why)
        out[name] = {"bad": bad, "text": text}
    fails = [r for r in RESULTS if r["status"] == "FAIL" and not r.get("simulated")]
    sim = [r for r in RESULTS if r["status"] == "FAIL" and r.get("simulated")]
    print("\n  失败 %d 项（另有 %d 项模拟项失败，不计入），注意 %d 项，跳过 %d 项" % (
        len(fails), len(sim),
        len([r for r in RESULTS if r["status"] == "WARN"]),
        len([r for r in RESULTS if r["status"] == "SKIP"])))
    if fails:
        print("  失败清单：")
        for r in fails:
            print("    · %s —— %s" % (r["name"], r["detail"][:120]))
    print("\n  报告（贴回来给我看的就是这两份）：")
    print("    %s" % os.path.join(OUT, "verify-windows.log"))
    print("    %s" % os.path.join(OUT, "verify-windows.json"))
    return out, len(fails)


def write_report(verdicts):
    os.makedirs(OUT, exist_ok=True)
    log = os.path.join(OUT, "verify-windows.log")
    js = os.path.join(OUT, "verify-windows.json")

    lines = []
    lines.append("奶我助 · Windows 兼容性体检报告")
    lines.append("生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("")
    lines.append("== 环境事实 ==")
    for k, v in FACTS.items():
        if k == "tests" or k == "fonts":
            continue
        lines.append("  %-22s %s" % (k, v))
    lines.append("")
    lines.append("== 步骤结果 ==")
    for r in RESULTS:
        lines.append("  [%s] %-30s %s" % (r["status"], r["name"], r["detail"]))
    lines.append("")
    lines.append("== 判定 ==")
    for name, v in verdicts.items():
        lines.append("  %-16s %s" % (name, v["text"] if isinstance(v, dict) else v))
    lines.append("")
    lines.append("== 测试矩阵（suite → 编码环境 → 退出码/是否编码崩） ==")
    for suite, modes in (FACTS.get("tests") or {}).items():
        for mode, v in modes.items():
            lines.append("  %-16s %-8s rc=%s enc_error=%s" %
                         (suite, mode, v["rc"], v["enc"]))
    lines.append("")
    lines.append("== 字体 ==")
    f = FACTS.get("fonts") or {}
    if f:
        lines.append("  配置字体 %s -> %s（汉字%s）" %
                     (f.get("configured"), f.get("configured_path"),
                      "齐全" if f.get("configured_covers") else "缺失"))
        for fam, v in (f.get("resolved") or {}).items():
            lines.append("    %-20s -> %-34s %s" %
                         (fam, v["path"], "能" if v["covers"] else "缺字形"))
        lines.append("  能打汉字的字体：%s" % "、".join(f.get("usable") or []))
    lines.append("")

    # 失败项的原始输出尾部：判定问题就靠这几行
    fails = [r for r in RESULTS if r["status"] == "FAIL" and not r.get("simulated")]
    if fails:
        lines.append("== 失败项的原始输出（尾部若干行） ==")
        for r in fails:
            for p in (r.get("err_path"), r.get("out_path")):
                if not p or not os.path.exists(p):
                    continue
                txt = read(p)
                if not txt.strip():
                    continue
                lines.append("  --- %s ---" % os.path.relpath(p, ROOT))
                lines.extend("    " + x for x in tail(txt, 10).splitlines())
                break

    with open(log, "w", encoding="utf-8-sig", newline="\r\n") as fp:
        fp.write("\n".join(lines) + "\n")
    with open(js, "w", encoding="utf-8") as fp:
        json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "facts": FACTS, "results": RESULTS, "verdicts": verdicts},
                  fp, ensure_ascii=False, indent=2, default=str)
    return log, js


def main():
    ap = argparse.ArgumentParser(description="奶我助 · Windows 兼容性体检")
    ap.add_argument("--skip-server", action="store_true", help="跳过起服务那一步")
    ap.add_argument("--skip-docs", action="store_true", help="跳过生成 Word 那一步")
    ap.add_argument("--skip-tests", action="store_true", help="跳过四套测试")
    ap.add_argument("--port", type=int, default=0, help="服务端口，0=自动挑一个空的")
    args = ap.parse_args()

    os.makedirs(RAW, exist_ok=True)
    print("=" * 68)
    print("  奶我助 · Windows 兼容性体检")
    print("  仓库：%s" % ROOT)
    if os.name != "nt":
        print("  注意：当前不是 Windows。②里「默认」那一列反映的是**本机**编码")
        print("        （Linux 一般是 UTF-8，所以不会崩）；真正要看的是")
        print("        Windows 上跑出来的结果。其余各步在哪个系统上都成立。")
    print("=" * 68)

    step_env()
    if not args.skip_tests:
        step_tests()
    if not args.skip_server:
        try:
            step_server(args.port or free_port())
        except Exception as e:
            add("S9", "服务那一步", "FAIL", "脚本自己出错：%r" % (e,))
    if not args.skip_docs:
        try:
            step_docs()
        except Exception as e:
            add("D9", "生成 Word 那一步", "FAIL", "脚本自己出错：%r" % (e,))
    try:
        step_fonts()
    except Exception as e:
        add("F9", "字体那一步", "FAIL", "脚本自己出错：%r" % (e,))

    verdicts, nfail = summary()
    log, js = write_report(verdicts)
    print("\n  报告已写好：%s" % log)
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())

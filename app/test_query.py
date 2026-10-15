"""查询条件的用例。运行： .venv/bin/python app/test_query.py

这个文件同时是"答辩时可以现场跑"的证据：
说明"否定句/说法变化"这个难点是被系统性验证过的，不是拍脑袋写的规则。

两段：
  ① 一句话 → 条件（语音、命令行走这条）
  ② 下拉列表的时间选项 → 时间区间（网页走这条）
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime
from model import CST
from query import parse, resolve_range, span_from_days, RANGES

import console        # noqa: E402

console.setup()

# (输入, 是否应为查询, 期望天数, 期望课程)
CASES = [
    # ---- 正常查询 ----
    ("我还有哪些作业没交",        True,  None, None),
    ("明天有什么要交的",          True,  2,    None),
    ("后天有什么要交的",          True,  3,    None),
    ("这周还有什么没交",          True,  7,    None),
    ("下周有哪些作业",            True,  14,   None),
    ("高数有什么作业",            True,  None, "高数"),
    ("线代的作业还有哪些",        True,  None, "线代"),
    ("这周要交什么",              True,  7,    None),
    ("有什么截止的",              True,  None, None),
    ("查一下我的ddl",             True,  None, None),

    # ---- 否定/陈述句：不能去查数据 ----
    ("明天没什么要交的",          False, 2,    None),
    ("这周没有作业",              False, 7,    None),
    ("我作业都交了",              False, None, None),
    ("作业已经全部做完了",        False, None, None),
    ("没有作业了",                False, None, None),
    ("高数不用交作业",            False, None, "高数"),

    # ---- 无关输入：必须说"没听懂"，不能瞎答 ----
    ("今天天气不错",              False, None, None),
    ("你好",                      False, None, None),
    ("",                          False, None, None),
]


# ---- ② 下拉列表的时间选项 ----
#
# 固定一个"现在"（2025-10-15 周三 14:30），区间才可复现；
# 否则每天跑出来的期望值都不一样。
NOW = datetime(2025, 10, 15, 14, 30, tzinfo=CST)

# (选项 id, 开始日期, 结束日期, 期望区间 或 "ERR")
RANGE_CASES = [
    ("all",      None,         None,         (None, None)),
    ("",         None,         None,         (None, None)),
    ("today",    None,         None,         ("2025-10-15 00:00", "2025-10-16 00:00")),
    ("tomorrow", None,         None,         ("2025-10-16 00:00", "2025-10-17 00:00")),
    # 近 3 天 = 今天 + 后天，止于第 4 天 00:00
    ("3d",       None,         None,         ("2025-10-15 00:00", "2025-10-18 00:00")),
    ("7d",       None,         None,         ("2025-10-15 00:00", "2025-10-22 00:00")),
    # 自定义含首尾两天：到 10-07 就要止于 10-08 00:00
    ("custom",   "2025-10-01", "2025-10-07", ("2025-10-01 00:00", "2025-10-08 00:00")),
    ("custom",   "2025-10-03", "2025-10-03", ("2025-10-03 00:00", "2025-10-04 00:00")),
    ("custom",   "2025-10-07", "2025-10-01", "ERR"),      # 反过来填
    ("custom",   None,         None,         "ERR"),      # 没填日期
    ("custom",   "不是日期",    "2025-10-01", "ERR"),
    ("下个月",    None,         None,         "ERR"),      # 不在列表里的选项
]

RANGE_LABELS_EXPECTED = {
    "all": "不限时间", "today": "今天", "tomorrow": "明天",
    "3d": "近 3 天", "7d": "一周内", "custom": "自定义",
}


def range_main():
    """跑 ②。返回 (通过, 失败)。"""
    from query import range_label, span_label
    passed = failed = 0
    print(f"\n{'选项':<12}{'起':<12}{'止':<12}结果")
    print("-" * 68)
    for rid, start, end, want in RANGE_CASES:
        errs = []
        got = None
        try:
            span = resolve_range(rid, start, end, now=NOW)
            got = tuple(d.strftime("%Y-%m-%d %H:%M") for d in span) if span else (None, None)
            if want == "ERR":
                errs.append("本该报错却算出来了")
        except ValueError:
            got = "ValueError"
            if want != "ERR":
                errs.append("本该算出来却报错")
        if not errs and want != "ERR" and got != want:
            errs.append(f"区间应为{want}实为{got}")
        ok = not errs
        passed += ok
        failed += not ok
        mark = "✓" if ok else "✗ " + "；".join(errs)
        print(f"{(rid or '(空)'):<12}{str(start or '—'):<12}{str(end or '—'):<12}{mark}")

    print("\n选项名字")
    for rid, label in RANGES:
        ok = range_label(rid) == label == RANGE_LABELS_EXPECTED.get(rid)
        passed += ok
        failed += not ok
        print(f"  {'✓' if ok else '✗'} {rid:<10}{label}")

    print("\n一句话问出来的时间窗（从现在起算，不是从今天 0 点）")
    span = span_from_days(7, now=NOW)
    ok = span is not None and span[0] == NOW
    passed += ok
    failed += not ok
    print(f"  {'✓' if ok else '✗'} 未来 7 天：{span_label(span)}")
    ok = span_from_days(None) is None
    passed += ok
    failed += not ok
    print(f"  {'✓' if ok else '✗'} 没提时间词 → 不限时间")
    return passed, failed


def parse_main():
    """跑 ①。返回 (通过, 失败)。"""
    passed = failed = 0
    print(f"{'输入':<22}{'类型':<8}{'天数':<7}{'课程':<7}结果")
    print("-" * 68)
    for text, want_q, want_d, want_c in CASES:
        r = parse(text)
        errs = []
        if r["is_query"] != want_q:
            errs.append(f"是否查询应为{want_q}实为{r['is_query']}")
        if want_q and r["days"] != want_d:
            errs.append(f"天数应为{want_d}实为{r['days']}")
        if want_q and r["course_kw"] != want_c:
            errs.append(f"课程应为{want_c}实为{r['course_kw']}")
        ok = not errs
        passed += ok
        failed += not ok
        kind = "查询" if r["is_query"] else "不查"
        mark = "✓" if ok else "✗ " + "；".join(errs)
        print(f"{text or '(空)':<22}{kind:<8}{str(r['days'] or '—'):<7}"
              f"{str(r['course_kw'] or '—'):<7}{mark}")
    return passed, failed


def main():
    print("① 一句话 → 条件")
    p1, f1 = parse_main()
    print("-" * 68)
    print(f"通过 {p1} / {len(CASES)}，失败 {f1}")

    print("\n② 下拉列表的时间选项 → 时间区间（现在是 {:%Y-%m-%d %H:%M}）"
          .format(NOW))
    p2, f2 = range_main()
    print("-" * 68)
    print(f"通过 {p2}，失败 {f2}")

    print(f"\n合计通过 {p1 + p2} 项，失败 {f1 + f2} 项")
    return 1 if (f1 or f2) else 0


if __name__ == "__main__":
    sys.exit(main())

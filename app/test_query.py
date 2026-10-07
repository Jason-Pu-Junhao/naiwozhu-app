"""查询解析器的用例。运行： .venv/bin/python app/test_query.py

这个文件同时是"答辩时可以现场跑"的证据：
说明"否定句/说法变化"这个难点是被系统性验证过的，不是拍脑袋写的规则。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from query import parse

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


def main():
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
    print("-" * 68)
    print(f"通过 {passed} / {len(CASES)}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

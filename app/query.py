"""把一句话解析成查询条件。

第一步（网页版）先用纯规则做；第二步加语音时，
把输入从"文本框"换成"识别结果"，这个模块原样复用。

这里刻意不做大模型：指令只有几条，规则写清楚就够，而且完全可控。
"""
import re

# ---- 时间词 → 天数窗口 ----
TIME_WORDS = [
    (r"今天|今日", 1),
    (r"明天|明日", 2),
    (r"后天", 3),
    (r"这周|本周|这个星期|本星期", 7),
    (r"下周|下个星期", 14),
    (r"一个月|这个月|本月", 31),
]

# ---- 否定/陈述句式 ----
#
# 这一步比看上去难：中文里"没"字到处都是。
# 「作业没交」是查询（"没交"＝尚未提交），
# 「明天没什么要交的」才是陈述。
# 所以不能只匹配"没"，要匹配"表陈述"的完整说法。
NEGATION_PATTERNS = [
    r"没(?:有)?(?:什么|啥|任何).{0,8}(?:要交|作业|deadline|ddl|截止|课)",
    r"没有(?:作业|任务|deadline|ddl)",
    # 允许"已经全部""全部都"这类副词连用
    r"(?:都|全部|已经|全|统统){1,3}.{0,4}(?:交|做|写|完成)(?:完)?了",
    r"(?:作业|任务|事情|ddl|deadline).{0,6}(?:都|已经|全部|全).{0,6}"
    r"(?:交|做|写|完成)(?:完)?了",
    r"不(?:用|需要|必)(?:交|写|做)",
]

# ---- 查询意图关键词（必须先出现这些才认为是查询）----
QUERY_WORDS = [
    "作业", "任务", "deadline", "ddl", "截止", "要交", "没交", "未交",
    "还没交", "哪些", "多少", "哪几", "什么",
]

# 常见课程简称。可按自己的课表改。
COURSE_ALIASES = {
    "高数": ["高等数学", "高数"],
    "线代": ["线性代数", "线代"],
    "大物": ["大学物理", "大物"],
    "程设": ["程序设计", "程设"],
    "工导": ["工程学导论", "工导"],
    "英语": ["大学英语", "综合英语", "英语"],
    "思修": ["思想道德", "思修"],
    "近代史": ["近代史", "近现代史"],
}


def extract_course(low):
    for alias in COURSE_ALIASES:
        if alias in low:
            return alias
    return None


def parse(text):
    """把一句话解析成查询条件。

    返回 {'is_query', 'negated', 'days', 'course_kw', 'reason'}
    """
    t = (text or "").strip()
    if not t:
        return {"is_query": False, "negated": False, "days": None,
                "course_kw": None, "reason": "空输入"}

    low = t.lower().replace(" ", "")

    negated = any(re.search(p, low) for p in NEGATION_PATTERNS)
    has_query_word = any(w in low for w in QUERY_WORDS)

    days = None
    matched_time = None
    for pat, d in TIME_WORDS:
        m = re.search(pat, low)
        if m:
            days = d
            matched_time = m.group(0)
            break

    course_kw = extract_course(low)

    # 陈述句：不查数据
    if negated:
        return {"is_query": False, "negated": True, "days": days,
                "course_kw": course_kw,
                "reason": "这句是陈述（含否定），我不去查数据"
                          + (f"；顺带提到“{matched_time}”" if matched_time else "")}

    # 必须出现真正的查询词，避免"今天天气不错"被误当成查作业
    if has_query_word:
        return {"is_query": True, "negated": False, "days": days,
                "course_kw": course_kw, "reason": "识别为查询"}

    if course_kw:
        return {"is_query": True, "negated": False, "days": days,
                "course_kw": course_kw, "reason": f"按课程“{course_kw}”查询"}

    return {"is_query": False, "negated": False, "days": None,
            "course_kw": None, "reason": "没听懂，请换一种说法"}


HELP_TEXT = """我可以回答这几类问题：
  · 我还有哪些作业没交
  · 明天有什么要交的
  · 这周还有什么没交
  · 高数有什么作业
  （否定句会被当成陈述，比如「明天没什么要交的」不会去查数据）
"""

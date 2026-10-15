"""把各种输入规范化成同一组查询条件。

两条输入路径，出口是同一个：

    网页上的下拉列表   → resolve_range()：预设区间 / 自定义起止日期
    一句话（语音、CLI）→ parse()：时间词 + 类型意图 + 课程简称 + 否定句

网页那条路**不经过文字解析**：时间是从下拉列表里选的，不靠猜。
等第二步接上语音，识别出来的文字再走 parse()，条件凑齐后交给同一套筛选。

这里刻意不做大模型：指令只有几条，规则写清楚就够，而且完全可控。
"""
import re
from datetime import datetime, timedelta

from model import CST

# ---- 时间范围：网页下拉列表的选项 ----
#
# 这份清单同时是三个地方的依据：网页上照着它渲染下拉框、
# 服务端照着它解析、测试再回头核对两边一字不差。改这里就是改全局。
RANGES = [
    ("all",      "不限时间"),
    ("today",    "今天"),
    ("tomorrow", "明天"),
    ("3d",       "近 3 天"),
    ("7d",       "一周内"),
    ("custom",   "自定义"),
]
RANGE_LABELS = dict(RANGES)


def day_start(dt):
    """某一天在东八区的 00:00。"""
    d = dt.astimezone(CST)
    return datetime(d.year, d.month, d.day, tzinfo=CST)


def parse_day(s):
    """"2025-10-15" 这类日期 → 那一天的 00:00；解析不了返回 None。"""
    import model
    dt = model.parse_dt(s)
    return day_start(dt) if dt is not None else None


def resolve_range(range_id, start=None, end=None, now=None):
    """把下拉列表选的时间变成半开区间 (起, 止)。

    "all" → None，表示不加时间限制（"我还有哪些作业没交"就是这一档）。
    "custom" 用 start/end 两个日期，**含首尾两天**。
    选项不认识、或者自定义的日期不成立时抛 ValueError，由调用方转成 400，
    让人看到哪里填错了；悄悄返回一个空列表反而更难查。
    """
    rid = (range_id or "").strip()
    if rid in ("", "all"):
        return None
    today = day_start(now or datetime.now(CST))
    one = timedelta(days=1)
    preset = {
        "today":    (today, today + one),          # 今天 00:00 ~ 明天 00:00
        "tomorrow": (today + one, today + 2 * one),
        "3d":       (today, today + 3 * one),      # 今天、明天、后天
        "7d":       (today, today + 7 * one),      # 今天起七天
    }
    if rid in preset:
        return preset[rid]
    if rid == "custom":
        s, e = parse_day(start), parse_day(end)
        if s is None or e is None:
            raise ValueError("自定义范围要填开始和结束日期")
        if e < s:
            raise ValueError("结束日期不能早于开始日期")
        return (s, e + one)
    raise ValueError(f"不认识的时间范围：{range_id}")


def span_from_days(days, now=None):
    """一句话问出来的"未来 N 天"，也折成半开区间。

    语音/CLI 走这条：它说的是"从现在起 N 天"，讲的是离现在多少天，
    所以起点是此刻，"今天 00:00"反而是另一套说法。
    """
    if days is None:
        return None
    t = now or datetime.now(CST)
    return (t, t + timedelta(days=days))


def range_label(range_id):
    """下拉列表选项给人看的名字。"""
    return RANGE_LABELS.get((range_id or "").strip(), "")


def span_label(span):
    """区间的给人看写法。区间是半开的，排版时把右端减一秒再说。"""
    if span is None:
        return ""
    s = span[0].astimezone(CST)
    e = (span[1] - timedelta(seconds=1)).astimezone(CST)
    if s.date() == e.date():
        return f"{s:%m-%d}"
    return f"{s:%m-%d} ~ {e:%m-%d}"


# ---- 时间词 → 天数窗口（只给一句话那条路用）----
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
    "还没交", "哪些", "多少", "哪几", "什么", "日程", "安排", "会议",
    "活动", "讲座", "考试",
]

# ---- 类型意图：问的是「截止」还是「日程」 ----
#
# 两类都出现、或都没出现时返回 None，表示"两种都列出来"。
DEADLINE_WORDS = ["作业", "deadline", "ddl", "截止", "要交", "没交", "未交",
                  "提交"]
SCHEDULE_WORDS = ["日程", "会议", "活动", "讲座", "课", "考试", "报名"]


def detect_kind(low):
    """返回 'deadline' / '日程' / None（两种都看）。"""
    has_d = any(w in low for w in DEADLINE_WORDS)
    has_s = any(w in low for w in SCHEDULE_WORDS)
    if has_d and not has_s:
        return "deadline"
    if has_s and not has_d:
        return "日程"
    return None

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
    """把一句话解析成查询条件（语音、命令行走这条；网页不走这里）。

    返回 {'is_query', 'negated', 'days', 'course_kw', 'want_kind', 'reason'}

    want_kind 为 'deadline' / '日程' / None（两种都列出来）。
    days 交给 span_from_days() 折成时间区间。
    """
    t = (text or "").strip()
    if not t:
        return {"is_query": False, "negated": False, "days": None,
                "course_kw": None, "want_kind": None, "reason": "空输入"}

    low = t.lower().replace(" ", "")

    negated = any(re.search(p, low) for p in NEGATION_PATTERNS)
    has_query_word = any(w in low for w in QUERY_WORDS)
    want_kind = detect_kind(low)

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
                "course_kw": course_kw, "want_kind": want_kind,
                "reason": "这句是陈述（含否定），我不去查数据"
                          + (f"；顺带提到“{matched_time}”" if matched_time else "")}

    # 必须出现真正的查询词，避免"今天天气不错"被误当成查作业
    if has_query_word:
        return {"is_query": True, "negated": False, "days": days,
                "course_kw": course_kw, "want_kind": want_kind,
                "reason": "识别为查询"}

    if course_kw:
        return {"is_query": True, "negated": False, "days": days,
                "course_kw": course_kw, "want_kind": want_kind,
                "reason": f"按课程“{course_kw}”查询"}

    return {"is_query": False, "negated": False, "days": None,
            "course_kw": None, "want_kind": None, "reason": "没听懂，请换一种说法"}


HELP_TEXT = """我可以回答这几类问题：
  · 我还有哪些作业没交          （只问「截止」）
  · 明天有什么要交的
  · 这周还有什么没交
  · 高数有什么作业
  · 我明天有什么日程            （只问「日程」）
  · 下周有哪些课和考试
  · 这周有什么安排              （两种都列出来）
  （否定句会被当成陈述，比如「明天没什么要交的」不会去查数据）
"""

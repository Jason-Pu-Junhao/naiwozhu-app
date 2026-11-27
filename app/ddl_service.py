"""业务逻辑：把 Canvas 的数据整理成「我还没交的作业」列表。

这里是整个项目第一步的核心，网页和命令行都调它。
"""
import re
from datetime import datetime, timezone, timedelta
from unicodedata import normalize

from canvas_client import CanvasError
from model import (KIND_DEADLINE, SRC_CANVAS,
                   CST, parse_dt as _parse_dt, remaining as _remaining,
                   make_deadline)
from query import COURSE_ALIASES

# Canvas 的 submission 里，这些 workflow_state 表示"已交/已判"
DONE_STATES = {"submitted", "graded", "pending_review", "complete"}


def _search_key(text):
    """统一全半角、空白和作业序号（第 3 次 / 第三次）。"""
    text = re.sub(r"\s+", "", normalize("NFKC", text).casefold())
    digits = "零一二三四五六七八九"

    def ordinal(match):
        number = match[1]
        tens, sep, ones = number.partition("十")
        value = (digits.index(tens or "一") * 10 + digits.index(ones or "零")
                 if sep else digits.index(tens))
        return f"第{value}"

    return re.sub(r"第([一二三四五六七八九]?十[一二三四五六七八九]?|[一二三四五六七八九])",
                  ordinal, text)


def _submission_status(assignment):
    """查单项状态时，只有明确的提交证据才能报已提交。"""
    sub = assignment.get("submission")
    if not isinstance(sub, dict) or sub.get("excused") is True:
        return "unknown"
    state = sub.get("workflow_state")
    if _parse_dt(sub.get("submitted_at")) or state in ("submitted", "pending_review"):
        return "submitted"
    types = assignment.get("submission_types") or []
    if any(kind in types for kind in ("on_paper", "none")):
        return "unknown"
    if state == "unsubmitted" or sub.get("missing") is True:
        return "unsubmitted"
    return "unknown"


def _is_done(assignment):
    """判断本人是否已经提交了这个作业。"""
    sub = assignment.get("submission") or {}
    if not sub:
        # 没带提交信息时，保守地认为"未交"
        return False
    if sub.get("excused"):
        return True          # 免修，不算待办
    if sub.get("workflow_state") in DONE_STATES:
        return True
    if sub.get("submitted_at"):
        return True
    return False


def _norm(rec):
    """统一成内部结构。

    来源是 Canvas，所以类型固定记作 deadline——这一步不猜。
    """
    return make_deadline(
        rec["id"], rec["title"], rec["due_at"], SRC_CANVAS,
        course="",                       # 课程名由调用方用课程表补上
        html_url=rec.get("html_url") or "",
        sources=rec["sources"],
        extra={"course_id": rec["course_id"]},
    )


class DDLService:
    def __init__(self, client, verbose=False):
        self.c = client
        self.verbose = verbose
        self._course_cache = None
        # 这一轮里哪些课程没取到（网络抖动、被移出课程、403……）。
        # 调用方据此判断"这次是不是一次完整的抓取"：只有完整的才该
        # 覆盖已存的缓存，否则一次部分失败会把别的课程从缓存里抹掉。
        self.skipped = []

    # ------------------------------------------------------------ 课程名
    def course_map(self):
        if self._course_cache is None:
            m = {}
            for c in self.c.courses():
                m[c["id"]] = c.get("name") or c.get("course_code") or f"课程{c['id']}"
            self._course_cache = m
        return self._course_cache

    def assignment_matches(self, course_kw, title_kw):
        """查匹配课程的全部作业，保留已交、无截止时间和同名项。"""
        course_key = _search_key(course_kw)
        course_keys = next((names for names in COURSE_ALIASES.values() if course_key in names),
                           [course_key])
        title_key = _search_key(title_kw)
        matches = {}
        for course_id, course in self.course_map().items():
            course_name = _search_key(course)
            if not any(key in course_name for key in course_keys):
                continue
            # 任一匹配课程取数失败时，让调用方回答无法确认，避免遗漏同名项。
            for assignment in self.c.course_assignments(course_id):
                if not isinstance(assignment, dict):
                    raise CanvasError("Canvas 返回的作业格式有误")
                title = assignment.get("name") or ""
                if title_key not in _search_key(title):
                    continue
                assignment_id = assignment["id"]
                due = _parse_dt(assignment.get("due_at"))
                matches[course_id, assignment_id] = {
                    "id": assignment_id,
                    "course_id": course_id,
                    "course": course,
                    "title": title,
                    "due_at": due.astimezone(CST).isoformat() if due else None,
                    "status": _submission_status(assignment),
                }
        return sorted(matches.values(), key=lambda item: (
            item["course"], item["title"], item["due_at"] or "",
            str(item["course_id"]), str(item["id"])))

    # ------------------------------------------------------------ 取数
    def _from_todo(self):
        """路径 A：官方待办接口（type=submitting 即"待提交"）。"""
        out = []
        for t in self.c.todo():
            if t.get("type") != "submitting":
                continue
            a = t.get("assignment") or {}
            if not a:
                continue
            out.append({
                "id": a.get("id"),
                "title": a.get("name") or "(未命名作业)",
                "course_id": a.get("course_id"),
                "due_at": _parse_dt(a.get("due_at")),
                "html_url": a.get("html_url") or "",
                "sources": {"待办接口"},
            })
        return out

    def _from_courses(self):
        """路径 B：遍历课程逐条查作业，这是待办为空时的替代路径。"""
        out = []
        cmap = self.course_map()
        for cid in list(cmap.keys()):
            try:
                assignments = self.c.course_assignments(cid)
            except Exception as e:                # noqa: BLE001
                self.skipped.append({"course_id": cid,
                                     "course": cmap.get(cid, ""),
                                     "error": f"{type(e).__name__}: {e}"[:200]})
                if self.verbose:
                    print(f"    [跳过] 课程 {cid}: {e}")
                continue
            for a in assignments:
                if _is_done(a):
                    continue
                out.append({
                    "id": a.get("id"),
                    "title": a.get("name") or "(未命名作业)",
                    "course_id": cid,
                    "due_at": _parse_dt(a.get("due_at")),
                    "html_url": a.get("html_url") or "",
                    "sources": {"逐课程查询"},
                })
        return out

    # ------------------------------------------------------------ 主入口
    def pending(self, days=None, course_kw=None, include_overdue=True,
                show_all=False):
        """汇总"还没交的作业"。

        days        只保留未来 N 天内到期（None = 不限）
        course_kw   只保留课程名包含该关键词的
        include_overdue  是否包含已过期的
        show_all    显示全部（默认只显示有截止时间的）
        """
        pooled = {}

        def add(rec):
            key = rec["id"]
            if key in pooled:
                pooled[key]["sources"] |= rec["sources"]
            else:
                pooled[key] = rec

        for rec in self._from_todo():
            add(rec)
        for rec in self._from_courses():
            add(rec)

        cmap = self.course_map()
        items = []
        for rec in pooled.values():
            item = _norm(rec)
            item["course"] = cmap.get(rec["course_id"], f"课程{rec['course_id']}")

            if course_kw and course_kw not in item["course"]:
                continue

            due = rec["due_at"]
            if due is None and not show_all:
                continue
            if due is not None:
                now = datetime.now(timezone.utc)
                if not include_overdue and due < now:
                    continue
                if days is not None:
                    if due > now + timedelta(days=days):
                        continue
            items.append(item)

        # 按截止时间排序；无截止时间的排最后
        items.sort(key=lambda x: (x["due_at"] is None, x["due_at"] or ""))
        return items

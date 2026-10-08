"""业务逻辑：把 Canvas 的数据整理成「我还没交的作业」列表。

这里是整个项目第一步的核心，网页和命令行都调它。
"""
from datetime import datetime, timezone, timedelta

from model import (KIND_DEADLINE, SRC_CANVAS,
                   parse_dt as _parse_dt, remaining as _remaining,
                   make_deadline)

# Canvas 的 submission 里，这些 workflow_state 表示"已交/已判"
DONE_STATES = {"submitted", "graded", "pending_review", "complete"}


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

    # ------------------------------------------------------------ 课程名
    def course_map(self):
        if self._course_cache is None:
            m = {}
            for c in self.c.courses():
                m[c["id"]] = c.get("name") or c.get("course_code") or f"课程{c['id']}"
            self._course_cache = m
        return self._course_cache

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
            except Exception as e:
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

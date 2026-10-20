"""用模拟数据验证取数与汇总逻辑。

重点覆盖"最容易静默出错"的两件事：
  1. 翻页 —— 漏翻会少拿作业，程序却不报错
  2. 去重 —— todo 与逐课程查询会返回同一条作业

运行： .venv/bin/python app/test/test_ddl.py
"""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ddl_service import DDLService, _is_done, _remaining

CST = timezone(timedelta(hours=8))
NOW = datetime.now(timezone.utc)


def iso(**kw):
    return (NOW + timedelta(**kw)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- 假客户端
class FakeResp:
    def __init__(self, data, headers=None):
        self._d = data
        self.headers = headers or {}
        self.status_code = 200

    def json(self):
        return self._d


from canvas_client import CanvasClient, CanvasError

import console        # noqa: E402

console.setup()


class FakeClient(CanvasClient):
    """继承真客户端、只覆写 _get。

    这样 _paginate / _next_link 用的是**生产代码**，
    翻页逻辑真的被测到了，测的是生产代码本身。
    """

    def __init__(self):
        self.base = "https://example.invalid"
        self.verbose = False
        self.calls = []
        # 故意不调用父类 __init__，避免真的建 Session

    def _get(self, url, params=None):
        self.calls.append(url)
        if "/api/v1/courses" in url:
            # 第一页带 next 链接，第二页不带 —— 模拟真实翻页
            if "page=2" in url:
                return FakeResp([{"id": 2, "name": "工程学导论"}], {})
            return FakeResp(
                [{"id": 1, "name": "高等数学"}],
                {"Link": '<https://example.invalid/api/v1/courses?page=2>; rel="next"'},
            )
        raise AssertionError(f"未预期的请求：{url}")

    # 以下方法绕过 _get，直接给数据（因为它们的返回值结构不同）
    def me(self):
        return {"id": 9, "name": "测试同学", "login_id": "test"}

    def todo(self):
        from datetime import datetime, timezone, timedelta
        NOW = datetime.now(timezone.utc)
        iso = lambda **kw: (NOW + timedelta(**kw)).isoformat().replace("+00:00", "Z")
        return [
            {"type": "submitting",
             "assignment": {"id": 101, "name": "高数作业三", "course_id": 1,
                            "due_at": iso(days=1), "html_url": "u/101"}},
            {"type": "submitting",
             "assignment": {"id": 102, "name": "阅读文献", "course_id": 2,
                            "due_at": iso(days=5), "html_url": "u/102"}},
            {"type": "grading",
             "assignment": {"id": 999, "name": "已交的作业", "course_id": 1,
                            "due_at": iso(days=2)}},
        ]

    def course_assignments(self, course_id):
        from datetime import datetime, timezone, timedelta
        NOW = datetime.now(timezone.utc)
        iso = lambda **kw: (NOW + timedelta(**kw)).isoformat().replace("+00:00", "Z")
        if course_id == 1:
            return [
                {"id": 101, "name": "高数作业三", "due_at": iso(days=1),
                 "submission": {}, "html_url": "u/101"},
                {"id": 103, "name": "高数作业四", "due_at": iso(days=9),
                 "submission": {}, "html_url": "u/103"},
                {"id": 104, "name": "已交作业", "due_at": iso(days=3),
                 "submission": {"workflow_state": "submitted",
                                "submitted_at": iso(days=-1)}},
                {"id": 105, "name": "免修作业", "due_at": iso(days=4),
                 "submission": {"excused": True}},
                {"id": 106, "name": "过期未交", "due_at": iso(days=-2),
                 "submission": {}, "html_url": "u/106"},
                {"id": 107, "name": "没写截止时间",
                 "due_at": None, "submission": {}},
            ]
        return [
            {"id": 102, "name": "阅读文献", "due_at": iso(days=5),
             "submission": {}, "html_url": "u/102"},
        ]


# ---------------------------------------------------------------- 断言
def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  {detail}" if detail and not cond else ""))
    return bool(cond)


def main():
    svc = DDLService(FakeClient())
    ok = True

    print("翻页")
    courses = svc.course_map()
    ok &= check("课程跨两页都被取到", len(courses) == 2, f"实际 {courses}")
    ok &= check("课程名正确", courses.get(1) == "高等数学" and courses.get(2) == "工程学导论")

    print("\n汇总与去重")
    items_def = svc.pending()                 # 默认：不显示无截止时间的
    items_all = svc.pending(show_all=True)    # --all：显示全部
    ids_def = [i["id"] for i in items_def]
    ids_all = [i["id"] for i in items_all]

    ok &= check("默认排除没写截止时间的", 107 not in ids_def, f"ids={ids_def}")
    ok &= check("--all 时把它带上", 107 in ids_all, f"ids={ids_all}")
    ok &= check("已交的被排除", 104 not in ids_all)
    ok &= check("免修的被排除", 105 not in ids_all)
    ok &= check("批改类待办不算待交", 999 not in ids_all)
    ok &= check("重复项被合并（101 只出现一次）", ids_all.count(101) == 1)
    ok &= check("只在逐课程里出现的也被抓到", 103 in ids_all, f"ids={ids_all}")
    ok &= check("过期的默认包含", 106 in ids_all)

    items = items_all

    print("\n来源标注")
    it101 = next(i for i in items if i["id"] == 101)
    it103 = next(i for i in items if i["id"] == 103)
    ok &= check("101 标注了两个来源",
                set(it101["sources"]) == {"待办接口", "逐课程查询"},
                str(it101["sources"]))
    ok &= check("103 只有逐课程来源",
                it103["sources"] == ["逐课程查询"], str(it103["sources"]))

    print("\n排序（按截止时间）")
    dated = [i for i in items if i["due_at"]]
    ok &= check("按截止时间升序",
                dated == sorted(dated, key=lambda x: x["due_at"]),
                str([i["due_at"] for i in dated]))

    print("\n无截止时间的排最后")
    last = items_all[-1]["id"]
    ok &= check("无截止时间的排在列表末尾", last == 107, f"最后一条是 {last}")

    print("\n时间窗口过滤")
    d2 = svc.pending(days=2, show_all=True)
    ok &= check("days=2 只留 2 天内的", all(
        i["due_at"] is None or i["due_at"] <= (NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z")
        for i in d2), f"ids={[i['id'] for i in d2]}")

    print("\n课程过滤")
    dg = svc.pending(course_kw="高数", show_all=True)
    ok &= check("course_kw=高数 全部命中", all("高数" in i["course"] for i in dg),
                str([i["course"] for i in dg]))

    print("\n过期判定")
    rem, over = _remaining(NOW - timedelta(hours=3))
    ok &= check("过期 3 小时被识别", over and "小时" in rem, rem)
    rem, over = _remaining(NOW + timedelta(minutes=30))
    ok &= check("30 分钟后到期被识别", (not over) and "分钟" in rem, rem)
    rem, over = _remaining(None)
    ok &= check("无截止时间不报错", rem == "—")

    # ---- 回归：网络失败与非 JSON 响应都必须变成 CanvasError ----
    # 真机上踩过：Canvas 连不上时抛的是 requests.ConnectionError，
    # 漏到 Flask 就变成 500 的 HTML 页，前端只显示"JSON.parse 失败"。
    print("\n连不上 / 返回的不是 JSON（都必须是 CanvasError）")
    import requests

    real = CanvasClient("https://example.invalid", "fake-token")

    def boom(*a, **kw):
        raise requests.ConnectionError("Failed to establish a new connection")

    real.s.get = boom
    try:
        real._get("/api/v1/users/self/todo")
        ok &= check("连接失败抛的是 CanvasError", False, "居然没抛")
    except CanvasError as e:
        ok &= check("★连接失败会包成 CanvasError", "连不上" in str(e), str(e)[:50])
    except Exception as e:
        ok &= check("★连接失败会包成 CanvasError", False, f"抛的是 {type(e).__name__}")

    class HtmlResp:
        status_code, headers, text = 200, {}, "<html>请先登录</html>"

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    real2 = CanvasClient("https://example.invalid", "fake-token")
    real2.s.get = lambda *a, **kw: HtmlResp()
    try:
        list(real2._paginate("/api/v1/users/self/todo"))
        ok &= check("非 JSON 响应抛的是 CanvasError", False, "居然没抛")
    except CanvasError as e:
        ok &= check("★非 JSON 响应会包成 CanvasError",
                    "不是 JSON" in str(e) and "请先登录" in str(e), str(e)[:60])
    except Exception as e:
        ok &= check("★非 JSON 响应会包成 CanvasError", False, f"抛的是 {type(e).__name__}")

    print("\n" + "=" * 50)
    print("全部通过 ✓" if ok else "有失败项 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

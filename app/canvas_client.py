"""Canvas REST API 客户端。

只做三件事：认证、翻页、取数据。业务逻辑不放在这里。
"""
import re
import requests

TIMEOUT = 25
# 每页取大一点，减少翻页次数；Canvas 一般允许到 100
PER_PAGE = 100


class CanvasError(RuntimeError):
    pass


class CanvasClient:
    def __init__(self, base, token, verbose=False):
        self.base = base.rstrip("/")
        self.verbose = verbose
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "nailwozhu-ddl/0.1 (course project)",
        })

    # ---------------------------------------------------------------- 底层
    def _get(self, url, params=None):
        if not url.startswith("http"):
            url = self.base + url
        r = self.s.get(url, params=params, timeout=TIMEOUT)
        if r.status_code == 401:
            raise CanvasError(
                "401 未授权：令牌无效或已撤销。请重新在 "
                f"{self.base}/profile/settings 生成一个。"
            )
        if r.status_code == 403:
            raise CanvasError(f"403 无权限访问 {url}")
        if r.status_code >= 400:
            raise CanvasError(f"HTTP {r.status_code} 访问 {url}\n{r.text[:300]}")
        return r

    @staticmethod
    def _next_link(resp):
        """从 Link 头里取 rel="next" 的地址。

        注意：若用查询参数传令牌，返回的链接不含该参数，需要自己补。
        本项目用 Authorization 头，所以不存在这个问题。
        """
        link = resp.headers.get("Link", "")
        if not link:
            return None
        for part in link.split(","):
            m = re.search(r'<([^>]+)>\s*;\s*rel="next"', part)
            if m:
                return m.group(1)
        return None

    def _paginate(self, path, params=None):
        """生成器：自动跟随 Link 头翻页，逐条产出对象。"""
        params = dict(params or {})
        params.setdefault("per_page", PER_PAGE)
        url = self.base + path
        page = 0
        while url:
            page += 1
            r = self._get(url, params if page == 1 else None)
            data = r.json()
            if self.verbose:
                print(f"    [翻页] 第 {page} 页，{len(data)} 条")
            for item in data:
                yield item
            url = self._next_link(r)

    # ---------------------------------------------------------------- 接口
    def me(self):
        return self._get("/api/v1/users/self").json()

    def courses(self, include_term=True):
        params = {"enrollment_state": "active"}
        if include_term:
            params["include[]"] = "term"
        return list(self._paginate("/api/v1/courses", params))

    def todo(self):
        """官方待办：已包含 type=submitting（待提交）与 grading。"""
        return list(self._paginate("/api/v1/users/self/todo"))

    def course_assignments(self, course_id):
        """某课程的全部作业，含本人提交状态。"""
        return list(self._paginate(
            f"/api/v1/courses/{course_id}/assignments",
            {"include[]": "submission", "order_by": "due_at"},
        ))

    def upcoming_events(self):
        return list(self._paginate("/api/v1/users/self/upcoming_events"))

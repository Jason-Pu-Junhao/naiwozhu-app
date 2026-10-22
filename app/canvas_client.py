"""Canvas REST API 客户端。

只做三件事：认证、翻页、取数据。业务逻辑不放在这里。
"""
import re
import requests

TIMEOUT = 25
# 连接和读取分开设：连不上要快点认输（树莓派连着没有出口的热点时，SYN 会被直接丢掉，
# 那种情况下 25 秒的连接超时等于把整个页面卡住）。读则给足时间——翻页时
# 一次要读几十条作业，慢一点是正常的。
CONNECT_TIMEOUT = 5
READ_TIMEOUT = TIMEOUT
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
    @staticmethod
    def _json(r):
        """把响应体当 JSON 解；不是 JSON 就抛 CanvasError。

        真实会遇到：网络里插了个登录页或代理错误页，返回的是 HTML。
        直接 r.json() 抛出的 ValueError 会一路穿到 Flask，变成 500 的 HTML，
        前端只能显示一句"JSON.parse: unexpected character"，真正的原因全被遮住。
        """
        try:
            return r.json()
        except ValueError:
            raise CanvasError(
                f"返回的不是 JSON（HTTP {r.status_code}）。前 200 字符：{r.text[:200]!r}"
            )

    def _get(self, url, params=None):
        if not url.startswith("http"):
            url = self.base + url
        try:
            r = self.s.get(url, params=params,
                           timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        except requests.RequestException as e:
            # 连不上、超时、代理不可用都走这里。必须包成 CanvasError：
            # 调用方只认这一种异常，漏出去就会变成 500 的 HTML 页。
            raise CanvasError(
                f"连不上 {self.base}。先查网络：网断了、校园网没连上、"
                f"或者终端里设了 HTTPS_PROXY 指向一个没开的代理。"
                f"（原始错误：{str(e)[:200]}）"
            )
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
            data = self._json(r)
            if self.verbose:
                print(f"    [翻页] 第 {page} 页，{len(data)} 条")
            for item in data:
                yield item
            url = self._next_link(r)

    # ---------------------------------------------------------------- 接口
    def me(self):
        return self._json(self._get("/api/v1/users/self"))

    def profile(self):
        """个人信息。学号（login_id）只有这个接口里有，me() 里是 null。"""
        return self._json(self._get("/api/v1/users/self/profile"))

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

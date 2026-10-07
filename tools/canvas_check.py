#!/usr/bin/env python3
"""Canvas 接口可用性实测脚本 —— “奶我助” 项目第 1 步验证

用法：
    # 1) 去 https://oc.sjtu.edu.cn/profile/settings 生成访问令牌（页面底部）
    # 2) 把令牌写进环境变量（不要直接写进代码！）
    export CANVAS_TOKEN='你生成的令牌'
    python3 canvas_check.py

它会依次检查：
    1. 令牌是否有效（我是谁）
    2. 能拿到几门课（对比你实际在上的门数）
    3. /users/self/todo 是否返回内容（这是最关键的一项）
    4. 近期日程
    5. 分页 Link 头（决定要不要实现翻页）
    6. 逐课程查作业（todo 为空时的替代路径）

只读操作，不会修改 Canvas 上的任何数据。
"""
import os
import sys
import json

try:
    import requests
except ImportError:
    sys.exit("请先安装 requests： pip install requests")

BASE = os.environ.get("CANVAS_BASE", "https://oc.sjtu.edu.cn")
TOKEN = os.environ.get("CANVAS_TOKEN", "").strip()

if not TOKEN:
    sys.exit(
        "没有找到令牌。\n"
        "请先执行： export CANVAS_TOKEN='你的令牌'\n"
        "令牌生成方法：登录 oc.sjtu.edu.cn → 头像 → 设置 →\n"
        "页面底部“已批准的访问许可” → “+ 新建访问许可证” → 生成 → 立刻复制"
    )

H = {"Authorization": f"Bearer {TOKEN}"}
TIMEOUT = 25
ok, warn, fail = [], [], []


def show(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def get(path, **params):
    r = requests.get(BASE + path, headers=H, params=params, timeout=TIMEOUT)
    return r


# ---------------------------------------------------------------- 1
show("1. 令牌是否有效")
r = get("/api/v1/users/self")
if r.status_code == 200:
    me = r.json()
    print(f"  ✓ 登录成功：{me.get('name')}  (id={me.get('id')})")
    print(f"    学号/登录名: {me.get('login_id')}")
    ok.append("令牌有效")
elif r.status_code == 401:
    fail.append("令牌无效或已撤销（401）")
    sys.exit("  ✗ 401：令牌无效。请重新生成一个。")
else:
    fail.append(f"未预期状态码 {r.status_code}")
    print(f"  ✗ HTTP {r.status_code}")
    print("   ", r.text[:300])
    sys.exit(1)

# ---------------------------------------------------------------- 2
show("2. 能拿到几门课")
r = get("/api/v1/courses", per_page=100,
        enrollment_state="active", include=["term"])
if r.status_code == 200:
    courses = r.json()
    print(f"  ✓ 返回 {len(courses)} 门课程")
    for c in courses[:15]:
        term = (c.get("term") or {}).get("name", "")
        print(f"     - [{c.get('id')}] {c.get('name')}  {c.get('course_code','')} {term}")
    if len(courses) > 15:
        print(f"     ... 还有 {len(courses)-15} 门")
    print("\n  ⚠ 请核对：这个数字和你实际在上的门数一致吗？")
    print("     少了很多 → 说明有权限或 enrollment_state 参数问题")
    ok.append(f"课程 {len(courses)} 门")
    # 分页
    link = r.headers.get("Link", "")
    print(f"\n  Link 头: {link[:200] if link else '（无）'}")
    if 'rel="next"' in link:
        warn.append("存在分页，必须实现翻页否则会漏作业")
        print("  ⚠ 有 rel=\"next\" → 课程列表被打断了，代码里必须翻页")
else:
    fail.append(f"课程接口 {r.status_code}")
    print(f"  ✗ HTTP {r.status_code}: {r.text[:200]}")

# ---------------------------------------------------------------- 3
show("3. 待办接口 /users/self/todo （最关键）")
r = get("/api/v1/users/self/todo", per_page=100)
if r.status_code == 200:
    todo = r.json()
    print(f"  返回 {len(todo)} 条待办")
    if len(todo) == 0:
        warn.append("/users/self/todo 为空 —— 需改用逐课程查询")
        print("  ⚠ 是 0 条！两种可能：")
        print("     (a) 你确实没有待办（先去 Canvas 首页确认一下）")
        print("     (b) 交大实例改写了默认行为 → 必须改用第 6 步的替代路径")
    else:
        ok.append(f"todo {len(todo)} 条")
        for t in todo[:5]:
            a = t.get("assignment") or {}
            print(f"     - [{t.get('type')}] {a.get('name')}")
            print(f"       截止: {a.get('due_at')}   课程id: {a.get('course_id')}")
        print("\n  ✓ 内容结构（第一条完整 JSON）:")
        print(json.dumps(todo[0], ensure_ascii=False, indent=2)[:900])
else:
    fail.append(f"todo 接口 {r.status_code}")
    print(f"  ✗ HTTP {r.status_code}: {r.text[:200]}")

# ---------------------------------------------------------------- 4
show("4. 近期日程 /users/self/upcoming_events")
r = get("/api/v1/users/self/upcoming_events")
if r.status_code == 200:
    ev = r.json()
    print(f"  ✓ 返回 {len(ev)} 条日程")
    for e in ev[:5]:
        print(f"     - {e.get('title')}  start={e.get('start_at')}  end={e.get('end_at')}")
    ok.append(f"日程 {len(ev)} 条")
else:
    print(f"  ✗ HTTP {r.status_code}: {r.text[:200]}")

# ---------------------------------------------------------------- 5
show("5. 分页机制验证（用一个较大的 per_page 对比）")
r1 = get("/api/v1/courses", per_page=5)
n1 = len(r1.json()) if r1.status_code == 200 else -1
link = r1.headers.get("Link", "")
print(f"  per_page=5  → 实际返回 {n1} 条")
print(f"  Link: {link[:200] if link else '（无）'}")
if n1 == 5 and 'rel="next"' in link:
    ok.append("分页机制正常，需实现翻页")
    print("  ✓ 确认：默认会被截断，必须跟 Link 头翻页")
else:
    print("  （未观察到分页，或课程数不足 5）")

# ---------------------------------------------------------------- 6
show("6. 替代路径：逐课程查作业（todo 为空时用这个）")
if r.status_code == 200 or True:
    rc = get("/api/v1/courses", per_page=100, enrollment_state="active")
    courses = rc.json() if rc.status_code == 200 else []
    total = 0
    for c in courses[:12]:
        ra = get(f"/api/v1/courses/{c['id']}/assignments", per_page=100,
                 order_by="due_at")
        if ra.status_code != 200:
            print(f"  ✗ [{c['id']}] {c.get('name')}: HTTP {ra.status_code}")
            continue
        asg = ra.json()
        total += len(asg)
        pend = [a for a in asg if not (a.get("has_submitted_submissions"))]
        print(f"  [{c['id']}] {str(c.get('name'))[:34]:<34} 作业 {len(asg):>3} 条")
        for a in asg[:2]:
            print(f"        · {str(a.get('name'))[:44]:<44} due={a.get('due_at')}")
    print(f"\n  合计扫描到 {total} 条作业")
    if total == 0:
        warn.append("逐课程也没查到作业 —— 可能课程未使用 Canvas 作业功能")
    else:
        ok.append(f"逐课程合计 {total} 条作业")

# ---------------------------------------------------------------- 汇总
show("汇总")
print("  通过：")
for x in ok or ["（无）"]:
    print("    ✓", x)
print("  注意：")
for x in warn or ["（无）"]:
    print("    ⚠", x)
print("  失败：")
for x in fail or ["（无）"]:
    print("    ✗", x)

print("""
下一步：
  · 如果第 3 步有内容 → 直接用它做数据源，最省事
  · 如果第 3 步是 0 但第 6 步有内容 → 用逐课程查询做数据源
  · 如果两步都空 → 检查是不是这学期课程都还没布置作业，
    或改用日历订阅（/feeds/calendars/<feed_code>）
  · 把这些输出留一份，写进项目报告的“接口验证”部分
""")
print("提示：测完后建议回 Canvas 撤销这个令牌，需要时再生成。")

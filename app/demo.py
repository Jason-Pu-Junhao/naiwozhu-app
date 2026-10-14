# -*- coding: utf-8 -*-
"""演示数据：给第一阶段网页用的"随时能演"的假数据。

为什么需要它
    真的数据是空的。实测下来 Canvas 上 5 门课的作业**全部已提交**、交我办日程
    也还没导入过，所以网页打开就是「没有找到符合条件的条目」。功能是对的，
    但没法演示，也没法在没网、没登录的时候给人看。

为什么要按当前时间现算
    演示数据的日期**必须相对"现在"生成**。写死 2026-10-09 的话，
    过两天再演示，"今天要交"就变成"早就过期"了，整个页面反而更像坏的。
    所以每次调用都拿 now 重新算一遍。

为什么只把开关落盘、条目本身不落盘
    开关状态存在 app/data/demo.json，演示条目每次现生成。
    这样演示数据**永远不可能混进真实数据文件**（calendar.json / manual.json），
    把 app/data/ 删掉就等于彻底复原。

演示模式在服务端的语义（见 server.py）
    **用演示数据顶替 Canvas**：开着时不联网、不调 Canvas，
    本机数据（交我办导入快照 + 手动录入）照常参与。
    一句话能讲清，而且录入一条就能立刻在页面上看到。
"""
from datetime import datetime, timedelta

from model import CST, make_deadline, make_schedule, sort_items

SRC_DEMO_CANVAS = "演示数据 · Canvas"
SRC_DEMO_CAL = "演示数据 · 交我办"

# 一句话横幅，前端和接口都用它，避免两处各写一份说法
BANNER = "演示模式：以下数据是编的，没有联网，也不会写进你的真实记录。"


def _at(base, days, hour, minute=0):
    """取 base 之后第 days 天的某个整点时刻（东八区）。"""
    d = (base + timedelta(days=days)).astimezone(CST)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _due(base, days):
    """作业截止时间统一落在当天 23:59。

    直接拿 now 加几天会带出 19:24 这种零碎分钟，一眼就看出是机器生成的。
    真实的作业截止时间几乎都是深夜整点，落成 23:59 更像真的。
    """
    return _at(base, days, 23, 59)


def items(now=None):
    """生成一份演示数据。

    覆盖了两类日期（截止 / 日程）、三种时间状态（已过期、今天/进行中、将来），
    以及窗口边界（有一条落在「这周」之外，用来验证时间窗过滤真的在起作用）。
    """
    now = now or datetime.now(CST)
    out = []

    # 「今天要交」的那一条：取今天 23:59。深夜演示时它已经贴脸了，
    # 这时改成 2 小时后，免得刚生成就显示成"已过期"。
    due_today = _due(now, 0)
    if due_today <= now + timedelta(minutes=30):
        due_today = now + timedelta(hours=2)

    # ---------------- 截止（deadline）：模拟 Canvas 那一侧 ----------------
    out.append(make_deadline(
        "demo-dl-1", "第五章作业", _due(now, -2), SRC_DEMO_CANVAS,
        course="程序设计思想与方法（C++）",
        html_url="",
        sources=["演示数据"],
        extra={"demo": True},
    ))
    out.append(make_deadline(
        "demo-dl-2", "项目建议书终稿", due_today, SRC_DEMO_CANVAS,
        course="工程学导论",
        sources=["演示数据"],
        extra={"demo": True},
    ))
    out.append(make_deadline(
        "demo-dl-3", "读书报告", _due(now, 3), SRC_DEMO_CANVAS,
        course="马克思主义基本原理",
        sources=["演示数据"],
        extra={"demo": True},
    ))
    out.append(make_deadline(
        "demo-dl-4", "实验报告 3", _due(now, 9), SRC_DEMO_CANVAS,
        course="大学物理实验（2）",
        sources=["演示数据"],
        extra={"demo": True},
    ))

    # ---------------- 日程：模拟交我办日历那一侧 ----------------
    out.append(make_schedule(
        "demo-sc-1", "高等数学",
        _at(now, 1, 8, 0), _at(now, 1, 9, 40), SRC_DEMO_CAL,
        location="上院100", calendar="2026秋 课程表", course_like=True,
        sources=["演示数据"], extra={"demo": True},
    ))
    out.append(make_schedule(
        "demo-sc-2", "小组会议",
        _at(now, 2, 14, 0), _at(now, 2, 15, 30), SRC_DEMO_CAL,
        location="闵行校区", calendar="我的日程",
        sources=["演示数据"], extra={"demo": True},
    ))
    out.append(make_schedule(
        "demo-sc-3", "新生讲座：如何用好图书馆",
        _at(now, 4, 19, 0), _at(now, 4, 20, 30), SRC_DEMO_CAL,
        location="包玉刚图书馆", calendar="我的日程",
        sources=["演示数据"], extra={"demo": True},
    ))
    out.append(make_schedule(
        "demo-sc-4", "班级团建",
        _at(now, 6, 0, 0), None, SRC_DEMO_CAL,
        all_day=True, calendar="班级活动",
        sources=["演示数据"], extra={"demo": True},
    ))
    # 已经结束的一条：用来验证"日程结束后就不再列出来"
    out.append(make_schedule(
        "demo-sc-5", "已结束的组会",
        now - timedelta(days=1), now - timedelta(days=1) + timedelta(hours=1),
        SRC_DEMO_CAL, location="电院3号楼",
        sources=["演示数据"], extra={"demo": True},
    ))

    return sort_items(out)

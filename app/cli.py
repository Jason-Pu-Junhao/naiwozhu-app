#!/usr/bin/env python3
"""“奶我助” 第一步 · 命令行版

用法：
    .venv/bin/python app/cli.py                      # 查全部未交作业
    .venv/bin/python app/cli.py "明天有什么要交的"      # 用自然语言问
    .venv/bin/python app/cli.py --days 7             # 只看 7 天内
    .venv/bin/python app/cli.py --all                # 连没写截止时间的也显示
    .venv/bin/python app/cli.py --save-token         # 保存令牌到本地配置
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from canvas_client import CanvasClient, CanvasError
from ddl_service import DDLService
import query as q

C_RED, C_YEL, C_GRN, C_DIM, C_END = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[0m"


def ask_token(args):
    cfg = config.load()
    if args.save_token:
        import getpass
        print("去这里生成令牌：")
        print(f"  {cfg['base']}/profile/settings  → 页面底部「已批准的访问许可」")
        print("  → 「+ 新建访问许可证」→ 生成 → 立刻复制\n")
        tok = getpass.getpass("粘贴令牌（不回显）：").strip()
        if not tok:
            sys.exit("没输入，取消。")
        path = config.save_token(tok, cfg["base"])
        os.chmod(path, 0o600)
        print(f"\n已保存到 {path}（权限 600，仅本人可读）")
        print("提示：这个文件不要提交进 Git。")
        sys.exit(0)

    if not cfg["token"]:
        sys.exit(
            "没有找到访问令牌。两种方式任选：\n"
            "  1) 保存到本地配置（推荐一次）：\n"
            "     .venv/bin/python app/cli.py --save-token\n"
            "  2) 临时用环境变量：\n"
            "     export CANVAS_TOKEN='你的令牌'\n\n"
            "令牌生成方法：登录 oc.sjtu.edu.cn → 头像 → 设置 → 页面底部"
            "「已批准的访问许可」→「+ 新建访问许可证」→ 生成 → 立刻复制"
        )
    return cfg


def render(items, parsed):
    if not items:
        print(f"\n{C_GRN}没有找到符合条件的待交作业。{C_END}")
        if parsed and parsed.get("negated"):
            print(f"（本来就没查：{parsed['reason']}）")
        return
    print(f"\n共 {len(items)} 项：\n")
    for i, it in enumerate(items, 1):
        tag = f"{C_RED}已过期{C_END}" if it["overdue"] else f"{C_GRN}{it['remaining']}{C_END}"
        print(f"  {i:>2}. {it['due_local']}  {tag}")
        print(f"      {it['title']}")
        print(f"      {C_DIM}{it['course']}　来源: {'、'.join(it['sources'])}{C_END}")
        if it["html_url"]:
            print(f"      {C_DIM}{it['html_url']}{C_END}")
    print()


def main():
    ap = argparse.ArgumentParser(description="奶我助 · 待交作业查询（第一步·命令行版）")
    ap.add_argument("text", nargs="*", help="自然语言提问，例如：明天有什么要交的")
    ap.add_argument("--days", type=int, default=None, help="只看未来 N 天内")
    ap.add_argument("--all", action="store_true", help="连没设截止时间的也显示")
    ap.add_argument("--course", default=None, help="按课程关键词过滤")
    ap.add_argument("--no-overdue", action="store_true", help="不显示已过期的")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印翻页过程")
    ap.add_argument("--save-token", action="store_true", help="保存令牌到本地配置")
    ap.add_argument("--whoami", action="store_true", help="只验证令牌，打印身份")
    args = ap.parse_args()

    cfg = ask_token(args)

    try:
        client = CanvasClient(cfg["base"], cfg["token"], verbose=args.verbose)
        if args.whoami:
            me = client.me()
            print(f"令牌有效：{me.get('name')}（{me.get('login_id')}）")
            return 0

        text = " ".join(args.text).strip()
        parsed = None
        days, course = args.days, args.course

        if text:
            parsed = q.parse(text)
            print(f"\n{C_DIM}你的问题：{text}")
            print(f"解析结果：{parsed['reason']}{C_END}")
            if not parsed["is_query"]:
                print(f"\n{C_YEL}这句我没当成查询，所以不去查数据。{C_END}")
                print(q.HELP_TEXT)
                return 0
            if parsed["days"] is not None and days is None:
                days = parsed["days"]
            if parsed["course_kw"] and course is None:
                course = parsed["course_kw"]
            if days is not None:
                print(f"{C_DIM}时间范围：未来 {days} 天内{C_END}")
            if course:
                print(f"{C_DIM}课程过滤：{course}{C_END}")

        svc = DDLService(client, verbose=args.verbose)
        items = svc.pending(
            days=days, course_kw=course,
            include_overdue=not args.no_overdue, show_all=args.all,
        )
        render(items, parsed)
        return 0

    except CanvasError as e:
        print(f"\n{C_RED}{e}{C_END}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())

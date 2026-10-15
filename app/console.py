# -*- coding: utf-8 -*-
"""把重定向后的输出编码固定成 UTF-8。

为什么需要
    Windows 上 stdout **被重定向到文件或管道**时，Python 用的是"系统 ANSI
    代码页"（中文系统上是 936/GBK），跟控制台代码页不是一回事。测试里那些
    `✓ ✗ ⚠` 在 GBK 里没有对应字节，于是一行 print 直接抛
    UnicodeEncodeError——进程退出码变成 1，看起来像"测试失败"，其实是打印挂了。

在真机上量到过
    Windows 11 / 系统 ANSI 代码页 936 / 控制台代码页 65001：
    四套测试重定向后全部崩在 `print(f"  ✓ {name}")` 上，退出码 1；
    设 PYTHONUTF8=1 后四套全绿。

修法
    **只在输出不是终端时**把编码换成 UTF-8。真在终端里跑时一个字都不动——
    Windows 终端走的是 WriteConsoleW（PEP 528），本来就没有这个问题，
    硬改反而会让中文变乱码。

调试用
    设 NAIWOZHU_NO_UTF8=1 可以关掉这个修复，用来复现原始问题。
"""
import os
import sys

ENV_OFF = "NAIWOZHU_NO_UTF8"


def setup():
    """在脚本入口调用一次。重复调用无副作用。"""
    if os.environ.get(ENV_OFF):
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                # 3.7+ 才有 reconfigure；老版本或流被替换过就算了，不影响主流程
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


if __name__ == "__main__":        # 手动看一眼当前判断
    setup()
    print("stdout.isatty() =", sys.stdout.isatty())
    print("stdout.encoding =", sys.stdout.encoding)
    print("汉字与记号 ✓ ✗ ⚠ 都能打出来")

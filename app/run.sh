#!/usr/bin/env bash
# 一键起「奶我助」本地网页版。
#
#   bash app/run.sh                 # 默认 5173 端口，自动开浏览器
#   bash app/run.sh --port 5199     # 换端口
#   bash app/run.sh --no-browser    # 不自动开浏览器
#
# 只监听本机 127.0.0.1，访问令牌只存在你自己电脑上。按 Ctrl+C 退出。
set -e
cd "$(dirname "$0")/.."

# 解释器位置两边不一样：POSIX 在 .venv/bin/，Windows 在 .venv\Scripts\
# （Git Bash 里也是后者）。挨个试，谁在就用谁。
PY=""
for cand in ".venv/Scripts/python.exe" ".venv/bin/python" ".venv/bin/python3"; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  cat <<'EOF'
没有找到 .venv（项目自带的 Python 环境）。

先建一个，只需要做一次：

    Linux / macOS / Git Bash:
        python3 -m venv .venv
        .venv/bin/pip install -r app/requirements.txt

    Windows（直接用 PowerShell 那份脚本更省事）:
        py -3 -m venv .venv
        .venv\Scripts\python.exe -m pip install -r app\requirements.txt
        .\app\run.ps1

然后重新跑这个脚本。
EOF
  exit 1
fi

exec "$PY" app/server.py "$@"

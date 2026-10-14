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

PY=".venv/bin/python"

if [ ! -x "$PY" ]; then
  cat <<'EOF'
没有找到 .venv（项目自带的 Python 环境）。

先建一个，只需要做一次：

    python3 -m venv .venv
    .venv/bin/pip install -r app/requirements.txt

然后重新跑这个脚本。
EOF
  exit 1
fi

exec "$PY" app/server.py "$@"

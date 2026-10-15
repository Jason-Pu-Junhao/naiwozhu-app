#!/usr/bin/env bash
# 一键跑全部测试
#   Linux / macOS / Git Bash:  bash app/run_tests.sh
#   Windows 原生:              .\app\run.ps1 -Tests
set -e
cd "$(dirname "$0")/.."

# 解释器位置两边不一样：POSIX 在 .venv/bin/，Windows 在 .venv\Scripts\
PY=""
for cand in ".venv/Scripts/python.exe" ".venv/bin/python" ".venv/bin/python3"; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "没有找到 .venv。先建一个："
  echo "    Linux / Git Bash:  python3 -m venv .venv && .venv/bin/pip install -r app/requirements.txt"
  echo "    Windows:           py -3 -m venv .venv 然后 .venv\\Scripts\\python.exe -m pip install -r app\\requirements.txt"
  exit 1
fi

echo "=========== 查询解析用例 ==========="
"$PY" app/test_query.py
echo
echo "=========== 取数/翻页/去重验证 ==========="
"$PY" app/test_ddl.py
echo
echo "=========== 交我办导入 + 两种日期类型 ==========="
"$PY" app/test_store.py
echo
echo "=========== 网页服务：接口 / 演示模式 / 缓存 ==========="
"$PY" app/test_server.py

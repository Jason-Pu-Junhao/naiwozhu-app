#!/usr/bin/env bash
# 一键跑全部测试
set -e
cd "$(dirname "$0")/.."
echo "=========== 查询解析用例 ==========="
.venv/bin/python app/test_query.py
echo
echo "=========== 取数/翻页/去重验证 ==========="
.venv/bin/python app/test_ddl.py

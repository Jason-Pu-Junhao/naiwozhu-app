#!/usr/bin/env bash
# 一键跑全部测试
set -e
cd "$(dirname "$0")/.."
echo "=========== 查询解析用例 ==========="
.venv/bin/python app/test_query.py
echo
echo "=========== 取数/翻页/去重验证 ==========="
.venv/bin/python app/test_ddl.py
echo
echo "=========== 交我办导入 + 两种日期类型 ==========="
.venv/bin/python app/test_store.py

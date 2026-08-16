#!/usr/bin/env bash
# 尾盘打法看板启动脚本（部署于 PVE 213 / VM 104）
cd "$(dirname "$0")"
PORT="${PORT:-8140}"
echo "启动尾盘打法看板 @ http://0.0.0.0:${PORT}"
exec python3 app.py

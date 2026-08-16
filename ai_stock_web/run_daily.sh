#!/bin/bash
# 盘后每日选股：抓行情+打分 → watch_pool.json（Web 自动读取，无需重启）
cd "$HOME/ai_stock_web" || exit 1
export PATH="$HOME/.local/bin:$PATH"
echo "[$(date '+%F %T')] 开始每日选股..."
python3 build_pool.py -n 0 --top 30 -o watch_pool.json >> build.log 2>&1
echo "[$(date '+%F %T')] 完成"

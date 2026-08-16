#!/bin/bash
# 每日盘后：从104拉最新龙虎榜 → 全量选股打分 → 推送结果回104
set -e
cd /home/ai/ai_stock_web
export PATH="$HOME/.local/bin:$PATH"
LOG=build.log
echo "===== $(date '+%F %T') 开始 =====" >> $LOG
# 1. 拉最新龙虎榜数据
mkdir -p data
sshpass -p '1000' scp -o StrictHostKeyChecking=no 'z99777@192.168.0.203:/opt/longhubang-server/data/*.json' data/ >> $LOG 2>&1 || echo "  龙虎榜拉取失败(沿用已有)" >> $LOG
# 2. 全量选股打分（只保留最好的3只）
python3 build_pool.py -n 0 --top 3 -o watch_pool.json >> $LOG 2>&1
# 3. LLM 定性（一句话逻辑/赛道/风险）
python3 llm_report.py >> $LOG 2>&1 || echo "  LLM定性失败" >> $LOG
# 4. 推送结果到 104
sshpass -p '1000' scp -o StrictHostKeyChecking=no watch_pool.json z99777@192.168.0.203:~/ai_stock_web/watch_pool.json >> $LOG 2>&1
sshpass -p '1000' scp -o StrictHostKeyChecking=no llm_report.json z99777@192.168.0.203:~/ai_stock_web/llm_report.json >> $LOG 2>&1 || true
echo "===== $(date '+%F %T') 完成 =====" >> $LOG

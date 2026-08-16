#!/bin/bash
# 用户级启动脚本（无 sudo 环境）
cd "$HOME/ai_stock_web" || exit 1
export PATH="$HOME/.local/bin:$PATH"
pkill -f 'python3 app.py' 2>/dev/null
sleep 1
nohup python3 app.py >> app.log 2>&1 &
echo "started PID $!"
sleep 2

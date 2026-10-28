#!/bin/bash
# A股短线雷达 - 开机自启脚本
# 用法：先 export 好环境变量，再 nohup bash startup.sh > /tmp/astock-startup.log 2>&1 &

# 环境变量从 ~/.bashrc 或外部 export 读取：
#   FEISHU_APP_ID, FEISHU_APP_SECRET, DEEPSEEK_API_KEY

cd /home/user/Doubao/chats/38449645803520258/astock

# 启动 bot WebSocket 服务
if ! pgrep -f "bot_ws.py" > /dev/null; then
    nohup python3 -B bot_ws.py > /tmp/astock-bot.log 2>&1 &
    echo "$(date) bot_ws started (PID $!)"
fi

# 启动盘中扫描 daemon（仅在交易日工作时间内）
if ! pgrep -f "scan_and_push.py --daemon" > /dev/null; then
    python3 -B -c "
from datetime import datetime
from zoneinfo import ZoneInfo
now = datetime.now(ZoneInfo('Asia/Shanghai'))
is_weekday = now.weekday() < 5
is_trading_hours = 9*60+30 <= now.hour*60+now.minute <= 15*60+5
if is_weekday and is_trading_hours:
    print('trading time, starting scan daemon')
else:
    print(f'not trading time (weekday={is_weekday}, minutes={now.hour*60+now.minute}), skip scan')
    exit(0)
" && nohup python3 -B scan_and_push.py --daemon > /tmp/astock-scan.log 2>&1 &
    echo "$(date) scan_daemon started"
fi

echo "$(date) startup done"

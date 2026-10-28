#!/usr/bin/env python3
"""盘中买卖点扫描 + 飞书私信推送包装脚本。

由豆包定时任务在交易日 09:35 调用（--daemon 模式），进程内每 5 分钟
扫描一次，直到 15:00 收盘。只有信号状态变化时才发飞书私信并提交。

单次模式（不带 --daemon）：只扫描一次，用于调试。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parent
TARGET_CHAT_ID = "oc_4a44bf793473ba5dd87d50904065a92f"
SHANGHAI = ZoneInfo("Asia/Shanghai")
SCAN_INTERVAL = 300  # 5 分钟


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True, timeout=120)


def scan_once() -> bool:
    """执行一次扫描，有信号并成功推送返回 True。"""
    scan = run([sys.executable, "-B", "server.py", "--lightclaw-scan-prepare"])
    try:
        result = json.loads(scan.stdout.strip().splitlines()[-1] if scan.stdout.strip() else "{}")
    except (json.JSONDecodeError, IndexError):
        print(f"[{datetime.now(SHANGHAI):%H:%M}] 扫描输出无法解析：{scan.stdout[-300:]}", file=sys.stderr)
        return False

    status = result.get("status", "")
    if status != "prepared":
        reason = result.get("reason", result.get("event_id", ""))
        print(f"[{datetime.now(SHANGHAI):%H:%M}] {status}: {reason}")
        return False

    message = result.get("message", "")
    token = result.get("commit_token", "")
    if not message or not token:
        print("prepared 状态但缺少 message 或 commit_token", file=sys.stderr)
        return False

    # 根据信号类型选择卡片颜色
    msg_lower = message.lower()
    if "买入" in message or "buy" in msg_lower or "开仓" in message:
        header_color = "green"
        tag = "买入信号"
    elif "卖出" in message or "sell" in msg_lower or "平仓" in message or "止损" in message:
        header_color = "red"
        tag = "卖出信号"
    else:
        header_color = "blue"
        tag = "盘中信号"

    now_str = datetime.now(SHANGHAI).strftime("%H:%M")

    # 从消息中提取股票代码（6位数字）
    import re as _re
    code_match = _re.search(r"(\d{6})", message)
    stock_code = code_match.group(1) if code_match else ""

    elements = [
        {"tag": "markdown", "content": message},
    ]
    # 信号卡片加操作按钮
    if stock_code:
        if header_color == "green":
            # 买入信号：确认买入按钮
            elements.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "✓ 确认买入"},
                "type": "primary",
                "behaviors": [{"type": "callback", "value": {"action": "confirm_buy", "code": stock_code}}],
            })
        elif header_color == "red":
            # 卖出信号：确认卖出按钮
            elements.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "✓ 确认卖出"},
                "type": "danger",
                "behaviors": [{"type": "callback", "value": {"action": "confirm_sell", "code": stock_code}}],
            })
    elements.append({"tag": "markdown", "content": "<font color='grey'>仅供研究参考，不构成个性化投资建议</font>", "text_size": "notation"})

    card = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "default"},
        "header": {
            "title": {"tag": "plain_text", "content": "盘中买卖点信号"},
            "subtitle": {"tag": "plain_text", "content": f"{datetime.now(SHANGHAI):%Y-%m-%d} {now_str}"},
            "template": header_color,
            "icon": {"tag": "standard_icon", "token": "calendar_colorful"},
            "text_tag_list": [
                {"tag": "text_tag", "text": {"tag": "plain_text", "content": tag}, "color": header_color}
            ],
        },
        "body": {
            "direction": "vertical", "padding": "12px 12px 20px 12px",
            "vertical_spacing": "8px",
            "elements": elements,
        },
    }
    card_json = json.dumps(card, ensure_ascii=False)

    send = run([sys.executable, "-B", "send_bot.py", TARGET_CHAT_ID, card_json])
    if send.returncode != 0:
        print(f"[{datetime.now(SHANGHAI):%H:%M}] 飞书发送失败: {send.stderr[-300:]}", file=sys.stderr)
        return False

    commit = run([sys.executable, "-B", "server.py", "--lightclaw-commit", token])
    if commit.returncode != 0:
        print(f"[{datetime.now(SHANGHAI):%H:%M}] commit 失败: {commit.stderr[-300:]}", file=sys.stderr)
        return False

    print(f"[{datetime.now(SHANGHAI):%H:%M}] 已推送并提交：{result.get('event_id', '')}")
    return True


def in_scan_window(now: datetime) -> bool:
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 35 <= minutes <= 11 * 60 + 30) or (13 * 60 <= minutes <= 15 * 60)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true", help="持续运行到收盘，每5分钟扫描一次")
    args = parser.parse_args()

    if not args.daemon:
        return 0 if scan_once() else 1

    print(f"守护进程启动：{datetime.now(SHANGHAI):%Y-%m-%d %H:%M}，将在交易时段每5分钟扫描")
    while True:
        now = datetime.now(SHANGHAI)
        if now.weekday() >= 5:
            print("周末，退出")
            break
        minutes = now.hour * 60 + now.minute
        if minutes > 15 * 60 + 5:
            print("已过 15:05，退出")
            break
        if in_scan_window(now):
            try:
                scan_once()
            except Exception as e:
                print(f"扫描异常：{e}", file=sys.stderr)
        else:
            print(f"[{now:%H:%M}] 不在扫描窗口")
        time.sleep(SCAN_INTERVAL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

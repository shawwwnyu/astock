#!/usr/bin/env python3
"""盘中信号扫描与飞书推送；单次执行或 --daemon 每5分钟执行到收盘。"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import server

PROJECT_DIR = Path(__file__).resolve().parent
TARGET_CHAT_ID = "oc_4a44bf793473ba5dd87d50904065a92f"
SHANGHAI = server.SHANGHAI
SCAN_INTERVAL = 300


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True, timeout=180)


def command_result(result: subprocess.CompletedProcess) -> dict:
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"子进程失败：{result.stdout[-300:]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("子进程未返回单个 JSON 对象") from error
    if not isinstance(payload, dict):
        raise RuntimeError("子进程 JSON 必须是对象")
    return payload


def signal_button(action: dict) -> dict:
    side = action["side"]
    label = "买入" if side == "buy" else "卖出"
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": f"登记实际{label} {action['name']}({action['code']})"},
        "type": "default",
        "behaviors": [{"type": "callback", "value": {
            "action": f"confirm_{side}", "code": action["code"],
            "signal_id": action["notification_fingerprint"],
        }}],
    }


def build_signal_card(result: dict) -> dict:
    actions = result.get("actions", [])
    if not actions:
        raise ValueError("扫描结果缺少结构化 actions")
    sides = {action["side"] for action in actions}
    if not sides <= {"buy", "sell"}:
        raise ValueError("扫描结果包含无效 side")
    color = "green" if sides == {"buy"} else ("red" if sides == {"sell"} else "blue")
    elements = [
        {"tag": "markdown", "content": result["message"]},
        {"tag": "markdown", "content": "送达仅更新策略假定持仓；实际成交需逐只登记价格与数量。"},
    ]
    from llm import analyze_buy_signal, analyze_sell_signal
    for action in actions:
        explain = analyze_buy_signal if action["side"] == "buy" else analyze_sell_signal
        try:
            text = explain({key: action[key] for key in (
                "side", "code", "name", "price", "invalidation", "resistance", "reasons",
            ) if key in action})
        except RuntimeError as error:
            print(f"[LLM] {error}", file=sys.stderr)
            text = "暂不可用，以上规则信号不受影响。"
        if text:
            elements.append({"tag": "markdown", "content": f"**AI解读 · {action['code']}**\n{text[:600]}"})
        elements.append(signal_button(action))
    elements.append({"tag": "markdown", "content": "仅供研究参考，不构成个性化投资建议。"})
    return {
        "schema": "2.0", "config": {"update_multi": True, "width_mode": "default"},
        "header": {
            "title": {"tag": "plain_text", "content": "盘中买卖点信号"},
            "subtitle": {"tag": "plain_text", "content": result["decision_at"]},
            "template": color,
        },
        "body": {"elements": elements},
    }


def scan_once() -> bool:
    result = command_result(run([
        sys.executable, "-B", "server.py", "--lightclaw-scan-prepare", "--resume-pending",
    ]))
    status = result.get("status")
    if status == "no_signal":
        print(f"[{datetime.now(SHANGHAI):%H:%M}] 无信号：{result.get('reason', '')}")
        return False
    if status not in {"prepared", "already_pending"}:
        raise RuntimeError(f"扫描无法继续：{status}: {result.get('reason', '')}")
    token = result.get("commit_token")
    if not result.get("message") or not token or not result.get("event_id"):
        raise RuntimeError("扫描批次缺少消息、token 或事件标识")
    # 送达标识在 pending 外保存，不改变不可变报告及其校验哈希。
    if not result.get("delivery_message_id"):
        delivery_uuid = hashlib.sha256(result["event_id"].encode()).hexdigest()[:32]
        delivery = command_result(run([
            sys.executable, "-B", "send_bot.py", TARGET_CHAT_ID,
            json.dumps(build_signal_card(result), ensure_ascii=False), "--uuid", delivery_uuid,
        ]))
        if delivery.get("code") != 0:
            raise RuntimeError(f"飞书拒绝发送：{delivery.get('code')}")
        server.record_scan_delivery(result["event_id"], delivery.get("data", {}).get("message_id", ""))
    committed = command_result(run([sys.executable, "-B", "server.py", "--lightclaw-commit", token]))
    if committed.get("status") not in {"committed", "already_committed"}:
        raise RuntimeError("飞书已送达，但策略提交未完成；下轮继续提交")
    print(f"[{datetime.now(SHANGHAI):%H:%M}] 已推送并提交：{result['event_id']}")
    return True


def in_scan_window(now: datetime) -> bool:
    return server.scan_window_status(now)[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daemon", action="store_true", help="每5分钟扫描到收盘")
    args = parser.parse_args()
    if not args.daemon:
        try:
            scan_once()
            return 0
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
            print(f"扫描失败：{error}", file=sys.stderr)
            return 1
    while True:
        now = datetime.now(SHANGHAI)
        if now.weekday() >= 5 or now.hour * 60 + now.minute > 15 * 60 + 5:
            break
        if in_scan_window(now):
            try:
                scan_once()
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
                print(f"扫描异常：{error}", file=sys.stderr)
        time.sleep(SCAN_INTERVAL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

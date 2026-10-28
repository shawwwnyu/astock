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


def kline_chart_element(code: str) -> dict | None:
    """获取近20日K线，生成 VChart 蜡烛图配置。"""
    try:
        symbol = server.symbol_for(code)
        history = server.fetch_history(symbol)
        bars = history.get("raw_rows", [])[-20:]
        if len(bars) < 10:
            return None
        data = []
        for bar in bars:
            date_str = str(bar[0])[5:]  # MM-DD
            data.append({
                "date": date_str,
                "open": float(bar[1]),
                "close": float(bar[2]),
                "low": float(bar[3]),
                "high": float(bar[4]),
            })
        spec = {
            "type": "candlestick",
            "data": [{"id": "kline", "values": data}],
            "xField": "date",
            "yField": "open,close,high,low",
            "height": 180,
        }
        return {"tag": "chart", "spec": spec, "title": {"text": f"{code} 近20日K线"}}
    except Exception:
        return None


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
        # 加K线图
        chart = kline_chart_element(action["code"])
        if chart:
            elements.append(chart)
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


def check_position_risks() -> None:
    """检查持仓股票是否跌破止损位，是则推送风险卡片。"""
    state = server.load_trading_state()
    holdings = state.get("holdings", [])
    if not holdings:
        return
    risk_key = "position_risk_notified"
    notified = set(state.get(risk_key, []))
    now_str = datetime.now(SHANGHAI).strftime("%Y%m%d")
    new_risks = []
    for h in holdings:
        if h.get("status") != "open":
            continue
        code = h["code"]
        stop_loss = h.get("invalidation", 0)
        if not stop_loss:
            continue
        try:
            symbol = server.symbol_for(code)
            minute = server.fetch_minute(symbol)
            price = minute["price"]
        except Exception:
            continue
        if price <= stop_loss:
            risk_id = f"{now_str}-{code}-stop"
            if risk_id not in notified:
                new_risks.append({**h, "current_price": price})
                notified.add(risk_id)
    if new_risks:
        elements = [{"tag": "markdown", "content": "**⚠️ 持仓风险预警**"}]
        for h in new_risks:
            pnl_pct = (h["current_price"] - h["buy_price"]) / h["buy_price"] * 100
            elements.append({
                "tag": "markdown",
                "content": f"**{h['name']}({h['code']})**\n"
                           f"现价：{h['current_price']:.2f} | 买入：{h['buy_price']:.2f} | "
                           f"盈亏：{pnl_pct:+.2f}%\n"
                           f"已跌破止损位 {h['invalidation']:.2f}，建议卖出止损",
            })
        card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "持仓止损预警"}, "template": "red"},
            "body": {"elements": elements},
        }
        delivery = command_result(run([
            sys.executable, "-B", "send_bot.py", TARGET_CHAT_ID,
            json.dumps(card, ensure_ascii=False),
        ]))
        if delivery.get("code") == 0:
            state[risk_key] = list(notified)
            server.save_trading_state(state)
            print(f"[{datetime.now(SHANGHAI):%H:%M}] 已推送持仓止损预警：{[h['code'] for h in new_risks]}")


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
            try:
                check_position_risks()
            except Exception as error:
                print(f"风险检查异常：{error}", file=sys.stderr)
        time.sleep(SCAN_INTERVAL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

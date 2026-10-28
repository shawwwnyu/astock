#!/usr/bin/env python3
"""飞书 bot WebSocket 服务 - @机器人交互。

支持命令：
  @bot <代码或名称>        查询股票行情和信号状态
  @bot <代码> 已买入       填写实际成交价格和股数
  @bot <代码> 已卖出       填写实际卖出价格和股数
  @bot 持仓               查看用户确认的实际持仓
  @bot 帮助               显示帮助
"""
from __future__ import annotations

import json
import os
import re
import sys
import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

if TYPE_CHECKING:
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

import server
from actual_trades import record_trade, signal_batch, validate_signal
from scan_and_push import signal_button

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
TARGET_CHAT_ID = "oc_4a44bf793473ba5dd87d50904065a92f"
SHANGHAI = ZoneInfo("Asia/Shanghai")

LOG = Path("/tmp/bot-service.log")


def log(msg: str):
    line = f"[{datetime.now(SHANGHAI):%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def get_stock_name(code: str) -> str:
    """从 holdings 或历史里找股票名称。"""
    state = server.load_trading_state()
    for h in state["actual_holdings"] + state["actual_history"] + state["holdings"] + state["history"]:
        if h.get("code") == code:
            return h.get("name", code)
    return code


def find_stock(query: str) -> str | None:
    """根据代码或名称模糊匹配，返回6位代码。"""
    query = query.strip()
    # 直接是6位数字
    if re.fullmatch(r"[0-9]{6}", query):
        return query
    # 在 watchlist 里找名称
    state = server.load_trading_state()
    names = {}
    for h in state["actual_holdings"] + state["actual_history"] + state["holdings"] + state["history"]:
        if h.get("name"):
            names[h["name"]] = h.get("code")
    for name, code in names.items():
        if query in name or name in query:
            return code
    return None


def fetch_stock_quote(code: str) -> dict | None:
    """获取单只股票实时行情。"""
    symbol = server.symbol_for(code)
    try:
        minute = server.fetch_minute(symbol)
        if not minute or minute.get("trade_date") != datetime.now(SHANGHAI).strftime("%Y%m%d"):
            return None
        parsed = server.parse_minute(minute, 15 * 60)
        return parsed
    except Exception as e:
        log(f"获取 {code} 行情失败: {e}")
        return None


def make_reply_card(title: str, color: str, lines: list[str], tag: str = "") -> str:
    """构造回复卡片 JSON。"""
    elements = []
    for line in lines:
        elements.append({"tag": "markdown", "content": line})
    elements.append({
        "tag": "markdown",
        "content": "<font color='grey'>仅供研究参考，不构成个性化投资建议</font>",
        "text_size": "notation",
    })
    text_tag = []
    if tag:
        text_tag.append({"tag": "text_tag", "text": {"tag": "plain_text", "content": tag}, "color": color})
    card = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "default"},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
            "text_tag_list": text_tag,
        },
        "body": {"direction": "vertical", "padding": "12px 12px 20px 12px",
                 "vertical_spacing": "8px", "elements": elements},
    }
    return json.dumps(card, ensure_ascii=False)


def handle_query_stock(code: str) -> str:
    """查询股票行情。"""
    name = get_stock_name(code)
    quote = fetch_stock_quote(code)
    if not quote:
        return make_reply_card(
            f"{name}({code})", "grey",
            ["当前无实时行情数据（可能休市或数据获取失败）"],
            "无数据",
        )
    price = quote.get("price", "—")
    change_pct = quote.get("change_pct")
    chg_str = f"{change_pct:+.2f}%" if change_pct is not None else "—"
    color = "green" if (change_pct or 0) > 0 else ("red" if (change_pct or 0) < 0 else "grey")

    state = server.load_trading_state()
    holding = None
    for h in state["actual_holdings"]:
        if h.get("code") == code and h.get("status") == "open":
            holding = h
            break

    lines = [f"**现价：{price}  {chg_str}**"]
    if holding:
        buy_price = holding.get("buy_price", 0)
        shares = holding.get("shares", 0)
        invalid = holding.get("invalidation", 0)
        target = holding.get("target_price", 0)
        pnl_pct = (price - buy_price) / buy_price * 100 if buy_price else 0
        pnl_color = "green" if pnl_pct > 0 else "red"
        lines.append(f"**持仓：{shares}股 | 买入价 {buy_price} | 浮盈 <font color='{pnl_color}'>{pnl_pct:+.1f}%</font>**")
        lines.append(f"止损价：{invalid} | 目标价：{target}")
    else:
        lines.append("<font color='grey'>当前无已确认实际持仓；策略假定持仓另行统计。</font>")

    return make_reply_card(f"{name}({code})", color, lines, "持仓中" if holding else "自选股")


def handle_buy(code: str) -> str:
    return make_trade_form(code, "buy")


def handle_sell(code: str) -> str:
    return make_trade_form(code, "sell")


def handle_holdings() -> str:
    """查看当前持仓。"""
    state = server.load_trading_state()
    holdings = state["actual_holdings"]
    if not holdings:
        return make_reply_card("实际持仓", "grey", ["尚无已确认的实际持仓，策略假定持仓不在此列。"], "未登记")
    lines = []
    for h in holdings:
        code = h.get("code")
        name = h.get("name", code)
        buy_price = h.get("buy_price", 0)
        lines.append(f"**{name}({code})** | 买入价 {buy_price} | {h.get('shares', 0)}股")
    return make_reply_card("实际持仓", "blue", lines, f"{len(holdings)}只")


def handle_help() -> str:
    """帮助。"""
    lines = [
        "**@bot <代码或名称>** — 查询行情",
        "例：@bot 贵州茅台 / @bot 600519",
        "",
        "**@bot <代码> 已买入** — 填写实际成交价格和股数",
        "例：@bot 300398 已买入",
        "",
        "**@bot <代码> 已卖出** — 填写实际卖出价格和股数",
        "例：@bot 300398 已卖出",
        "",
        "**@bot 持仓** — 查看实际持仓（与策略假定持仓隔离）",
    ]
    return make_reply_card("A股短线雷达 Bot", "blue", lines, "帮助")


def parse_command(text: str) -> str:
    """从消息文本中提取命令（去掉 @bot 标记）。"""
    # 去掉 @_user_1 等 @标记
    text = re.sub(r"@_\w+\s*", "", text).strip()
    return text


def send_reply(chat_id: str, card_json: str):
    """用 bot 身份回复卡片到群。"""
    token_result = server_http_post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": APP_ID, "app_secret": APP_SECRET},
    )
    token = token_result.get("tenant_access_token", "")
    if not token:
        raise RuntimeError(f"获取飞书 token 失败：{token_result.get('code')}")
    result = server_http_post(
        f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": card_json,
        },
        token=token,
    )
    if result.get("code") != 0:
        raise RuntimeError(f"飞书回复失败：{result.get('code')} {result.get('msg', '')}")


def server_http_post(url: str, data: dict, token: str | None = None) -> dict:
    import urllib.request
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def do_message(data: P2ImMessageReceiveV1) -> None:
    try:
        msg = data.event.message
        chat_id = msg.chat_id
        if chat_id != TARGET_CHAT_ID or msg.message_type != "text":
            return
        content = json.loads(msg.content)
        text = content.get("text", "")
        cmd = parse_command(text)
        log(f"收到命令: '{cmd}' from chat={chat_id}")

        if not cmd:
            return

        card_json = None
        if cmd in ("帮助", "help", "?", "？"):
            card_json = handle_help()
        elif cmd in ("持仓", "仓位", "holdings"):
            card_json = handle_holdings()
        elif "已买入" in cmd or "买入" in cmd:
            code_part = cmd.replace("已买入", "").replace("买入", "").strip()
            code = find_stock(code_part)
            if code:
                card_json = handle_buy(code)
            else:
                card_json = make_reply_card("未找到股票", "grey", [f"找不到 '{code_part}'，请输入6位代码或名称"])
        elif "已卖出" in cmd or "卖出" in cmd:
            code_part = cmd.replace("已卖出", "").replace("卖出", "").strip()
            code = find_stock(code_part)
            if code:
                card_json = handle_sell(code)
            else:
                card_json = make_reply_card("未找到股票", "grey", [f"找不到 '{code_part}'，请输入6位代码或名称"])
        else:
            code = find_stock(cmd)
            if code:
                card_json = handle_query_stock(code)
            else:
                card_json = make_reply_card("未找到股票", "grey", [f"找不到 '{cmd}'，试试 @bot 帮助"])

        if card_json:
            send_reply(chat_id, card_json)
            log("回复已发送")
    except Exception as e:
        log(f"处理消息异常: {e}")
        import traceback
        log(traceback.format_exc())


def other_signal_buttons(signal_id: str) -> list[dict]:
    if not signal_id:
        return []
    state = server.load_trading_state()
    return [
        signal_button(item) for item in signal_batch(state, signal_id)
        if item["notification_fingerprint"] != signal_id
        and f"signal:{item['notification_fingerprint']}" not in state["actual_trade_receipts"]
    ]


def make_trade_form(code: str, side: str, signal_id: str = "") -> str:
    if not re.fullmatch(r"[0-9]{6}", code) or side not in {"buy", "sell"}:
        raise ValueError("股票代码或买卖方向无效")
    name = get_stock_name(code)
    if signal_id:
        signal = validate_signal(server.load_trading_state(), signal_id, code, side, datetime.now(SHANGHAI))
        name = signal["name"]
    label = "买入" if side == "buy" else "卖出"
    identity = signal_id or f"manual{secrets.token_hex(16)}"
    card = json.loads(make_reply_card(
        f"登记实际{label} {name}({code})", "green" if side == "buy" else "red",
        ["仅登记已经完成的真实成交，不发送券商委托，不修改策略假定持仓。"],
    ))
    card["body"]["elements"].insert(1, {
        "tag": "form", "name": "trade_form", "elements": [
            {"tag": "input", "name": "price", "required": True,
             "label": {"tag": "plain_text", "content": "实际成交价格"},
             "placeholder": {"tag": "plain_text", "content": "正数，如 28.15"}},
            {"tag": "input", "name": "shares", "required": True,
             "label": {"tag": "plain_text", "content": "实际成交股数"},
             "placeholder": {"tag": "plain_text", "content": "正整数，如 100"}},
            {"tag": "button", "name": f"trade_{side}_{code}_{identity}",
             "text": {"tag": "plain_text", "content": f"确认登记{label}"},
             "type": "primary_filled", "width": "fill", "form_action_type": "submit"},
        ],
    })
    card["body"]["elements"].extend(other_signal_buttons(signal_id))
    return json.dumps(card, ensure_ascii=False)


def card_response(payload: dict):
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    return P2CardActionTriggerResponse(payload)


def do_card_action(data: P2CardActionTrigger):
    try:
        if data.event.context.open_chat_id != TARGET_CHAT_ID:
            raise ValueError("该会话不允许登记持仓")
        action = data.event.action
        value = action.value or {}
        action_name = action.name or ""
        if action_name.startswith("trade_"):
            match = re.fullmatch(r"trade_(buy|sell)_([0-9]{6})_(manual[0-9a-f]{32}|[0-9a-f]{64})", action_name)
            if not match:
                raise ValueError("成交表单标识无效")
            side, code, identity = match.groups()
            signal_id = "" if identity.startswith("manual") else identity
            form = action.form_value or {}
            # 先构造其余按钮；渲染异常不得让已完成写入伪装成失败。
            other_buttons = other_signal_buttons(signal_id)
            receipt = record_trade(
                code, side, form.get("price"), form.get("shares"),
                request_id=action_name, signal_id=signal_id,
            )
            label = "买入" if side == "buy" else "卖出"
            prefix = "已登记过，未重复写入" if receipt["duplicate"] else "实际成交已登记"
            text = f"{prefix}：{receipt['name']}({code}) {label} {receipt['shares']}股，成交价 {receipt['price']}"
            card = json.loads(make_reply_card("实际成交登记", "blue", [text]))
            card["body"]["elements"].extend(other_buttons)
            return card_response({
                "toast": {"type": "success", "content": text},
                "card": {"type": "raw", "data": card},
            })
        if value.get("action") in ("confirm_buy", "confirm_sell"):
            signal_id = value.get("signal_id", "")
            if not signal_id:
                raise ValueError("旧卡片缺少信号标识，请使用新信号或手工登记")
            side = value["action"].removeprefix("confirm_")
            card = json.loads(make_trade_form(value.get("code", ""), side, signal_id))
            return card_response({"card": {"type": "raw", "data": card}})
        raise ValueError("未知操作，请使用新卡片")
    except ValueError as error:
        return card_response({"toast": {"type": "error", "content": str(error)}})
    except Exception as error:
        log(f"卡片回调异常: {error}")
        return card_response({"toast": {"type": "error", "content": "登记异常，请核对持仓后重试"}})


def main():
    import lark_oapi as lark
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    if not APP_ID or not APP_SECRET:
        log("错误：请设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET 环境变量")
        sys.exit(1)
    log("=== Bot 服务启动 ===")
    event_handler = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_message)
        .register_p2_card_action_trigger(do_card_action)
        .build()
    )
    cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=event_handler, log_level=lark.LogLevel.INFO)
    cli.start()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""飞书 bot WebSocket 服务 - @机器人交互。

支持命令：
  @bot <代码或名称>        查询股票行情和信号状态
  @bot <代码> 已买入       标记已买入（加入 holdings）
  @bot <代码> 已卖出       标记已卖出（关闭持仓）
  @bot 持仓               查看当前持仓
  @bot 帮助               显示帮助
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

import lark_oapi as lark
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger, P2CardActionTriggerResponse, CallBackToast, CallBackCard

import server

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
    for h in state.get("holdings", []) + state.get("history", []):
        if h.get("code") == code:
            return h.get("name", code)
    return code


def find_stock(query: str) -> str | None:
    """根据代码或名称模糊匹配，返回6位代码。"""
    query = query.strip()
    # 直接是6位数字
    if re.fullmatch(r"\d{6}", query):
        return query
    # 在 watchlist 里找名称
    state = server.load_trading_state()
    names = {}
    for h in state.get("holdings", []) + state.get("history", []):
        names[h.get("name", "")] = h.get("code")
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
    for h in state.get("holdings", []):
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
        lines.append("<font color='grey'>当前无持仓</font>")

    return make_reply_card(f"{name}({code})", color, lines, "持仓中" if holding else "自选股")


def handle_buy(code: str) -> str:
    """标记已买入。"""
    name = get_stock_name(code)
    state = server.load_trading_state()
    now = datetime.now(SHANGHAI)
    quote = fetch_stock_quote(code)
    price = quote.get("price", 0) if quote else 0

    # 检查是否已在持仓
    for h in state.get("holdings", []):
        if h.get("code") == code and h.get("status") == "open":
            return make_reply_card(
                f"{name}({code})", "yellow",
                [f"该股票已在持仓中（买入价 {h.get('buy_price')}）"],
                "已有持仓",
            )

    holding = {
        "id": f"{now.strftime('%Y%m%d-%H%M')}-{code}",
        "code": code,
        "name": name,
        "buy_date": now.strftime("%Y%m%d"),
        "buy_time": now.strftime("%H%M"),
        "buy_price": price,
        "shares": 0,
        "capital_used": 0,
        "invalidation": 0,
        "target_price": 0,
        "source": "manual",
        "status": "open",
    }
    state.setdefault("holdings", []).append(holding)
    server.save_trading_state(state)
    return make_reply_card(
        f"{name}({code})", "green",
        [f"**已标记买入** | 现价 {price}", "后续扫描将监控该股票的止损和目标价"],
        "已买入",
    )


def handle_sell(code: str) -> str:
    """标记已卖出。"""
    name = get_stock_name(code)
    state = server.load_trading_state()
    found = False
    for h in state.get("holdings", []):
        if h.get("code") == code and h.get("status") == "open":
            h["status"] = "closed"
            h["sell_time"] = datetime.now(SHANGHAI).strftime("%H%M")
            state.setdefault("history", []).append(h)
            state["holdings"].remove(h)
            found = True
            break
    server.save_trading_state(state)
    if found:
        return make_reply_card(
            f"{name}({code})", "red",
            ["**已标记卖出，持仓已关闭**"],
            "已卖出",
        )
    return make_reply_card(
        f"{name}({code})", "grey",
        ["当前没有该股票的持仓"],
        "无持仓",
    )


def handle_holdings() -> str:
    """查看当前持仓。"""
    state = server.load_trading_state()
    holdings = [h for h in state.get("holdings", []) if h.get("status") == "open"]
    if not holdings:
        return make_reply_card("当前持仓", "grey", ["<font color='grey'>无持仓</font>"], "空仓")
    lines = []
    for h in holdings:
        code = h.get("code")
        name = h.get("name", code)
        buy_price = h.get("buy_price", 0)
        lines.append(f"**{name}({code})** | 买入价 {buy_price} | {h.get('shares', 0)}股")
    return make_reply_card("当前持仓", "blue", lines, f"{len(holdings)}只")


def handle_help() -> str:
    """帮助。"""
    lines = [
        "**@bot <代码或名称>** — 查询行情",
        "例：@bot 贵州茅台 / @bot 600519",
        "",
        "**@bot <代码> 已买入** — 标记买入",
        "例：@bot 300398 已买入",
        "",
        "**@bot <代码> 已卖出** — 标记卖出",
        "例：@bot 300398 已卖出",
        "",
        "**@bot 持仓** — 查看当前持仓",
    ]
    return make_reply_card("A股短线雷达 Bot", "blue", lines, "帮助")


def parse_command(text: str) -> str:
    """从消息文本中提取命令（去掉 @bot 标记）。"""
    # 去掉 @_user_1 等 @标记
    text = re.sub(r"@_\w+\s*", "", text).strip()
    return text


def send_reply(chat_id: str, card_json: str):
    """用 bot 身份回复卡片到群。"""
    import urllib.request
    token_result = server_http_post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": APP_ID, "app_secret": APP_SECRET},
    )
    token = token_result.get("tenant_access_token", "")
    if not token:
        log(f"获取 token 失败: {token_result}")
        return
    server_http_post(
        f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": card_json,
        },
        token=token,
    )


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


def do_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """处理卡片按钮回调。"""
    try:
        action = data.event.action
        value = action.value or {}
        form_value = action.form_value or {}
        action_type = value.get("action", "")
        code = value.get("code", "")
        name = get_stock_name(code)

        # 表单提交（输入价格后确认）
        if action_type == "submit_trade":
            price_str = form_value.get("price", "0")
            try:
                price = float(price_str)
            except ValueError:
                price = 0
            trade_action = value.get("trade_action", "buy")
            if trade_action == "buy":
                result = handle_buy_with_price(code, price)
            else:
                result = handle_sell_with_price(code, price)
            return P2CardActionTriggerResponse({
                "toast": {"type": "success", "content": result},
                "card": {"type": "raw", "data": json.loads(make_reply_card(
                    f"{name}({code})", "green" if trade_action == "buy" else "red",
                    [result], "已记录"
                ))},
            })

        # 按钮点击：弹出价格输入表单
        if action_type in ("confirm_buy", "confirm_sell"):
            trade_action = "buy" if action_type == "confirm_buy" else "sell"
            action_label = "买入" if trade_action == "buy" else "卖出"
            form_card = {
                "schema": "2.0",
                "config": {"update_multi": True, "width_mode": "default"},
                "header": {
                    "title": {"tag": "plain_text", "content": f"确认{action_label} {name}({code})"},
                    "template": "green" if trade_action == "buy" else "red",
                },
                "body": {
                    "direction": "vertical", "padding": "12px 12px 20px 12px",
                    "vertical_spacing": "12px",
                    "elements": [
                        {
                            "tag": "form",
                            "name": "trade_form",
                            "elements": [
                                {
                                    "tag": "input",
                                    "name": "price",
                                    "placeholder": {"tag": "plain_text", "content": "输入成交价格，如 28.15"},
                                    "label": {"tag": "plain_text", "content": "成交价格"},
                                    "default": "",
                                },
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": f"确认{action_label}"},
                                    "type": "primary" if trade_action == "buy" else "danger",
                                    "form_action_type": "submit",
                                    "behaviors": [{"type": "callback", "value": {"action": "submit_trade", "code": code, "trade_action": trade_action}}],
                                },
                            ],
                        }
                    ],
                },
            }
            return P2CardActionTriggerResponse({
                "card": {"type": "raw", "data": form_card},
            })

        return P2CardActionTriggerResponse({
            "toast": {"type": "warning", "content": "未知操作"},
        })
    except Exception as e:
        log(f"卡片回调异常: {e}")
        import traceback
        log(traceback.format_exc())
        return P2CardActionTriggerResponse({
            "toast": {"type": "error", "content": f"处理失败: {e}"},
        })


def handle_buy_with_price(code: str, price: float) -> str:
    """带价格的买入标记。"""
    name = get_stock_name(code)
    state = server.load_trading_state()
    now = datetime.now(SHANGHAI)
    for h in state.get("holdings", []):
        if h.get("code") == code and h.get("status") == "open":
            return f"{name}({code}) 已在持仓中"
    holding = {
        "id": f"{now.strftime('%Y%m%d-%H%M')}-{code}",
        "code": code, "name": name,
        "buy_date": now.strftime("%Y%m%d"),
        "buy_time": now.strftime("%H%M"),
        "buy_price": price, "shares": 0, "capital_used": 0,
        "invalidation": 0, "target_price": 0,
        "source": "signal", "status": "open",
    }
    state.setdefault("holdings", []).append(holding)
    server.save_trading_state(state)
    return f"{name}({code}) 已买入，价格 {price}"


def handle_sell_with_price(code: str, price: float) -> str:
    """带价格的卖出标记。"""
    name = get_stock_name(code)
    state = server.load_trading_state()
    for h in state.get("holdings", []):
        if h.get("code") == code and h.get("status") == "open":
            h["status"] = "closed"
            h["sell_price"] = price
            h["sell_time"] = datetime.now(SHANGHAI).strftime("%H%M")
            state.setdefault("history", []).append(h)
            state["holdings"].remove(h)
            server.save_trading_state(state)
            return f"{name}({code}) 已卖出，价格 {price}"
    return f"{name}({code}) 无持仓可卖"


def main():
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

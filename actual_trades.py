"""用户确认的真实成交账本，与策略假定持仓隔离。"""
from __future__ import annotations

import copy
import math
import re
from datetime import datetime
from pathlib import Path

import server


def signal_batch(state: dict, signal_id: str) -> list[dict]:
    pending = state.get("pending_report")
    if pending is not None:
        pending = server._validate_pending(pending, state)
        if pending["kind"] == "intraday-scan":
            report = pending["report"]
            if any(item["notification_fingerprint"] == signal_id for item in report["actions"]):
                return [
                    {**copy.deepcopy(item), "trade_date": report["analysis"]["trade_date"],
                     "event_id": report["event_id"],
                     "strategy_holding_id": f"{report['analysis']['trade_date']}-{report['scan_time']}-{item['code']}"}
                    for item in report["actions"]
                ]
    saved = state["delivered_signals"].get(signal_id)
    if not saved:
        raise ValueError("找不到对应信号，请使用新卡片或手工登记")
    return [
        copy.deepcopy(item) for item in state["delivered_signals"].values()
        if item["event_id"] == saved["event_id"]
    ]


def validate_signal(state: dict, signal_id: str, code: str, side: str, now: datetime) -> dict:
    signal = next(item for item in signal_batch(state, signal_id)
                  if item["notification_fingerprint"] == signal_id)
    if signal["code"] != code or signal["side"] != side:
        raise ValueError("股票或买卖方向与信号不符")
    if signal["trade_date"] != now.strftime("%Y%m%d"):
        raise ValueError("信号已过期，请手工登记实际成交")
    return signal


def record_trade(
    code: str, side: str, price: object, shares: object, *,
    request_id: str, signal_id: str = "", now: datetime | None = None,
    state_path: Path | None = None,
) -> dict:
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) or side not in {"buy", "sell"}:
        raise ValueError("股票代码或买卖方向无效")
    try:
        price_number = float(price)
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("成交价格必须为正有限数") from error
    if isinstance(price, bool) or not math.isfinite(price_number) or price_number <= 0:
        raise ValueError("成交价格必须为正有限数")
    if isinstance(shares, bool) or not re.fullmatch(r"[0-9]+", str(shares)) or int(str(shares)) <= 0:
        raise ValueError("成交股数必须为正整数")
    quantity = int(str(shares))
    try:
        finite_amount = math.isfinite(price_number * quantity)
    except OverflowError as error:
        raise ValueError("成交金额超出范围") from error
    if not finite_amount:
        raise ValueError("成交金额超出范围")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("缺少成交请求标识")
    now = now or datetime.now(server.SHANGHAI)
    day = now.strftime("%Y%m%d")
    receipt_key = f"signal:{signal_id}" if signal_id else f"request:{request_id}"
    with server.trading_state_transaction(state_path) as locked_path:
        state = server.load_trading_state(locked_path)
        prior = state["actual_trade_receipts"].get(receipt_key)
        if prior:
            if (prior["code"], prior["side"], prior["price"], prior["shares"]) != (code, side, price_number, quantity):
                raise ValueError("该请求已登记过不同的成交数据，请先核对账本")
            return {**prior, "duplicate": True}
        signal = validate_signal(state, signal_id, code, side, now) if signal_id else {}
        holding = next((item for item in state["actual_holdings"] if item["code"] == code), None)
        if side == "buy":
            if holding:
                raise ValueError("已有实际持仓，不支持重复开仓或自动加仓")
            name = signal.get("name", code)
            holding = {
                "id": receipt_key, "code": code, "name": name, "buy_date": day,
                "buy_time": now.strftime("%H%M"), "buy_price": price_number, "shares": quantity,
                "capital_used": round(price_number * quantity, 2), "status": "open",
                "source": "user-confirmed",
                "strategy_holding_id": signal.get("strategy_holding_id", ""),
            }
            for key, source in (("invalidation", "invalidation"), ("target_price", "resistance")):
                value = server.as_number(signal.get(source))
                holding[key] = value if value is not None and value > 0 else None
            state["actual_holdings"].append(holding)
        else:
            if not holding:
                raise ValueError("无已确认的实际持仓可卖；策略假定持仓不代表真实成交")
            if day <= holding["buy_date"]:
                raise ValueError("当日买入受T+1限制，不可卖出")
            if quantity > holding["shares"]:
                raise ValueError("卖出数量超过实际持仓")
            if signal and holding.get("strategy_holding_id") and signal["holding_id"] != holding["strategy_holding_id"]:
                raise ValueError("卖出信号与当前实际持仓的开仓批次不匹配")
            name = holding["name"]
            closed = {
                **copy.deepcopy(holding), "status": "closed", "shares": quantity,
                "capital_used": round(holding["buy_price"] * quantity, 2),
                "sell_date": day, "sell_time": now.strftime("%H%M"), "sell_price": price_number,
                "return_pct": round((price_number / holding["buy_price"] - 1) * 100, 2),
            }
            state["actual_history"].append(closed)
            holding["shares"] -= quantity
            holding["capital_used"] = round(holding["buy_price"] * holding["shares"], 2)
            if holding["shares"] == 0:
                state["actual_holdings"].remove(holding)
        receipt = {
            "code": code, "name": name, "side": side, "price": price_number,
            "shares": quantity, "recorded_at": now.isoformat(timespec="seconds"),
        }
        state["actual_trade_receipts"][receipt_key] = receipt
        server.save_trading_state(state, locked_path)
    return {**receipt, "duplicate": False}

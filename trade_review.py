#!/usr/bin/env python3
"""实际成交日/周复盘；由外部定时任务在北京时间20:00调用。"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

import server
import strategy_rules
from feishu_client import send_card, target_chat_id
from report_cards import message_card
import signal_review


def money(value) -> Decimal:
    return Decimal(str(value))


def day(value: str) -> date:
    return date.fromisoformat(value if "-" in value else f"{value[:4]}-{value[4:6]}-{value[6:8]}")


def close_prices(codes: set[str], start: date, end: date) -> dict:
    prices = {}
    for code in sorted(codes):
        symbol = server.symbol_for(code)
        bars = []
        try:
            history = server.fetch_history(symbol)
            bars = server.parse_history(history, (end + timedelta(days=1)).strftime("%Y%m%d"))
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            print(f"复盘历史行情获取失败 {code}: {error}", file=sys.stderr)
        before = [bar for bar in bars if day(str(bar["date"])) < start]
        closing = [bar for bar in bars if day(str(bar["date"])) <= end]
        latest = closing[-1] if closing else None
        # 当日K线可能尚未进入日线接口，使用完整收盘分时补齐。
        try:
            minute = server.fetch_minute(symbol)
            quote_day = day(minute["trade_date"])
            if quote_day == end:
                quote = server.parse_minute(minute, 900)
                if server.hhmm_to_minutes(quote["last_time"]) >= 900:
                    latest = {"date": end.isoformat(), "close": quote["price"]}
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            print(f"复盘分时行情获取失败 {code}: {error}", file=sys.stderr)
        prices[code] = {
            "before": before[-1]["close"] if before else None,
            "before_date": before[-1]["date"] if before else None,
            "close": latest["close"] if latest and day(str(latest["date"])) == end else None,
        }
    return prices


def signal_performance(state: dict, start: date, end: date) -> dict:
    histories = {}
    missing = []
    signal_closes = {}
    codes = {item["code"] for item in state.get("signal_observations", {}).values()
             if day(item["trade_date"]) <= end}
    benchmark = next(iter(server.BENCHMARKS))
    today_codes = {item["code"] for item in state.get("signal_observations", {}).values()
                   if day(item["trade_date"]) == end}
    for symbol in sorted({server.symbol_for(code) for code in codes} | ({benchmark} if codes else set())):
        bars = []
        try:
            history = server.fetch_history(symbol)
            if symbol != benchmark and history.get("adjustment") != "qfq":
                raise RuntimeError("未取得已核验的前复权日线，不计算信号收益")
            bars = server.parse_history(history, (end + timedelta(days=1)).strftime("%Y%m%d"))
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            print(f"信号复盘日线获取失败 {symbol}: {error}", file=sys.stderr)
            missing.append(f"{symbol} 信号后续日线缺失")
        # A minute failure must not discard verified historical bars.
        if symbol[2:] in today_codes or not bars or day(bars[-1]["date"]) < end:
            try:
                minute = server.fetch_minute(symbol)
                if day(minute["trade_date"]) == end:
                    quote = server.parse_minute(minute, 900)
                    if server.hhmm_to_minutes(quote["last_time"]) >= 900:
                        signal_closes[symbol[2:]] = quote["price"]
                        if not bars or day(bars[-1]["date"]) < end:
                            bars.append({"date": end.isoformat(), "close": quote["price"]})
            except (OSError, ValueError, RuntimeError, KeyError) as error:
                print(f"信号复盘收盘分时获取失败 {symbol}: {error}", file=sys.stderr)
                missing.append(f"{symbol} 当日完整收盘分时缺失")
        histories[symbol] = bars
    calendar = [bar["date"] for bar in histories.get(benchmark, [])]
    evaluated = copy.deepcopy(state)
    updates = {}
    for identity, item in evaluated.get("signal_observations", {}).items():
        if day(item["trade_date"]) == end and item["code"] in signal_closes:
            item["signal_day_close"] = signal_closes[item["code"]]
            updates[identity] = item["signal_day_close"]
    result = signal_review.evaluate(evaluated, start, end, {
        code: histories.get(server.symbol_for(code), []) for code in codes
    }, calendar=calendar, adjusted=True)
    result["missing_data"].extend(missing)
    result["signal_closes"] = updates
    return result


def build_review(state: dict, period: str, end: date, prices: dict) -> dict:
    if period not in {"daily", "weekly"}:
        raise ValueError("未知复盘周期")
    start = end if period == "daily" else end - timedelta(days=end.weekday())
    closed = state["actual_history"]
    # 还原截止日的未平仓数量，支持以后补查历史日期。
    open_lots = state["actual_holdings"] + [item for item in closed if day(item["sell_date"]) > end]
    open_lots = [item for item in open_lots if day(item["buy_date"]) <= end]
    sales = [item for item in closed if start <= day(item["sell_date"]) <= end]
    all_lots = state["actual_holdings"] + closed
    buys = {}
    for item in all_lots:
        if start <= day(item["buy_date"]) <= end:
            key = item["id"]
            if key not in buys:
                buys[key] = {key: item[key] for key in ("code", "name", "buy_date", "buy_price")}
                buys[key]["shares"] = 0
            buys[key]["shares"] += item["shares"]
    realized = sum(
        ((money(item["sell_price"]) - money(item["buy_price"])) * item["shares"] for item in sales),
        Decimal(0),
    )
    period_pnl = Decimal(0)
    floating = Decimal(0)
    missing = []
    holdings = []
    for item in open_lots + sales:
        code = item["code"]
        quote = prices.get(code, {})
        is_sale = "sell_date" in item and day(item["sell_date"]) <= end
        exit_price = item["sell_price"] if is_sale else quote.get("close")
        entry = item["buy_price"] if day(item["buy_date"]) >= start else quote.get("before")
        if entry is None or exit_price is None:
            missing.append(f"{code} 缺少{'期初收盘价' if entry is None else '期末收盘价'}")
        else:
            period_pnl += (money(exit_price) - money(entry)) * item["shares"]
        if not is_sale:
            pnl = ((money(exit_price) - money(item["buy_price"])) * item["shares"]) if exit_price is not None else None
            if pnl is not None:
                floating += pnl
            holdings.append({
                "code": code, "name": item["name"], "shares": item["shares"],
                "close": exit_price, "floating_pnl": round(float(pnl), 2) if pnl is not None else None,
            })
    losses = sum(item["sell_price"] < item["buy_price"] for item in sales)
    return {
        "schema": "astock.actual-review/v1", "period": period,
        "start": start.isoformat(), "end": end.isoformat(),
        "buys": list(buys.values()),
        "sells": [{key: item[key] for key in ("code", "name", "shares", "sell_date", "sell_price", "buy_price")} for item in sales],
        "realized_pnl": round(float(realized), 2),
        "period_pnl": None if missing else round(float(period_pnl), 2),
        "floating_pnl": None if any(item["floating_pnl"] is None for item in holdings) else round(float(floating), 2),
        "holdings": holdings, "missing_data": sorted(set(missing)), "losing_sales": losses,
        "assumed_holdings_count": len(state["holdings"]),
        "cost_basis": "实际登记价格，未扣佣金、印花税及其他费用；期间盈亏按期初/期末收盘价计价，不等于卖出已实现盈亏。",
        "rule_update": {**strategy_rules.plan_update(state, end), "status": "preview"},
    }


def review_card(report: dict) -> dict:
    def amount(value):
        return "无法计算（行情缺失）" if value is None else f"{value:+,.2f} 元"
    label = "每日" if report["period"] == "daily" else "每周"
    lines = [
        f"**{report['start']} 至 {report['end']} · 实际账本**",
        f"**本期盈亏：{amount(report['period_pnl'])}**",
        f"本期卖出已实现盈亏（相对买入成本）：{amount(report['realized_pnl'])}",
        f"期末持仓浮盈亏（相对买入成本）：{amount(report['floating_pnl'])}",
        "**本期操作**",
    ]
    for item in report["buys"]:
        lines.append(f"买入 {item['name']}({item['code']}) {item['shares']}股 · 成本 {item['buy_price']} · {item['buy_date']}")
    for item in report["sells"]:
        lines.append(f"卖出 {item['name']}({item['code']}) {item['shares']}股 · 成交 {item['sell_price']} · {item['sell_date']}")
    if not report["buys"] and not report["sells"]:
        lines.append("本期无已登记买卖；期间盈亏仍可能来自持仓价格变化。")
    lines.append("**期末持仓**")
    for item in report["holdings"]:
        lines.append(f"{item['name']}({item['code']}) {item['shares']}股 · 浮盈亏 {amount(item['floating_pnl'])}")
    lines.append("**策略复核**")
    if "signal_review" in report:
        performance = report["signal_review"]
        lines.append(f"信号成熟样本 {performance['matured_count']}，尚未满3个交易日 {performance['pending_count']}。")
        for key, metric in performance["metrics"].items():
            rate = metric["directional_hit_rate"]
            lines.append(f"{key}：{metric['count']}个 · 3日方向正确率 "
                         + (f"{rate:.1%}" if rate is not None else "样本不足，无法计算"))
        lines.append(performance["definition"])
        lines.extend(performance.get("missing_data", []))
    lines.append(report.get("ai_review", {}).get("analysis", ""))
    lines.append(f"本期卖出记录 {len(report['sells'])} 笔，其中亏损 {report['losing_sales']} 笔（可含部分卖出）。")
    update = report["rule_update"]
    lines.extend([
        f"**规则更新：{update['status']} · 生效日 {update['effective_date']}**",
        f"入场最低盈亏比：{update['before']['minimum_reward_risk']}R → {update['after']['minimum_reward_risk']}R",
        f"最长可卖交易日：{update['before']['max_holding_sessions']} → {update['after']['max_holding_sessions']}",
        update["reason"], update["warning"],
        "日/周复盘分别评估信号；每晚规则最多调整一次，周五不重复叠加。",
        "T+1、禁止自动加仓、仓位上限、市场红灯与数据/公告核验硬约束不变。",
    ])
    lines.extend(report["missing_data"])
    lines.extend([
        report["cost_basis"],
        f"策略假定持仓 {report['assumed_holdings_count']} 只，未混入实际盈亏。",
        "仅供研究参考，不构成个性化投资建议。",
    ])
    return message_card(f"A股{label}操作复盘", "blue", lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", choices=("daily", "weekly"), required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, help="复盘截止日，默认北京时间当天")
    parser.add_argument("--send", action="store_true", help="发送飞书；否则只输出报告JSON")
    parser.add_argument("--apply-rules", action="store_true", help="当天20:00后自动更新有界规则，下一交易日起读取")
    args = parser.parse_args()
    try:
        chat_id = target_chat_id() if args.send else ""
        end = args.as_of or datetime.now(server.SHANGHAI).date()
        if end > datetime.now(server.SHANGHAI).date():
            raise ValueError("不得复盘未来日期")
        if args.apply_rules and (end != datetime.now(server.SHANGHAI).date() or datetime.now(server.SHANGHAI).hour < 20):
            raise ValueError("自动调参只允许北京时间当天20:00后执行")
        state = server.load_trading_state()
        start = end if args.period == "daily" else end - timedelta(days=end.weekday())
        codes = {item["code"] for item in state["actual_holdings"] + state["actual_history"]
                 if day(item["buy_date"]) <= end and ("sell_date" not in item or day(item["sell_date"]) >= start)}
        report = build_review(state, args.period, end, close_prices(codes, start, end))
        report["signal_review"] = signal_performance(state, start, end)
        planned = strategy_rules.plan_ai_update(state, report)
        report["ai_review"] = planned
        report["rule_update"] = {**planned, "status": "preview"}
        if args.apply_rules:
            with server.trading_state_transaction() as path:
                latest = server.load_trading_state(path)
                expected_observations = copy.deepcopy(state["signal_observations"])
                for identity, price in report["signal_review"].get("signal_closes", {}).items():
                    expected_observations[identity]["signal_day_close"] = price
                observation_changed = (
                    latest["signal_observations"] != state["signal_observations"]
                    and latest["signal_observations"] != expected_observations
                )
                original_revisions = state["strategy_adjustments"]
                latest_revisions = latest["strategy_adjustments"]
                revisions_changed = (
                    latest_revisions[:len(original_revisions)] != original_revisions
                    or any(item["review_date"] != end.isoformat() for item in latest_revisions[len(original_revisions):])
                )
                if observation_changed or revisions_changed or any(
                    latest[key] != state[key] for key in ("actual_holdings", "actual_history")
                ):
                    raise RuntimeError("复盘期间账本、信号或规则发生变化，未修改规则；请重新执行")
                report["rule_update"] = strategy_rules.apply_ai_update(latest, planned, datetime.now(server.SHANGHAI))
                for identity, price in report["signal_review"].get("signal_closes", {}).items():
                    latest["signal_observations"][identity]["signal_day_close"] = price
                if planned["ai_status"] == "ok":
                    latest.setdefault("ai_reviews", {})[planned["evidence_hash"]] = planned
                server.save_trading_state(latest, path)
        if args.send:
            event_id = f"actual-review:{args.period}:{start}:{end}"
            # 同一进程间锁覆盖检查/发送/标记，UUID覆盖网络结果不确定时的短期重试。
            with server.trading_state_transaction() as path:
                latest = server.load_trading_state(path)
                for identity, price in report["signal_review"].get("signal_closes", {}).items():
                    if identity in latest["signal_observations"]:
                        latest["signal_observations"][identity]["signal_day_close"] = price
                deliveries = latest.setdefault("actual_review_deliveries", {})
                if event_id in deliveries:
                    report["delivery"] = {"status": "already_sent", "message_id": deliveries[event_id]}
                else:
                    result = send_card(chat_id, json.dumps(review_card(report), ensure_ascii=False),
                                       hashlib.sha256(event_id.encode()).hexdigest()[:32])
                    deliveries[event_id] = result["data"]["message_id"]
                    server.save_trading_state(latest, path)
                    report["delivery"] = {"status": "sent", "message_id": deliveries[event_id]}
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

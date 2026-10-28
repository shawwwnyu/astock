#!/usr/bin/env python3
"""简单回测：用历史日线数据测试不同参数组合的收益表现。"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import server

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))


def parse_bars(history: dict) -> list[dict]:
    """把原始K线转成 dict 列表。"""
    rows = history.get("raw_rows", [])
    bars = []
    for row in rows:
        bars.append({
            "date": str(row[0]),
            "open": float(row[1]),
            "close": float(row[2]),
            "low": float(row[3]),
            "high": float(row[4]),
            "volume": float(row[5]) if len(row) > 5 else 0,
        })
    return bars


def ma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def backtest_stock(code: str, bars: list[dict], min_rr: float, hold_days: int) -> dict:
    """单只股票回测。"""
    trades = []
    closes = [b["close"] for b in bars]
    for i in range(30, len(bars) - hold_days):
        # 简单买入条件：收盘价站上MA20，MA5>MA10（多头排列）
        ma5 = ma(closes[:i+1], 5)
        ma10 = ma(closes[:i+1], 10)
        ma20 = ma(closes[:i+1], 20)
        if not (ma5 and ma10 and ma20):
            continue
        price = closes[i]
        if price < ma20 or ma5 < ma10:
            continue
        # 止损 = 近20日最低价，目标 = 买入价 * (1 + (price - ma20) / ma20 * hold_days)
        recent_low = min(b["low"] for b in bars[i-20:i+1])
        stop = recent_low
        risk = price - stop
        if risk <= 0:
            continue
        # 持有 hold_days 天后卖出
        sell_price = closes[i + hold_days]
        reward = sell_price - price
        rr = reward / risk if risk > 0 else 0
        trades.append({
            "date": bars[i]["date"],
            "buy": price,
            "sell": sell_price,
            "stop": stop,
            "pnl_pct": (sell_price - price) / price * 100,
            "rr": rr,
            "win": sell_price > price,
        })
    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    total_pnl = sum(t["pnl_pct"] for t in trades)
    return {
        "code": code,
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "avg_pnl": total_pnl / len(trades) if trades else 0,
        "total_pnl": total_pnl,
        "profit_factor": abs(sum(t["pnl_pct"] for t in wins) / sum(t["pnl_pct"] for t in losses)) if losses and wins else 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stocks", type=int, default=10, help="测试前N只自选股")
    args = parser.parse_args()
    watchlist = server.WATCHLIST[:args.stocks]
    param_grid = [
        (2.0, 1), (2.0, 2), (2.0, 3),
        (2.5, 1), (2.5, 2), (2.5, 3),
        (3.0, 2), (3.0, 3),
    ]
    print(f"回测 {len(watchlist)} 只股票，{len(param_grid)} 组参数\n")
    print(f"{'盈亏比':>6} {'持有':>4} {'交易数':>6} {'胜率%':>7} {'平均收益%':>9} {'总收益%':>9} {'盈亏因子':>8}")
    print("-" * 60)
    for min_rr, hold in param_grid:
        all_results = []
        for code in watchlist:
            try:
                symbol = server.symbol_for(code)
                history = server.fetch_history(symbol)
                bars = parse_bars(history)
                r = backtest_stock(code, bars, min_rr, hold)
                all_results.append(r)
            except Exception:
                continue
        total_trades = sum(r["trades"] for r in all_results)
        total_wins = sum(r["wins"] for r in all_results)
        total_pnl = sum(r["total_pnl"] for r in all_results)
        win_rate = total_wins / total_trades * 100 if total_trades else 0
        avg_pnl = total_pnl / total_trades if total_trades else 0
        gross_win = sum(r["total_pnl"] * r["win_rate"] / 100 for r in all_results)
        gross_loss = sum(r["total_pnl"] * (1 - r["win_rate"] / 100) for r in all_results)
        pf = abs(gross_win / gross_loss) if gross_loss != 0 else 0
        print(f"{min_rr:>6.1f} {hold:>4}d {total_trades:>6} {win_rate:>7.1f} {avg_pnl:>9.2f} {total_pnl:>9.1f} {pf:>8.2f}")


if __name__ == "__main__":
    main()

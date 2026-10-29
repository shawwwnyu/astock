"""Immutable signal observations and forward-session directional evaluation."""
from __future__ import annotations

import copy
from datetime import date

from ai_strategy import digest


def observe(state: dict, analysis: dict, actions: list[dict]) -> None:
    ai = analysis.get("ai_decision")
    if not ai:
        return
    observations = state.setdefault("signal_observations", {})
    stocks = {item["code"]: item for item in analysis.get("universe", [])}
    rows = []
    for row in ai["buys"]:
        rows.append({**row, "side": "buy", "price": stocks[row["code"]]["price"], "holding_id": ""})
    for row in analysis.get("rule_sell_signals", []):
        verdict = next((item for item in ai["sells"] if item["holding_id"] == row["holding_id"]), None)
        rows.append({
            **row, "side": "sell",
            "approve": True if row["hard_exit"] else bool(verdict and verdict["approve"]),
            "reason": "硬性退出，不受AI否决" if row["hard_exit"] else (
                verdict["reason"] if verdict else "AI不可用，未确认非硬性退出"
            ),
        })
    # Unavailable AI is distinct from a reasoned veto.
    if ai["status"] == "unavailable":
        rows += [{**row, "side": "buy", "holding_id": "", "approve": False,
                  "reason": "AI不可用，不视为AI否决样本"} for row in analysis.get("rule_buy_signals", [])]
    for row in rows:
        status = "approved" if row["approve"] else ("unavailable" if ai["status"] == "unavailable" else "vetoed")
        key = digest([analysis["trade_date"], row["code"], row["side"], row["holding_id"], status])
        observation = observations.setdefault(key, {
            "id": key, "trade_date": analysis["trade_date"], "as_of": analysis["data_as_of"],
            "code": row["code"], "side": row["side"], "holding_id": row["holding_id"],
            "price": row["price"], "status": status, "reason": row["reason"],
            "origin": "hard_exit" if row.get("hard_exit") else "ai",
            "selected_for_plan": row["approve"] and (
                row["side"] == "sell" or any(item["code"] == row["code"] for item in analysis.get("candidates", []))
            ),
            "parameters": copy.deepcopy(analysis["parameters"]), "ai": copy.deepcopy(ai),
            "notification_ids": [],
        })
        for action in actions:
            if (action["code"], action["side"]) == (row["code"], row["side"]):
                fingerprint = action.get("notification_fingerprint")
                if fingerprint and fingerprint not in observation["notification_ids"]:
                    observation["notification_ids"].append(fingerprint)


def evaluate(state: dict, start: date, end: date, histories: dict[str, list[dict]],
             calendar: list[str] | None = None, adjusted: bool = False) -> dict:
    samples = []
    missing = []
    for observation in state.get("signal_observations", {}).values():
        signal_day = date.fromisoformat(observation["trade_date"][:4] + "-" + observation["trade_date"][4:6] + "-" + observation["trade_date"][6:])
        if signal_day > end:
            continue
        bars = {bar["date"]: bar for bar in histories.get(observation["code"], [])}
        sessions = sorted(set(calendar if calendar is not None else bars))
        if signal_day.isoformat() not in sessions and calendar is not None:
            if signal_day >= start:
                missing.append(f"{observation['code']} 信号日期缺少交易日核验")
            continue
        future = [value for value in sessions if signal_day.isoformat() < value <= end.isoformat()]
        reference = observation["price"]
        if adjusted:
            raw_close = observation.get("signal_day_close")
            adjusted_close = bars.get(signal_day.isoformat(), {}).get("close")
            if not raw_close or not adjusted_close:
                if signal_day >= start or (len(future) >= 3 and future[2] >= start.isoformat()):
                    missing.append(f"{observation['code']} 缺少信号日真实收盘价或同口径日线，无法核验复权收益")
                continue
            reference *= adjusted_close / raw_close
        horizons = {}
        for horizon in (1, 2, 3):
            if len(future) >= horizon and future[horizon - 1] in bars:
                bar = bars[future[horizon - 1]]
                change = (bar["close"] / reference - 1) * 100
                directional = change if observation["side"] == "buy" else -change
                horizons[str(horizon)] = {
                    "date": bar["date"], "return_pct": round(change, 4),
                    "directional_return_pct": round(directional, 4), "correct": directional > 0,
                }
        matured = horizons.get("3")
        # Daily reports include old signals maturing today, not just today's immature cohort.
        if signal_day < start and not (matured and start.isoformat() <= matured["date"] <= end.isoformat()):
            if len(future) >= 3 and future[2] >= start.isoformat() and not matured:
                missing.append(f"{observation['code']} 缺少第3个交易日收盘价")
            continue
        if any(value not in bars for value in future[:3]):
            missing.append(f"{observation['code']} 信号后交易日行情不完整")
        ids = observation.get("notification_ids", [])
        linked = any(f"signal:{identity}" in state["actual_trade_receipts"] for identity in ids)
        delivered = any(identity in state["delivered_signals"] for identity in ids)
        samples.append({
            **{key: observation[key] for key in ("id", "code", "side", "trade_date", "status", "reason")},
            "execution": "linked_fill" if linked else ("delivered_no_linked_fill" if delivered else "not_delivered"),
            "origin": observation.get("origin", "ai"),
            "selected_for_plan": observation.get("selected_for_plan", False),
            "parameters": observation.get("parameters", {}),
            "horizons": horizons, "matured": matured is not None,
        })
    metrics = {}
    for side in ("buy", "sell"):
        for status in ("approved", "vetoed"):
            cohort = [sample["horizons"]["3"] for sample in samples
                      if sample["side"] == side and sample["status"] == status and sample["matured"]]
            metrics[f"{side}_{status}"] = {
                "count": len(cohort),
                "directional_hit_rate": sum(item["correct"] for item in cohort) / len(cohort) if cohort else None,
                "mean_directional_return_pct": round(sum(item["directional_return_pct"] for item in cohort) / len(cohort), 4) if cohort else None,
            }
    return {
        "samples": samples, "metrics": metrics,
        "matured_count": sum(sample["matured"] and sample["status"] != "unavailable" for sample in samples),
        "pending_count": sum(not sample["matured"] for sample in samples),
        "missing_data": sorted(set(missing)),
        "definition": "信号后第1/2/3个交易日收盘相对信号价（用信号日真实收盘价校准复权口径）；买入上涨/卖出后下跌为方向正确。"
                      "这是未计费用的前瞻方向指标，不是实际收益或因果证明。否决样本单列，缺失/未到期不算失败；"
                      "无关联成交仅表示未通过信号表单登记，不代表用户未操作。同股同日同方向同裁决只取首次快照。",
    }

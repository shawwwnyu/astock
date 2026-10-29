"""有界、可审计的晚间规则更新，不修改资金与数据核验约束。"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import date, datetime, timedelta


BASELINE = {"minimum_reward_risk": 2.0, "max_holding_sessions": 3}
LIMITS = {"minimum_reward_risk": (2.0, 3.0), "max_holding_sessions": (1, 3)}


def validated(parameters: dict) -> dict:
    if not isinstance(parameters, dict) or set(parameters) != set(BASELINE):
        raise ValueError("自动规则只能包含筛选盈亏比和最长持有期")
    for key, (low, high) in LIMITS.items():
        value = parameters[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"规则参数 {key} 超出允许边界")
    if not isinstance(parameters["max_holding_sessions"], int):
        raise ValueError("最长持有期必须为整数")
    return copy.deepcopy(parameters)


def effective_parameters(state: dict, trade_date: str) -> dict:
    day = date.fromisoformat(f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}")
    result = dict(BASELINE)
    revisions = state.get("strategy_adjustments", [])
    if not isinstance(revisions, list):
        raise ValueError("规则变更记录格式无效")
    for revision in sorted(revisions, key=lambda item: item["effective_date"]):
        if date.fromisoformat(revision["effective_date"]) <= day:
            result = validated(revision["after"])
    return result


def plan_update(state: dict, end: date) -> dict:
    """日/周复盘使用同一份当日成交证据，避免周五双重调参。"""
    prior = [item for item in state.get("strategy_adjustments", [])
             if date.fromisoformat(item["review_date"]) < end]
    # 周末也执行复盘；保留周五已排定、尚未生效的周一规则。
    before = validated(max(prior, key=lambda item: item["review_date"])["after"]) if prior else dict(BASELINE)
    sales = [item for item in state["actual_history"]
             if str(item["sell_date"]).replace("-", "") == end.strftime("%Y%m%d")]
    # 同一持仓多次部分卖出仍只算一个样本。
    entries = {}
    for item in sales:
        key = item["id"]
        entries[key] = entries.get(key, 0) + (item["sell_price"] - item["buy_price"]) * item["shares"]
    pnl = round(sum(entries.values()), 2)
    after = dict(before)
    if pnl < 0:
        after["minimum_reward_risk"] = round(min(3.0, before["minimum_reward_risk"] + 0.2), 1)
        after["max_holding_sessions"] = max(1, before["max_holding_sessions"] - 1)
        reason = "当日卖出合计亏损：提高入场盈亏比要求、缩短最长持有期。"
    elif pnl > 0:
        after["minimum_reward_risk"] = round(max(2.0, before["minimum_reward_risk"] - 0.1), 1)
        after["max_holding_sessions"] = min(3, before["max_holding_sessions"] + 1)
        reason = "当日卖出合计盈利：向原始基线小步恢复，不突破原始宽松程度。"
    else:
        reason = "没有已实现盈亏证据或合计持平，保持当前规则。"
    source = sorted([
        {key: item[key] for key in ("id", "code", "shares", "buy_price", "sell_price", "sell_date")}
        for item in sales
    ], key=lambda item: json.dumps(item, sort_keys=True))
    digest = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
    effective = end + timedelta(days=1)
    while effective.weekday() >= 5:
        effective += timedelta(days=1)
    return {
        "review_date": end.isoformat(), "effective_date": effective.isoformat(),
        "before": before, "after": validated(after), "reason": reason,
        "sample_count": len(entries), "realized_pnl": pnl, "evidence_hash": digest,
        "method": "bounded-realized-pnl-v1",
        "warning": "这是预设的盈亏响应规则，不是经回测验证的最优参数；未计费用，不保证改善收益。",
    }


def apply_update(state: dict, end: date, now: datetime) -> dict:
    if end != now.date() or now.hour < 20:
        raise ValueError("自动调参只允许北京时间当天20:00后执行，历史复盘不改规则")
    planned = plan_update(state, end)
    revisions = state.setdefault("strategy_adjustments", [])
    existing = next((item for item in revisions if item["review_date"] == end.isoformat()), None)
    if existing is not None:
        if existing["evidence_hash"] != planned["evidence_hash"]:
            raise ValueError("当晚调参后成交账本发生变化，停止再次修改；请核对记录")
        return {**existing, "status": "already_applied"}
    revision = {**planned, "created_at": now.isoformat(timespec="seconds")}
    revisions.append(revision)
    return {**revision, "status": "applied" if planned["after"] != planned["before"] else "unchanged"}


MIN_AI_SAMPLES = 10


def plan_ai_update(state: dict, report: dict) -> dict:
    """AI reviews evidence, but can change only the two authorized parameters."""
    from ai_strategy import digest, reason, structured
    import llm

    end = date.fromisoformat(report["end"])
    baseline = plan_update(state, end)
    before = baseline["before"]
    evidence = {
        key: report[key] for key in ("start", "end", "period", "buys", "sells", "realized_pnl",
                                     "period_pnl", "floating_pnl", "missing_data", "signal_review")
    }
    evidence["before"] = before
    evidence["ledger_hash"] = digest([state["actual_holdings"], state["actual_history"]])
    evidence["previous_adjustments"] = [
        {key: item[key] for key in ("review_date", "effective_date", "before", "after", "reason")}
        for item in state.get("strategy_adjustments", []) if item["review_date"] < report["end"]
    ][-7:]
    evidence_hash = digest(evidence)
    cached = state.get("ai_reviews", {}).get(evidence_hash)
    if cached:
        return copy.deepcopy(cached)
    planned = {
        **baseline, "after": dict(before), "evidence_hash": evidence_hash,
        "method": "ai-signal-review-v1", "model": llm.MODEL,
        "period": report["period"], "period_start": report["start"], "period_end": report["end"],
        "realized_pnl": report["realized_pnl"],
        "ledger_hash": evidence["ledger_hash"],
        "sample_count": len({
            (sample["code"], sample["trade_date"], sample["side"])
            for sample in report["signal_review"]["samples"]
            if sample["matured"] and sample["status"] != "unavailable"
        }),
        "warning": "AI复盘不是回测验证，不保证提升收益。参数限2–3R及1–3个可卖交易日；每晚至多调整一次。",
    }
    try:
        answer = structured(
            "你是A股短线策略复盘员。评估已批准和被否决的买卖信号后1/2/3交易日方向表现，"
            "区分实际成交盈亏与未成交的反事实表现，未到期或行情缺失不可算错。"
            "分析误报、漏选风险、样本量和数据局限，不宣称必然改善。"
            "按证据直接决定是否修改参数，非给用户操作建议。"
            f"独立成熟样本不足{MIN_AI_SAMPLES}个或有数据缺失则保持不变。"
            "只能改 minimum_reward_risk（2至3，每次最多0.2）和max_holding_sessions（1至3整数，每次最多1），"
            "不得修改T+1、禁止加仓、仓位、数据公告核验。"
            '返回 {"analysis":"复盘结论，最多600字","reason":"是否调整及证据，最多600字",'
            '"after":{"minimum_reward_risk":2.0,"max_holding_sessions":3}}。',
            evidence,
        )
        if set(answer) != {"analysis", "reason", "after"}:
            raise ValueError("AI复盘字段无效")
        after = validated(answer["after"])
        if abs(after["minimum_reward_risk"] - before["minimum_reward_risk"]) > 0.200001 or abs(
            after["max_holding_sessions"] - before["max_holding_sessions"]
        ) > 1:
            raise ValueError("AI调整幅度超出单次限制")
        planned.update({"analysis": reason(answer["analysis"]), "reason": reason(answer["reason"]),
                        "ai_status": "ok"})
        if planned["sample_count"] < MIN_AI_SAMPLES or report["missing_data"] or report["signal_review"].get("missing_data"):
            planned["reason"] = f"成熟独立样本不足{MIN_AI_SAMPLES}个或行情不完整，保持规则。" + planned["reason"]
        else:
            planned["after"] = after
    except (RuntimeError, ValueError) as error:
        import sys
        print(f"[AI复盘] {error}；不修改规则", file=sys.stderr)
        planned.update({"ai_status": "unavailable", "analysis": f"AI复盘不可用：{error}",
                        "reason": "AI复盘不可用，保持现有规则，不回退到按盈亏机械调参。"})
    return planned


def apply_ai_update(state: dict, planned: dict, now: datetime) -> dict:
    if planned["review_date"] != now.date().isoformat() or now.hour < 20:
        raise ValueError("自动调参只允许北京时间当天20:00后执行")
    revisions = state.setdefault("strategy_adjustments", [])
    existing = next((item for item in revisions if item["review_date"] == planned["review_date"]), None)
    if existing:
        if existing.get("ledger_hash") and existing["ledger_hash"] != planned["ledger_hash"]:
            raise ValueError("当晚调参后成交账本发生变化，停止再次修改；请核对记录")
        return {**existing, "status": "already_applied"}
    # Failed AI must not consume tonight's adjustment slot; a later retry can recover.
    if planned["ai_status"] != "ok":
        return {**planned, "status": "unavailable"}
    validated(planned["after"])
    if planned["after"] == planned["before"]:
        return {**planned, "status": "unchanged"}
    revision = {**planned, "created_at": now.isoformat(timespec="seconds")}
    revisions.append(revision)
    return {**revision, "status": "applied"}

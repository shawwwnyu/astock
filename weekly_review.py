"""Offline, read-only weekly review for formally committed LightClaw events."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from statistics import median
from typing import Any

SCHEMA = "astock.weekly-review/v1"
ARCHIVE_SCHEMA = "astock.formal-event/v1"
STRATEGY_VERSION = "short-term-v2"
INTRADAY_MIGRATION_DATE = date(2026, 8, 26)
LEGACY_REQUIRED_STAGES = {"morning-summary", "morning-decision", "tail-decision", "close-summary"}
DEFAULT_PARAMETERS = {
    "capital": 50_000,
    "max_holding_sessions": 3,
    "minimum_reward_risk": 2.0,
    "yellow_position_pct": 30,
    "green_position_pct": 60,
}
COST_MODEL = {
    "commission_bps_each_side": 3,
    "minimum_commission_per_order": 5.0,
    "stock_sell_stamp_tax_bps": 5,
    "slippage_bps_each_side": 10,
    "etf_stamp_tax_bps": 0,
    "transfer_fee": 0.0,
    "rounding": "each order cost rounded up to CNY cent",
}
ALLOWED_CONFIG_KEYS = {
    "archive_dir", "trading_calendar", "benchmark_closes", "mark_prices",
    "initial_capital",
}
PREDEFINED_GRID = {
    "minimum_reward_risk": {1.8, 2.2},
    "max_holding_sessions": {2, 4},
    "position_pct": {50, 55},
}


class CorruptLocalData(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    temporary.replace(path)
    os.chmod(path, 0o600)
    parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def archive_path_for(state_path: Path, event_id: str) -> Path:
    safe_id = event_id.replace(":", "-").replace("/", "-")
    return state_path.parent / "formal-events" / f"{safe_id}.json"


def archive_content_hash(event: dict[str, Any]) -> str:
    return sha256_json({key: value for key, value in event.items() if key not in {"archive_status", "content_hash"}})


def seal_formal_event(event: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(event)
    sealed["content_hash"] = archive_content_hash(sealed)
    return sealed


def archive_formal_event(path: Path, event: dict[str, Any]) -> None:
    if event.get("schema") != ARCHIVE_SCHEMA or event.get("test_message") is not False:
        raise ValueError("only non-test formal events may enter the formal archive")
    if not event.get("event_id") or event.get("stage") not in LEGACY_REQUIRED_STAGES | {"intraday-scan"}:
        raise ValueError("invalid formal event")
    event = seal_formal_event(event)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CorruptLocalData(f"formal archive is unreadable: {path}: {error}") from error
        if existing.get("content_hash") != archive_content_hash(existing):
            raise CorruptLocalData(f"formal archive content hash is invalid: {path}")
        pending_transition = (
            existing.get("archive_status") == "pending"
            and event.get("archive_status") == "committed"
            and {**existing, "archive_status": "committed"} == event
        )
        if existing == event:
            os.chmod(path, 0o600)
            return
        if not pending_transition:
            raise CorruptLocalData(f"formal archive conflicts with event: {event['event_id']}")
    atomic_write_json(path, event)


def _next_minute(time_text: str) -> str:
    digits = time_text.replace(":", "")
    minutes = int(digits[:2]) * 60 + int(digits[2:]) + 1
    return f"{minutes // 60:02d}{minutes % 60:02d}"


def execution_evidence(report: dict[str, Any], minute_rows: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = minute_rows or {}
    decision_minute = report.get("scan_time") if report.get("stage") == "intraday-scan" else report.get("analysis", {}).get("market_data_time", "")
    cutoff = str(decision_minute).replace(":", "")
    expected = _next_minute(cutoff) if len(cutoff) == 4 and cutoff.isdigit() else ""
    actions: list[dict[str, Any]] = []
    raw_actions = []
    for stock in report.get("buys", []):
        raw_actions.append(("buy", stock, stock.get("price"), stock.get("shares")))
    for review in report.get("holding_reviews", []):
        if review.get("decision") == "sell":
            holding = review.get("holding", {})
            raw_actions.append(("sell", holding, review.get("current_price"), holding.get("shares")))
    for side, item, reference_price, shares in raw_actions:
        code = str(item.get("code", ""))
        symbol = ("sh" if code.startswith(("5", "6")) else "sz") + code
        records = rows.get(symbol, {}).get("records", []) if isinstance(rows.get(symbol), dict) else []
        record = next((row for row in records if str(row.get("time", "")).replace(":", "") == expected), None)
        raw_record = None
        if isinstance(record, dict):
            raw_record = {key: record.get(key) for key in ("time", "price", "volume", "suspended", "at_limit") if key in record}
        identity = str(item.get("id", "")) if side == "sell" else ""
        actions.append({
            "action_id": f"{report.get('event_id')}:{side}:{code}" + (f":{identity}" if identity else ""),
            "side": side,
            "code": code,
            "name": item.get("name", ""),
            "shares": int(shares or 0),
            "signal_price": reference_price,
            "expected_execution_minute": expected,
            "raw_next_minute": raw_record,
        })
    return actions


def verify_archived_action(action: dict[str, Any]) -> dict[str, Any]:
    verified = dict(action)
    row = action.get("raw_next_minute")
    expected = str(action.get("expected_execution_minute", ""))
    valid = (
        isinstance(row, dict)
        and str(row.get("time", "")).replace(":", "") == expected
        and isinstance(row.get("price"), (int, float))
        and math.isfinite(float(row["price"])) and float(row["price"]) > 0
        and isinstance(row.get("volume"), (int, float)) and float(row["volume"]) > 0
        and row.get("suspended") is False
        and row.get("at_limit") is False
    )
    if valid:
        verified.update({
            "status": "filled", "filled_price": float(row["price"]),
            "tradability_verified": True, "reason": "raw next-minute evidence passed deterministic fill rules",
        })
    else:
        reference = isinstance(row, dict) and row.get("price") is not None and row.get("volume") is not None
        verified.update({
            "status": "reference_only" if reference else "unfilled", "filled_price": None,
            "tradability_verified": False,
            "reason": "raw evidence cannot prove suspension, limit and positive-volume execution" if reference else "raw next-minute evidence unavailable",
        })
    return verified


def _counterfactual_archive_inputs(
    report: dict[str, Any], actions: list[dict[str, Any]], snapshot: dict[str, Any],
) -> dict[str, Any]:
    analysis = report.get("analysis", {})
    payload = {
        "schema": "astock.counterfactual-inputs/v1",
        "complete": False,
        "reason": "counterfactual_inputs_unavailable",
        "missing": [
            "raw_daily_bars", "raw_benchmark_bars", "raw_announcement_evidence",
            "pre_stage_portfolio", "verified_15_00_marks",
        ],
        "strategy_version": analysis.get("strategy_version", STRATEGY_VERSION),
        "baseline_parameters": dict(analysis.get("parameters", {})),
        "predefined_grid": {key: sorted(values) for key, values in PREDEFINED_GRID.items()},
        "stock_pool": [str(code) for code in analysis.get("stock_pool", [])],
        "stage": report.get("stage"),
        "signal_visible_snapshot": snapshot,
        "raw_execution_evidence": actions,
    }
    return {**payload, "input_hash": sha256_json(payload)}


def build_formal_event(
    pending: dict[str, Any], committed_at: datetime, minute_rows: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = pending["report"]
    analysis = report.get("analysis", {})
    universe = analysis.get("universe", [])
    stock_pool = [str(code) for code in analysis.get("stock_pool", [])]
    parameters = dict(DEFAULT_PARAMETERS)
    parameters.update(analysis.get("parameters", {}))
    parameters["capital"] = analysis.get("capital", parameters["capital"])
    snapshot = {
        "data_as_of": analysis.get("data_as_of"),
        "trade_date": analysis.get("trade_date"),
        "market_data_time": analysis.get("market_data_time"),
        "indices": analysis.get("indices", []),
        "universe": universe,
        "trading_dates": analysis.get("trading_dates", []),
    }
    actions = execution_evidence(report, minute_rows)
    return {
        "schema": ARCHIVE_SCHEMA,
        "version": 1,
        "event_id": report["event_id"],
        "stage": report["stage"],
        "decision_at": report.get("decision_at") or analysis.get("generated_at") or pending.get("prepared_at"),
        "strategy_version": analysis.get("strategy_version", STRATEGY_VERSION),
        "parameters": parameters,
        "universe_version": analysis.get("universe_version", "legacy-unversioned"),
        "stock_pool": stock_pool,
        "stock_pool_hash": analysis.get("stock_pool_hash") or sha256_json(stock_pool),
        "report": report,
        "actions": actions,
        "market_snapshot": snapshot,
        "counterfactual_inputs": _counterfactual_archive_inputs(report, actions, snapshot),
        "benchmark": next((row for row in analysis.get("indices", []) if row.get("symbol") == "sh000300"), None),
        "test_message": False,
        "formal": True,
        "committed_at": committed_at.isoformat(timespec="seconds"),
    }


def load_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"weekly config cannot be read: {error}") from error
    if not isinstance(value, dict) or set(value) - ALLOWED_CONFIG_KEYS:
        raise ValueError("weekly config contains unsupported keys")
    return value


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError) as error:
        raise ValueError("--as-of must be ISO date or datetime") from error


def _week_period(as_of: date) -> tuple[date, date]:
    start = as_of - timedelta(days=as_of.weekday())
    return start, start + timedelta(days=6)


def _money_up(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _verified_close(source: Any, day: str, label: str) -> float | None:
    if not isinstance(source, dict):
        return None
    price = source.get("price")
    as_of = str(source.get("as_of", ""))
    if not isinstance(price, (int, float)) or not math.isfinite(float(price)):
        return None
    if not as_of.startswith(day) or not (as_of.endswith("15:00") or as_of.endswith("15:00:00")):
        return None
    if source.get("verified_at_time") is not True:
        return None
    return float(price)


def is_etf(code: str) -> bool:
    return code.startswith(("5", "1"))


def order_cost(side: str, code: str, shares: int, price: float) -> dict[str, float]:
    notional = Decimal(str(shares)) * Decimal(str(price))
    commission = _money_up(max(Decimal("5"), notional * Decimal("0.0003")))
    slippage = _money_up(notional * Decimal("0.001"))
    stamp = Decimal("0")
    if side == "sell" and not is_etf(code):
        stamp = _money_up(notional * Decimal("0.0005"))
    return {"commission": float(commission), "stamp_tax": float(stamp), "slippage": float(slippage)}


def _load_events(archive_dir: Path, end: date) -> list[dict[str, Any]]:
    if not archive_dir.exists():
        return []
    events = []
    seen = set()
    for path in sorted(archive_dir.glob("*.json")):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CorruptLocalData(f"archive is corrupt: {path}: {error}") from error
        if not isinstance(event, dict) or event.get("schema") != ARCHIVE_SCHEMA:
            raise CorruptLocalData(f"archive schema is invalid: {path}")
        if event.get("content_hash") != archive_content_hash(event):
            raise CorruptLocalData(f"archive content hash is invalid: {path}")
        pool = event.get("stock_pool")
        if not isinstance(pool, list) or sha256_json(pool) != event.get("stock_pool_hash"):
            raise CorruptLocalData(f"archive stock pool hash is invalid: {path}")
        if not isinstance(event.get("actions"), list):
            raise CorruptLocalData(f"archive actions are invalid: {path}")
        counterfactual_inputs = event.get("counterfactual_inputs")
        if counterfactual_inputs is not None:
            if not isinstance(counterfactual_inputs, dict):
                raise CorruptLocalData(f"counterfactual inputs are invalid: {path}")
            supplied_hash = counterfactual_inputs.get("input_hash")
            payload = {key: value for key, value in counterfactual_inputs.items() if key != "input_hash"}
            if supplied_hash != sha256_json(payload):
                raise CorruptLocalData(f"counterfactual input hash is invalid: {path}")
        event["actions"] = [verify_archived_action(action) for action in event["actions"] if isinstance(action, dict)]
        event_id = event.get("event_id")
        if event_id in seen:
            continue
        seen.add(event_id)
        if (
            event.get("formal") is True
            and event.get("test_message") is False
            and event.get("archive_status") == "committed"
            and event.get("committed_at")
        ):
            trade_date = _parse_date(str(event_id).split(":", 1)[0].replace("/", "-") if "-" in str(event_id).split(":", 1)[0] else f"{str(event_id)[:4]}-{str(event_id)[4:6]}-{str(event_id)[6:8]}")
            if trade_date <= end:
                events.append(event)
    stage_order = {"morning-summary": 0, "morning-decision": 1, "tail-decision": 2, "intraday-scan": 3, "close-summary": 4}
    events.sort(key=lambda item: (
        _event_day(item), str(item.get("decision_at", "")),
        stage_order.get(str(item.get("stage", "")), 99), str(item.get("event_id", "")),
    ))
    return events


def _max_drawdown(equity: list[dict[str, Any]]) -> float | None:
    if not equity:
        return None
    peak = float(equity[0]["equity"])
    worst = 0.0
    for row in equity:
        value = float(row["equity"])
        peak = max(peak, value)
        if peak:
            worst = max(worst, (peak - value) / peak)
    return round(worst * 100, 4)


def _event_day(event: dict[str, Any]) -> str:
    digits = str(event.get("event_id", "")).split(":", 1)[0].replace("-", "")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _day_is_complete(day: str, stages: set[str]) -> bool:
    if _parse_date(day) < INTRADAY_MIGRATION_DATE:
        return stages == LEGACY_REQUIRED_STAGES
    return "close-summary" in stages and stages <= {"close-summary", "intraday-scan"}


def _candidate_evaluations(samples: Any, eligible: bool) -> list[dict[str, Any]]:
    """Legacy producer-reported counterfactual results are never trusted."""
    return []


def _counterfactual_candidates(events: list[dict[str, Any]], eligible: bool) -> tuple[list[dict[str, Any]], str]:
    if not eligible:
        return [], "eligibility_threshold_not_met"
    for event in events:
        inputs = event.get("counterfactual_inputs")
        if not isinstance(inputs, dict):
            continue
        supplied_hash = inputs.get("input_hash")
        payload = {key: value for key, value in inputs.items() if key != "input_hash"}
        if supplied_hash != sha256_json(payload):
            raise CorruptLocalData("counterfactual input hash is invalid")
    return [], "counterfactual_inputs_unavailable"


def weekly_review(as_of_text: str, archive_dir: Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    as_of = _parse_date(as_of_text)
    start, end = _week_period(as_of)
    events = _load_events(archive_dir, end)
    current = [event for event in events if start.isoformat() <= _event_day(event) <= end.isoformat()]
    calendar_evidence = config.get("trading_calendar", {})
    calendar_verified = (
        isinstance(calendar_evidence, dict) and calendar_evidence.get("complete") is True
        and isinstance(calendar_evidence.get("source"), str) and bool(calendar_evidence.get("source", "").strip())
        and isinstance(calendar_evidence.get("dates"), list)
    )
    calendar = sorted(str(value)[:10] for value in calendar_evidence.get("dates", [])) if calendar_verified else []
    expected_days = [value for value in calendar if start.isoformat() <= value <= end.isoformat()]
    observed_stages: defaultdict[str, set[str]] = defaultdict(set)
    for event in current:
        observed_stages[_event_day(event)].add(str(event.get("stage", "")))
    errors: list[str] = []
    if not events:
        errors.append("no formally committed post-launch events")
    if not calendar_verified:
        errors.append("verified complete trading-day calendar unavailable")
    elif not expected_days:
        errors.append("actual trading-day calendar unavailable")
    elif any(not _day_is_complete(day, observed_stages.get(day, set())) for day in expected_days):
        errors.append("formal daily ledger is incomplete for actual trading days")

    initial = float(config.get("initial_capital", 50_000))
    cash = initial
    lots: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    costs = {"commission": 0.0, "stamp_tax": 0.0, "slippage": 0.0}
    realized: list[float] = []
    stage_pnl: defaultdict[str, float] = defaultdict(float)
    security_pnl: defaultdict[str, float] = defaultdict(float)
    execution_ledger: list[dict[str, Any]] = []
    filled_orders = 0
    completed_trades = 0

    def apply_action(action: dict[str, Any], day: str, stage: str, is_current: bool) -> None:
        nonlocal cash, filled_orders, completed_trades
        if action.get("status") != "filled" or action.get("tradability_verified") is not True:
            if is_current:
                execution_ledger.append({"action_id": action.get("action_id"), "status": action.get("status", "unfilled"), "requested_shares": int(action.get("shares", 0) or 0), "executed_shares": 0, "unfilled_shares": int(action.get("shares", 0) or 0)})
            return
        code = str(action.get("code", ""))
        requested = max(0, int(action.get("shares", 0) or 0))
        price = float(action["filled_price"])
        side = str(action.get("side"))
        executed = requested
        if side == "sell":
            executed = min(requested, sum(int(lot["shares"]) for lot in lots[code] if lot["buy_date"] < day))
        if executed <= 0:
            if is_current:
                execution_ledger.append({"action_id": action.get("action_id"), "status": "rejected", "requested_shares": requested, "executed_shares": 0, "unfilled_shares": requested, "reason": "T+1 sellable quantity unavailable"})
            return
        fee = order_cost(side, code, executed, price)
        total_fee = sum(fee.values())
        filled_orders += 1
        if is_current:
            for key in costs:
                costs[key] += fee[key]
        if side == "buy":
            cash -= executed * price + total_fee
            lots[code].append({"shares": executed, "price": price, "buy_fees": total_fee, "buy_date": day})
        elif side == "sell":
            remaining = executed
            net = -(total_fee if is_current else 0.0)
            for lot in list(lots[code]):
                if remaining <= 0 or lot["buy_date"] >= day:
                    continue
                quantity = min(remaining, int(lot["shares"]))
                allocated_fee = float(lot["buy_fees"]) * quantity / int(lot["shares"])
                net += quantity * (price - float(lot["price"])) - allocated_fee
                lot["buy_fees"] -= allocated_fee
                lot["shares"] -= quantity
                remaining -= quantity
                if lot["shares"] == 0:
                    lots[code].remove(lot)
            cash += executed * price - total_fee
            completed_trades += 1
            if is_current:
                realized.append(net)
                security_pnl[code] += net
                stage_pnl[stage] += net
        if is_current:
            execution_ledger.append({
                "action_id": action.get("action_id"), "status": "filled" if executed == requested else "partially_filled",
                "requested_shares": requested, "executed_shares": executed, "unfilled_shares": requested - executed,
                "price": price, "fees": fee,
            })

    prior = [event for event in events if _event_day(event) < start.isoformat()]
    for event in prior:
        for action in event.get("actions", []):
            apply_action(action, _event_day(event), str(event.get("stage", "unknown")), False)

    mark_prices = config.get("mark_prices", {})
    opening_equity = None
    opening_cash = round(cash, 2)
    opening_holdings = {code: sum(int(lot["shares"]) for lot in code_lots) for code, code_lots in lots.items() if code_lots}
    if expected_days:
        opening_day = expected_days[0]
        opening_marks = mark_prices.get(opening_day, {}) if isinstance(mark_prices, dict) else {}
        verified_opening = {code: _verified_close(source, opening_day, code) for code, source in opening_marks.items()} if isinstance(opening_marks, dict) else {}
        missing = [code for code in opening_holdings if verified_opening.get(code) is None]
        if missing:
            errors.append(f"verified week-opening 15:00 mark missing for {opening_day}: {','.join(sorted(missing))}")
        else:
            opening_equity = round(cash + sum(shares * float(verified_opening[code]) for code, shares in opening_holdings.items()), 2)

    equity = []
    for day in expected_days:
        for event in [item for item in current if _event_day(item) == day]:
            for action in event.get("actions", []):
                apply_action(action, day, str(event.get("stage", "unknown")), True)
        marks = mark_prices.get(day, {}) if isinstance(mark_prices, dict) else {}
        verified_marks = {code: _verified_close(source, day, code) for code, source in marks.items()} if isinstance(marks, dict) else {}
        held = {code: sum(int(lot["shares"]) for lot in code_lots) for code, code_lots in lots.items() if code_lots}
        missing = [code for code in held if verified_marks.get(code) is None]
        if missing:
            errors.append(f"verified 15:00 mark missing for {day}: {','.join(sorted(missing))}")
            continue
        value = cash + sum(shares * float(verified_marks[code]) for code, shares in held.items())
        equity.append({"date": day, "equity": round(value, 2), "mark_time": "15:00", "verified_at_time": True})

    benchmark_closes = config.get("benchmark_closes", {})
    benchmark_values = [value for day in expected_days if (value := _verified_close(benchmark_closes.get(day), day, "sh000300")) is not None]
    benchmark_return = (benchmark_values[-1] / benchmark_values[0] - 1) * 100 if len(benchmark_values) >= 2 else None
    if benchmark_return is None:
        errors.append("CSI 300 sh000300 benchmark data incomplete")
    stages_by_day: defaultdict[str, set[str]] = defaultdict(set)
    for event in events:
        stages_by_day[_event_day(event)].add(str(event.get("stage", "")))
    calendar_weeks: defaultdict[tuple[int, int], list[str]] = defaultdict(list)
    for day in calendar:
        parsed = _parse_date(day)
        calendar_weeks[(parsed.isocalendar().year, parsed.isocalendar().week)].append(day)
    complete_weeks = sum(bool(days) and all(_day_is_complete(day, stages_by_day.get(day, set())) for day in days) for days in calendar_weeks.values())
    complete = bool(expected_days) and not any("incomplete" in error or "mark missing" in error for error in errors)
    available = complete and benchmark_return is not None and bool(equity) and opening_equity is not None
    status = "observational" if available else ("observational" if filled_orders else "insufficient_data")
    ending = equity[-1]["equity"] if equity else None
    net_pnl = round(float(ending) - float(opening_equity), 2) if available and ending is not None else None
    gross_pnl = round(net_pnl + sum(costs.values()), 2) if net_pnl is not None else None
    net_return = net_pnl / opening_equity * 100 if net_pnl is not None and opening_equity else None
    excess = net_return - benchmark_return if net_return is not None and benchmark_return is not None else None
    wins = [value for value in realized if value > 0]
    losses = [value for value in realized if value < 0]
    eligible = complete_weeks >= 26 and completed_trades >= 60
    candidates, counterfactual_reason = _counterfactual_candidates(events, eligible)
    if eligible and not candidates:
        errors.append(f"candidate optimization unavailable: {counterfactual_reason}")
    performance = {
        "available": available,
        "assumption": "事后账本核算，不是历史策略回测",
        "week_opening_equity": opening_equity, "opening_cash": opening_cash, "opening_holdings": opening_holdings,
        "ending_equity": ending if available else None,
        "gross_pnl": gross_pnl, "net_pnl": net_pnl,
        "gross_return_pct": round(gross_pnl / opening_equity * 100, 4) if gross_pnl is not None and opening_equity else None,
        "net_return_pct": round(net_return, 4) if net_return is not None else None,
        "commission": round(costs["commission"], 2) if available else None,
        "stamp_tax": round(costs["stamp_tax"], 2) if available else None,
        "slippage": round(costs["slippage"], 2) if available else None,
        "benchmark_symbol": "sh000300", "benchmark_return_pct": round(benchmark_return, 4) if available and benchmark_return is not None else None,
        "excess_return_pct": round(excess, 4) if excess is not None and available else None,
        "daily_equity": equity if available else [], "max_drawdown_pct": _max_drawdown(equity) if available else None,
        "filled_orders": sum(row["status"] in {"filled", "partially_filled"} for row in execution_ledger),
        "unfilled_orders": sum(row["unfilled_shares"] > 0 for row in execution_ledger),
        "executions": execution_ledger,
        "win_rate_pct": round(len(wins) / len(realized) * 100, 2) if realized else None,
        "profit_loss_ratio": round((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 4) if wins and losses else None,
    }
    adjustment = {
        "triggered": bool(candidates) if status != "insufficient_data" else False, "drawdown_triggered": False,
        "eligible": eligible,
        "eligibility": {"complete_weeks": complete_weeks, "minimum_weeks": 26, "filled_trades": completed_trades, "minimum_trades": 60},
        "method": "independent archived-input replay; predefined small grid; 22-week train and latest 4-week holdout",
        "reason": counterfactual_reason,
        "candidates": candidates if status != "insufficient_data" else [], "applied": False, "requires_user_confirmation": True,
    }
    return {
        "status": status, "schema": SCHEMA, "report_type": "weekly_review",
        "period": {"as_of": as_of.isoformat(), "week_start": start.isoformat(), "week_end": end.isoformat(), "actual_trading_days": expected_days},
        "data_quality": {"formal_events_only": True, "event_count": len(current), "complete": complete, "classification": status, "not_historical_unbiased_backtest": True},
        "cost_model": COST_MODEL, "performance": performance,
        "attribution": {"by_stage": dict(stage_pnl), "by_security": dict(security_pnl)}, "adjustment": adjustment,
        "message": "事后账本核算，不是历史策略回测；只读周复盘，候选须用户确认后另行版本化，绝不自动修改正式策略。",
        "errors": list(dict.fromkeys(errors)),
    }

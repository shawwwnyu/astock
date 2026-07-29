#!/usr/bin/env python3
"""Local live dashboard for a conservative A-share overnight screen."""

from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
SHANGHAI = ZoneInfo("Asia/Shanghai")
CAPITAL = 50_000
CACHE_SECONDS = 55

WATCHLIST = [
    "300740", "000909", "002080", "300007", "002318", "300398", "300847",
    "603023", "002938", "603322", "600707", "300413", "605488", "300757",
    "000901", "600258", "300545", "300220", "003016", "600426", "603131",
    "002346", "000555", "002230", "601138", "603181", "688037", "600809",
    "300722", "002714", "300493", "561360", "600779", "603369", "601288",
    "601669", "000625", "600104", "600309", "000596", "601633", "601668",
    "300433",
]

BENCHMARKS = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000688": "科创50",
}

RISK_KEYWORDS = (
    "立案", "处罚", "退市", "风险警示", "重大诉讼", "冻结", "减持", "解禁",
    "监管函", "问询函", "终止", "业绩预亏", "亏损", "异常波动", "停牌",
)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"expires": 0.0, "data": None}
_push_lock = threading.Lock()
_scheduler_stop = threading.Event()
PUSH_CONFIG_PATH = DATA_DIR / "wecom-push.json"


def symbol_for(code: str) -> str:
    if code.startswith(("5", "6")):
        return f"sh{code}"
    if code.startswith(("4", "8", "9")):
        return f"bj{code}"
    return f"sz{code}"


def as_number(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def fetch_json(url: str, timeout: int = 15, retries: int = 2) -> dict[str, Any]:
    last_error: Exception | None = None
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:  # Network providers fail in several different ways.
            last_error = error
            if attempt < retries:
                time.sleep(0.35 * (attempt + 1))
    raise RuntimeError(str(last_error) if last_error else "network request failed")


def validate_wecom_webhook(webhook: str) -> str:
    webhook = webhook.strip()
    parsed = urllib.parse.urlparse(webhook)
    key = urllib.parse.parse_qs(parsed.query).get("key", [""])[0]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "qyapi.weixin.qq.com"
        or parsed.path != "/cgi-bin/webhook/send"
        or len(key) < 20
    ):
        raise ValueError("必须使用企业微信群机器人生成的官方 Webhook 地址")
    return webhook


def load_push_config(include_secret: bool = False) -> dict[str, Any]:
    with _push_lock:
        if not PUSH_CONFIG_PATH.exists():
            config = {"enabled": False, "webhook": "", "last_sent": "", "last_result": "尚未配置"}
        else:
            try:
                config = json.loads(PUSH_CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                config = {"enabled": False, "webhook": "", "last_sent": "", "last_result": "配置文件损坏"}
    status = {
        "configured": bool(config.get("webhook")),
        "enabled": bool(config.get("enabled")),
        "last_sent": str(config.get("last_sent", "")),
        "last_result": str(config.get("last_result", "")),
        "schedule": "交易日 14:31",
        "channel": "企业微信群机器人",
    }
    if include_secret:
        status["webhook"] = str(config.get("webhook", ""))
    return status


def save_push_config(update: dict[str, Any]) -> dict[str, Any]:
    current = load_push_config(include_secret=True)
    webhook = str(update.get("webhook", "")).strip()
    if webhook:
        current["webhook"] = validate_wecom_webhook(webhook)
    if "enabled" in update:
        current["enabled"] = bool(update["enabled"])
    if current.get("enabled") and not current.get("webhook"):
        raise ValueError("启用自动推送前必须配置 Webhook")
    for key in ("last_sent", "last_result"):
        if key in update:
            current[key] = str(update[key])
    stored = {
        "webhook": current.get("webhook", ""),
        "enabled": bool(current.get("enabled")),
        "last_sent": current.get("last_sent", ""),
        "last_result": current.get("last_result", ""),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = PUSH_CONFIG_PATH.with_suffix(".tmp")
    with _push_lock:
        temporary.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(PUSH_CONFIG_PATH)
        os.chmod(PUSH_CONFIG_PATH, 0o600)
    return load_push_config()


def format_wecom_message(analysis: dict[str, Any], test: bool = False) -> str:
    prefix = "测试 · " if test else ""
    lines = [
        f"**{prefix}{analysis.get('trade_date', '')} 隔夜雷达**",
        f">结论：{analysis.get('headline', '未生成')}",
        f">市场：{analysis.get('market', {}).get('label', '未核验')} · 仓位：{analysis.get('position_pct', 0)}%",
        f">数据：{analysis.get('data_as_of', '未核验')}",
        "",
    ]
    candidates = analysis.get("candidates", [])
    if not candidates:
        lines.append("没有标的通过当前展示门槛。")
    for index, stock in enumerate(candidates[:3], start=1):
        failures = stock.get("failures", [])
        reason = failures[0] if failures else "通过当前自动硬门槛"
        lines.extend([
            f"**{index}. {stock.get('name', '')} {stock.get('code', '')}**",
            f">动作：{stock.get('action', '观察')} · 价格：{stock.get('price', '未核验')}",
            f">仓位：{stock.get('position_pct', 0)}% · 赔率：{stock.get('reward_risk', 0)}R",
            f">关键项：{reason}",
            "",
        ])
    lines.extend([
        "次日计划：09:25观察集合竞价，09:30-09:45执行，最迟10:00清仓可成交数量。",
        "仅供研究参考，不构成个性化投资建议。",
    ])
    return "\n".join(lines)[:3800]


def send_wecom_push(
    analysis: dict[str, Any], test: bool = False, webhook: str | None = None,
) -> dict[str, Any]:
    webhook = webhook or load_push_config(include_secret=True).get("webhook", "")
    if not webhook:
        raise RuntimeError("尚未配置企业微信 Webhook")
    webhook = validate_wecom_webhook(webhook)
    body = json.dumps({
        "msgtype": "markdown",
        "markdown": {"content": format_wecom_message(analysis, test=test)},
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("errcode") != 0:
        raise RuntimeError(f"企业微信返回错误：{result.get('errmsg', 'unknown')}")
    return result


def fetch_minute(symbol: str) -> dict[str, Any]:
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={urllib.parse.quote(symbol)}"
    payload = fetch_json(url)
    node = payload.get("data", {}).get(symbol)
    if payload.get("code") != 0 or not isinstance(node, dict):
        raise RuntimeError(f"invalid minute response for {symbol}")
    data = node.get("data", {})
    raw_records = data.get("data", [])
    quote = node.get("qt", {}).get(symbol, [])
    if not isinstance(raw_records, list) or not raw_records:
        raise RuntimeError(f"empty minute response for {symbol}")
    return {
        "symbol": symbol,
        "trade_date": str(data.get("date", "")),
        "raw_records": raw_records,
        "quote": quote if isinstance(quote, list) else [],
    }


def fetch_history(symbol: str) -> dict[str, Any]:
    param = urllib.parse.quote(f"{symbol},day,,,70,qfq")
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={param}"
    payload = fetch_json(url)
    node = payload.get("data", {}).get(symbol)
    if payload.get("code") != 0 or not isinstance(node, dict):
        raise RuntimeError(f"invalid history response for {symbol}")
    raw_rows = node.get("qfqday") or node.get("day") or []
    if not isinstance(raw_rows, list) or len(raw_rows) < 35:
        raise RuntimeError(f"insufficient history for {symbol}")
    return {"symbol": symbol, "raw_rows": raw_rows}


def fetch_announcements(code: str, trade_date: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({
        "sr": "-1", "page_size": "10", "page_index": "1", "ann_type": "A",
        "client_source": "web", "stock_list": code,
    })
    url = f"https://np-anotice-stock.eastmoney.com/api/security/ann?{query}"
    try:
        payload = fetch_json(url, timeout=12, retries=1)
        notices = payload.get("data", {}).get("list", [])
        if payload.get("success") != 1 or not isinstance(notices, list):
            raise RuntimeError("invalid announcement response")
        cutoff = datetime.strptime(trade_date, "%Y%m%d")
        recent = []
        risk_titles = []
        for notice in notices:
            title = str(notice.get("title", "")).strip()
            date_text = str(notice.get("notice_date", ""))[:10]
            try:
                notice_date = datetime.strptime(date_text, "%Y-%m-%d")
            except ValueError:
                continue
            if notice_date > cutoff or cutoff - notice_date > timedelta(days=10):
                continue
            item = {
                "title": title,
                "date": date_text,
                "url": f"https://data.eastmoney.com/notices/detail/{code}/{notice.get('art_code', '')}.html",
            }
            recent.append(item)
            if any(keyword in title for keyword in RISK_KEYWORDS):
                risk_titles.append(title)
        return {"verified": True, "recent": recent[:5], "risk_titles": risk_titles}
    except Exception as error:
        return {"verified": False, "recent": [], "risk_titles": [], "error": str(error)}


def hhmm_to_minutes(value: str) -> int:
    digits = value.replace(":", "")
    if len(digits) != 4 or not digits.isdigit():
        return -1
    return int(digits[:2]) * 60 + int(digits[2:])


def minutes_to_hhmm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def trading_progress(cutoff_minutes: int) -> float:
    morning_open = 9 * 60 + 30
    morning_close = 11 * 60 + 30
    afternoon_open = 13 * 60
    afternoon_close = 15 * 60
    if cutoff_minutes <= morning_open:
        elapsed = 0
    elif cutoff_minutes <= morning_close:
        elapsed = cutoff_minutes - morning_open
    elif cutoff_minutes < afternoon_open:
        elapsed = 120
    else:
        elapsed = 120 + min(cutoff_minutes, afternoon_close) - afternoon_open
    return max(0.0, min(1.0, elapsed / 240))


def parse_minute(minute: dict[str, Any], cutoff_minutes: int) -> dict[str, Any]:
    records = []
    for raw in minute["raw_records"]:
        parts = str(raw).split()
        if len(parts) < 4:
            continue
        minute_value = hhmm_to_minutes(parts[0])
        if minute_value < 0 or minute_value > cutoff_minutes:
            continue
        price = as_number(parts[1])
        volume = as_number(parts[2])
        amount = as_number(parts[3])
        if price is None or volume is None or amount is None:
            continue
        records.append({"time": parts[0], "minute": minute_value, "price": price, "volume": volume, "amount": amount})
    if not records:
        raise RuntimeError(f"no minute records before {minutes_to_hhmm(cutoff_minutes)}")
    last = records[-1]
    quote = minute.get("quote", [])
    name = str(quote[1]) if len(quote) > 1 else minute["symbol"]
    previous_close = as_number(quote[4] if len(quote) > 4 else None)
    vwap = last["amount"] / (last["volume"] * 100) if last["volume"] else None
    tail = [record for record in records if record["minute"] >= cutoff_minutes - 30]
    recent5 = [record for record in records if record["minute"] >= cutoff_minutes - 5]
    tail_prices = [record["price"] for record in tail] or [last["price"]]
    first_half = tail_prices[: max(1, len(tail_prices) // 2)]
    second_half = tail_prices[max(1, len(tail_prices) // 2):] or tail_prices
    return {
        "symbol": minute["symbol"],
        "code": minute["symbol"][2:],
        "name": name,
        "trade_date": minute["trade_date"],
        "last_time": last["time"],
        "price": last["price"],
        "previous_close": previous_close,
        "change_pct": ((last["price"] / previous_close - 1) * 100) if previous_close else None,
        "open": records[0]["price"],
        "high": max(record["price"] for record in records),
        "low": min(record["price"] for record in records),
        "volume": last["volume"],
        "amount": last["amount"],
        "vwap": vwap,
        "tail_return_pct": (tail_prices[-1] / tail_prices[0] - 1) * 100 if len(tail_prices) > 1 else 0.0,
        "tail_pullback_pct": (tail_prices[-1] / max(tail_prices) - 1) * 100,
        "tail_low_delta_pct": (min(second_half) / min(first_half) - 1) * 100 if first_half and second_half else 0.0,
        "spike_5m_pct": (recent5[-1]["price"] / recent5[0]["price"] - 1) * 100 if len(recent5) > 1 else 0.0,
        "records": records,
    }


def parse_history(history: dict[str, Any], before_date: str) -> list[dict[str, float | str]]:
    bars = []
    date_limit = f"{before_date[:4]}-{before_date[4:6]}-{before_date[6:8]}"
    for row in history["raw_rows"]:
        if len(row) < 6 or str(row[0]) >= date_limit:
            continue
        numbers = [as_number(row[index]) for index in (1, 2, 3, 4, 5)]
        if any(value is None for value in numbers):
            continue
        open_price, close, high, low, volume = numbers
        bars.append({"date": str(row[0]), "open": open_price, "close": close, "high": high, "low": low, "volume": volume})
    return bars[-69:]


def rolling_average(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < period:
            result.append(None)
        else:
            result.append(fmean(values[index + 1 - period:index + 1]))
    return result


def ema_series(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    result = []
    for value in values:
        result.append(value if not result else alpha * value + (1 - alpha) * result[-1])
    return result


def calculate_indicators(bars: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [float(bar["close"]) for bar in bars]
    ma5 = rolling_average(closes, 5)
    ma10 = rolling_average(closes, 10)
    ma20 = rolling_average(closes, 20)
    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    dif = [short - long for short, long in zip(ema12, ema26)]
    dea = ema_series(dif, 9)
    histogram = [2 * (left - right) for left, right in zip(dif, dea)]
    chart_bars = []
    for index, bar in enumerate(bars):
        chart_bars.append({
            **bar,
            "ma5": ma5[index], "ma10": ma10[index], "ma20": ma20[index],
            "dif": dif[index], "dea": dea[index], "histogram": histogram[index],
        })
    current = closes[-1]
    ma5_value, ma10_value, ma20_value = ma5[-1], ma10[-1], ma20[-1]
    if None not in (ma5_value, ma10_value, ma20_value) and current > ma5_value > ma10_value > ma20_value:
        trend = "多头排列"
    elif None not in (ma5_value, ma10_value, ma20_value) and current < ma5_value < ma10_value < ma20_value:
        trend = "空头排列"
    else:
        trend = "震荡混合"
    if len(dif) > 1 and dif[-2] <= dea[-2] and dif[-1] > dea[-1]:
        macd = "金叉"
    elif len(dif) > 1 and dif[-2] >= dea[-2] and dif[-1] < dea[-1]:
        macd = "死叉"
    elif dif[-1] > dea[-1]:
        macd = "DIF在DEA上方"
    else:
        macd = "DIF在DEA下方"
    true_ranges = []
    for index in range(max(1, len(bars) - 14), len(bars)):
        previous = float(bars[index - 1]["close"])
        high = float(bars[index]["high"])
        low = float(bars[index]["low"])
        true_ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    return {
        "ma5": ma5_value, "ma10": ma10_value, "ma20": ma20_value,
        "ma5_slope_pct": ((ma5_value / ma5[-2] - 1) * 100) if ma5_value and ma5[-2] else None,
        "dif": dif[-1], "dea": dea[-1], "histogram": histogram[-1],
        "histogram_previous": histogram[-2] if len(histogram) > 1 else histogram[-1],
        "trend": trend, "macd": macd,
        "atr14": fmean(true_ranges) if true_ranges else current * 0.02,
        "chart_bars": chart_bars[-45:],
    }


def benchmark_for(code: str) -> str:
    if code.startswith("300"):
        return "sz399006"
    if code.startswith("688"):
        return "sh000688"
    if code.startswith(("5", "6")):
        return "sh000001"
    return "sz399001"


def synthesize_bars(history: dict[str, Any], minute: dict[str, Any]) -> list[dict[str, Any]]:
    bars = parse_history(history, minute["trade_date"])
    if len(bars) < 34:
        raise RuntimeError("有效历史日线不足 35 条")
    bars.append({
        "date": f"{minute['trade_date'][:4]}-{minute['trade_date'][4:6]}-{minute['trade_date'][6:8]}",
        "open": minute["open"], "close": minute["price"], "high": minute["high"],
        "low": minute["low"], "volume": minute["volume"],
    })
    return bars


def percent_change(values: list[float], sessions: int) -> float:
    if len(values) <= sessions or values[-1 - sessions] == 0:
        return 0.0
    return (values[-1] / values[-1 - sessions] - 1) * 100


def analyze_security(
    code: str,
    minute: dict[str, Any],
    history: dict[str, Any],
    benchmark_returns: dict[str, dict[str, float]],
    cutoff_minutes: int,
) -> dict[str, Any]:
    bars = synthesize_bars(history, minute)
    indicators = calculate_indicators(bars)
    closes = [float(bar["close"]) for bar in bars]
    volumes = [float(bar["volume"]) for bar in bars]
    ret1, ret3, ret5 = (percent_change(closes, sessions) for sessions in (1, 3, 5))
    board_benchmark = benchmark_for(code)
    benchmark = benchmark_returns.get(board_benchmark, {})
    rs1 = ret1 - benchmark.get("ret1", 0.0)
    rs3 = ret3 - benchmark.get("ret3", 0.0)
    rs5 = ret5 - benchmark.get("ret5", 0.0)
    previous5_volume = fmean(volumes[-6:-1]) if len(volumes) >= 6 else 0
    progress = max(trading_progress(cutoff_minutes), 0.15)
    volume_pace = minute["volume"] / previous5_volume / progress if previous5_volume else None
    current = minute["price"]
    atr = float(indicators["atr14"])
    support_candidates = [
        min(float(bar["low"]) for bar in bars[-4:-1]),
        (indicators["ma10"] or current) * 0.995,
        (indicators["ma20"] or current) * 0.995,
    ]
    supports = [level for level in support_candidates if level < current * 0.996]
    invalidation = max(supports) if supports else current - max(atr * 0.8, current * 0.008)
    risk = current - invalidation
    previous20_high = max(float(bar["high"]) for bar in bars[-21:-1])
    reward_risk = (previous20_high - current) / risk if risk > 0 and previous20_high > current else 0.0
    distance_ma5 = ((current / indicators["ma5"] - 1) * 100) if indicators["ma5"] else None
    extension_threshold = 12.0 if code.startswith(("300", "688")) else 8.0
    if code.startswith("561"):
        extension_threshold = 4.0
    day_range = max(minute["high"] - minute["low"], 0.0001)
    upper_wick_ratio = (minute["high"] - max(minute["open"], minute["price"])) / day_range
    failures: list[str] = []
    warnings: list[str] = ["未自动核验行业板块联动"]
    name_upper = minute["name"].upper()
    risk_name = "ST" in name_upper or "退" in minute["name"]
    if risk_name:
        failures.append("风险警示或退市相关名称")
    minimum_amount = 20_000_000 * max(progress, 0.25)
    if minute["amount"] < minimum_amount:
        failures.append("成交额不足以支持次日快速退出")
    if indicators["trend"] == "空头排列":
        failures.append("日线均线空头排列")
    if indicators["dif"] < indicators["dea"] and current < (indicators["ma10"] or current):
        failures.append("MACD与价格结构同时偏弱")
    if minute["vwap"] and current < minute["vwap"] * 0.998:
        failures.append("价格在分时均价下方")
    if minute["tail_return_pct"] < -0.7 or minute["tail_low_delta_pct"] < -0.6:
        failures.append("最近30分钟结构走弱")
    if minute["tail_pullback_pct"] < -1.2:
        failures.append("尾盘冲高回落")
    if minute["spike_5m_pct"] > 2.0:
        failures.append("最近5分钟拉升过快")
    if upper_wick_ratio > 0.45 and (volume_pace or 0) > 1.2:
        failures.append("放量长上影风险")
    if percent_change(closes, 2) >= extension_threshold:
        failures.append("最近两日涨幅延伸过度")
    if distance_ma5 is not None and distance_ma5 > 6.0:
        failures.append("价格偏离MA5过大")
    limit_warning = 15.0 if code.startswith(("300", "688")) else 7.5
    if (minute["change_pct"] or 0) >= limit_warning:
        failures.append("接近涨停区域，不追高")
    if reward_risk < 2.0:
        failures.append("至前20日阻力位的空间不足2R")
    risk_pct = risk / current * 100 if current else 0
    if risk_pct < 0.6:
        warnings.append("结构失效距离较窄，容易受盘中噪声影响")
    if risk_pct > 4.5:
        failures.append("结构失效距离过大")
    if volume_pace is not None and volume_pace > 3.0:
        warnings.append("量能显著放大，需防衰竭")
    score = 50 + rs1 * 2.2 + rs3 * 1.1 + rs5 * 0.55
    score += 9 if indicators["trend"] == "多头排列" else (2 if indicators["trend"] == "震荡混合" else -12)
    score += 5 if indicators["dif"] > indicators["dea"] else -4
    score += 4 if indicators["histogram"] >= indicators["histogram_previous"] else -2
    score += 4 if minute["vwap"] and current >= minute["vwap"] else -4
    score += max(-5, min(5, minute["tail_return_pct"] * 4))
    score -= min(10, max(0, (distance_ma5 or 0) - 3) * 2)
    score -= min(28, len(failures) * 4)
    if risk_name:
        score = 0
    elif failures:
        score = min(score, max(25, 72 - len(failures) * 4))
    return {
        "code": code, "name": minute["name"], "symbol": minute["symbol"],
        "price": round(current, 3), "change_pct": round(minute["change_pct"] or 0, 2),
        "vwap": round(minute["vwap"], 3) if minute["vwap"] else None,
        "amount": round(minute["amount"], 2), "last_time": minute["last_time"],
        "ret1": round(ret1, 2), "ret3": round(ret3, 2), "ret5": round(ret5, 2),
        "rs1": round(rs1, 2), "rs3": round(rs3, 2), "rs5": round(rs5, 2),
        "volume_pace": round(volume_pace, 2) if volume_pace is not None else None,
        "trend": indicators["trend"], "macd": indicators["macd"],
        "ma5": round(indicators["ma5"], 3) if indicators["ma5"] else None,
        "ma10": round(indicators["ma10"], 3) if indicators["ma10"] else None,
        "ma20": round(indicators["ma20"], 3) if indicators["ma20"] else None,
        "dif": round(indicators["dif"], 4), "dea": round(indicators["dea"], 4),
        "histogram": round(indicators["histogram"], 4),
        "tail_return_pct": round(minute["tail_return_pct"], 2),
        "tail_pullback_pct": round(minute["tail_pullback_pct"], 2),
        "invalidation": round(invalidation, 3), "resistance": round(previous20_high, 3),
        "reward_risk": round(reward_risk, 2), "risk_pct": round(risk_pct, 2),
        "score": round(max(0, min(100, score)), 1),
        "failures": list(dict.fromkeys(failures)), "warnings": warnings,
        "chart": indicators["chart_bars"],
        "minute_records": minute["records"],
    }


def determine_phase(now: datetime, trade_date: str) -> tuple[str, str]:
    today = now.strftime("%Y%m%d")
    if now.weekday() >= 5 or trade_date != today:
        return "closed", "休市或行情非当日"
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 15 <= minutes < 10 * 60:
        return "exit", "T+1 退出窗口"
    if minutes < 9 * 60 + 15 or minutes > 15 * 60:
        return "closed", "非交易时段"
    if 14 * 60 + 30 <= minutes <= 15 * 60:
        return "decision", "14:30 入场决策窗口"
    return "observe", "盘中观察，等待14:30确认"


def market_state(index_rows: list[dict[str, Any]]) -> dict[str, Any]:
    changes = [row["change_pct"] for row in index_rows if row.get("change_pct") is not None]
    if not changes:
        return {"state": "red", "label": "红灯", "summary": "指数行情缺失，禁止建立新仓"}
    weak_count = sum(change <= -1.5 for change in changes)
    growth_crash = any(row["symbol"] in ("sz399006", "sh000688") and row["change_pct"] <= -3 for row in index_rows)
    average = fmean(changes)
    negative_tail = sum(row.get("tail_return_pct", 0) < -0.35 for row in index_rows)
    if weak_count >= 2 or growth_crash or average <= -2:
        state, label = "red", "红灯"
        summary = "主要宽基同步走弱，系统性风险不允许新开仓"
    elif average < 0 or max(changes) - min(changes) > 2.2 or negative_tail >= 2:
        state, label = "yellow", "黄灯"
        summary = "指数分化或尾盘结构不稳，只允许小仓位条件式方案"
    else:
        state, label = "green", "绿灯"
        summary = "宽基结构相对稳定，仍需通过全部个股硬门槛"
    return {"state": state, "label": label, "summary": summary, "average_change_pct": round(average, 2)}


def run_analysis(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(SHANGHAI)
    current_minutes = now.hour * 60 + now.minute
    cutoff_minutes = min(max(current_minutes, 9 * 60 + 30), 14 * 60 + 30)
    symbols = [symbol_for(code) for code in WATCHLIST]
    all_symbols = symbols + list(BENCHMARKS)
    minute_raw: dict[str, dict[str, Any]] = {}
    history_raw: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        jobs = {}
        for symbol in all_symbols:
            jobs[executor.submit(fetch_minute, symbol)] = ("minute", symbol)
            jobs[executor.submit(fetch_history, symbol)] = ("history", symbol)
        for future in as_completed(jobs):
            kind, symbol = jobs[future]
            try:
                result = future.result()
                (minute_raw if kind == "minute" else history_raw)[symbol] = result
            except Exception as error:
                errors.append(f"{symbol} {kind}: {error}")
    trade_dates = [item.get("trade_date", "") for item in minute_raw.values() if item.get("trade_date")]
    if not trade_dates:
        raise RuntimeError("实时分时行情全部获取失败")
    trade_date = max(trade_dates)
    phase, phase_label = determine_phase(now, trade_date)
    parsed_minutes: dict[str, dict[str, Any]] = {}
    for symbol, raw in minute_raw.items():
        if raw.get("trade_date") != trade_date:
            continue
        try:
            parsed_minutes[symbol] = parse_minute(raw, cutoff_minutes)
        except Exception as error:
            errors.append(f"{symbol} minute parse: {error}")
    index_rows = []
    benchmark_returns: dict[str, dict[str, float]] = {}
    for symbol, name in BENCHMARKS.items():
        minute = parsed_minutes.get(symbol)
        history = history_raw.get(symbol)
        if not minute or not history:
            continue
        try:
            bars = synthesize_bars(history, minute)
            closes = [float(bar["close"]) for bar in bars]
            row = {
                "symbol": symbol, "name": name, "price": round(minute["price"], 2),
                "change_pct": round(minute["change_pct"] or 0, 2),
                "tail_return_pct": round(minute["tail_return_pct"], 2),
            }
            index_rows.append(row)
            benchmark_returns[symbol] = {
                "ret1": percent_change(closes, 1),
                "ret3": percent_change(closes, 3),
                "ret5": percent_change(closes, 5),
            }
        except Exception as error:
            errors.append(f"{symbol} index analysis: {error}")
    market = market_state(index_rows)
    securities = []
    for code in WATCHLIST:
        symbol = symbol_for(code)
        minute = parsed_minutes.get(symbol)
        history = history_raw.get(symbol)
        if not minute or not history:
            continue
        try:
            securities.append(analyze_security(code, minute, history, benchmark_returns, cutoff_minutes))
        except Exception as error:
            errors.append(f"{symbol} security analysis: {error}")
    securities.sort(key=lambda item: item["score"], reverse=True)
    announcement_targets = securities[:8]
    with ThreadPoolExecutor(max_workers=8) as executor:
        jobs = {executor.submit(fetch_announcements, item["code"], trade_date): item for item in announcement_targets}
        for future in as_completed(jobs):
            item = jobs[future]
            announcement = future.result()
            item["announcements"] = announcement
            if not announcement["verified"]:
                item["failures"].append("近期公告未完成自动核验")
            elif announcement["risk_titles"]:
                item["failures"].append("近期公告命中风险关键词")
                item["warnings"].extend(announcement["risk_titles"][:2])
    for item in securities[8:]:
        item["announcements"] = {"verified": False, "recent": [], "risk_titles": []}
        item["failures"].append("未进入公告核验名单")
    for item in securities:
        if item["failures"] and item["score"] > 68:
            item["score"] = 68
        item["eligible"] = not item["failures"]
    securities.sort(key=lambda item: (item["eligible"], item["score"]), reverse=True)
    strict_candidates = [item for item in securities if not item["failures"]]
    actionable = phase == "decision" and market["state"] != "red"
    recommendations = strict_candidates[: (1 if market["state"] == "yellow" else 2)] if actionable else []
    if recommendations:
        decision = "buy"
        headline = f"{len(recommendations)} 个条件式买点"
        summary = "仅在页面列出的14:30触发条件仍成立时执行；任一取消条件出现则保持现金。"
        total_limit = 30 if market["state"] == "yellow" else 60
        per_position = total_limit / len(recommendations)
    elif phase == "observe":
        decision, headline = "watch", "观察，不在当前时点买入"
        summary = "排名仅用于缩小观察范围，等待14:30分时结构和公告门槛确认。"
        per_position = 0
    elif phase == "exit":
        decision, headline = "cash", "新仓 0%，昨日仓位最迟10:00退出"
        summary = "当前处于T+1退出窗口，不为隔夜策略建立新仓。"
        per_position = 0
    elif phase == "decision" and market["state"] == "red":
        decision, headline = "cash", "空仓，仓位 0%"
        summary = "市场红灯覆盖个股排名，今天不建立隔夜仓位。"
        per_position = 0
    elif phase == "decision":
        decision, headline = "cash", "空仓，仓位 0%"
        summary = "没有股票同时通过市场、技术、分时、2R与公告硬门槛。"
        per_position = 0
    else:
        decision, headline = "cash", "当前不可新开仓"
        summary = "已收盘、休市或行情不是当日数据；仅保留下一交易日观察名单。"
        per_position = 0
    recommendation_codes = {item["code"] for item in recommendations}
    # `candidates` is reserved for executable plans. Observation rankings stay in
    # `universe`, so a cash decision cannot be mistaken for a trade suggestion.
    display_candidates = recommendations
    for item in display_candidates:
        is_buy = item["code"] in recommendation_codes
        position_pct = per_position if is_buy else 0
        entry = item["price"]
        shares = math.floor((CAPITAL * position_pct / 100) / entry / 100) * 100 if entry else 0
        item["action"] = "条件式买入" if is_buy else "观察，不买"
        item["position_pct"] = round(position_pct, 1)
        item["shares"] = shares
        item["capital_used"] = round(shares * entry, 2)
        item["planned_loss"] = round(shares * max(0, entry - item["invalidation"]), 2)
        item["entry_range"] = f"{entry * 0.997:.2f}-{entry * 1.003:.2f}" if is_buy else "未触发"
        item["no_chase"] = round(entry * 1.008, 3)
        item["exit_plan"] = [
            {"time": "09:25", "action": "按集合竞价判断高开、平开或低开情景"},
            {"time": "09:30-09:45", "action": "弱于指数或无承接优先退出；高开按计划兑现"},
            {"time": "10:00", "action": "清仓全部可成交数量"},
        ]
        item.pop("minute_records", None)
    universe = []
    for item in securities:
        universe.append({
            key: item.get(key) for key in (
                "code", "name", "price", "change_pct", "score", "trend", "macd",
                "reward_risk", "last_time", "failures", "eligible",
            )
        })
    last_times = [item["last_time"] for item in parsed_minutes.values() if item.get("last_time")]
    data_as_of = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} {minutes_to_hhmm(cutoff_minutes)}"
    response = {
        "generated_at": now.isoformat(timespec="seconds"),
        "data_as_of": data_as_of,
        "trade_date": trade_date,
        "phase": phase, "phase_label": phase_label,
        "decision": decision, "headline": headline, "summary": summary,
        "market": market, "indices": index_rows,
        "capital": CAPITAL,
        "position_pct": round(sum(item["position_pct"] for item in display_candidates), 1),
        "candidates": display_candidates,
        "universe": universe,
        "errors": errors[:12],
        "sources": [
            {"name": "腾讯前复权日线", "url": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", "as_of": data_as_of},
            {"name": "腾讯当日分时", "url": "https://web.ifzq.gtimg.cn/appstock/app/minute/query", "as_of": data_as_of},
            {"name": "东方财富公告", "url": "https://data.eastmoney.com/notices/", "as_of": now.strftime("%Y-%m-%d")},
        ],
        "disclaimer": "自动筛选仅供研究参考，不构成个性化投资建议。公告关键词筛查不等于完整公告研读；隔夜跳空、涨跌停和流动性可能使计划价格无法成交。",
        "methodology": "14:30隔夜策略 v1",
    }
    if phase == "decision" and trade_date == now.strftime("%Y%m%d"):
        persist_decision(response, parsed_minutes)
    return response


def persist_decision(response: dict[str, Any], minute_rows: dict[str, dict[str, Any]]) -> None:
    decision_dir = DATA_DIR / "decisions"
    decision_dir.mkdir(parents=True, exist_ok=True)
    destination = decision_dir / f"{response['trade_date']}-1430.json"
    if destination.exists():
        return
    audit = {
        "analysis": response,
        "minute_records": {
            symbol: item["records"] for symbol, item in minute_rows.items() if symbol not in BENCHMARKS
        },
    }
    destination.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")


def get_analysis(force: bool = False) -> dict[str, Any]:
    now_monotonic = time.monotonic()
    with _cache_lock:
        if not force and _cache["data"] is not None and now_monotonic < _cache["expires"]:
            return _cache["data"]
        data = run_analysis()
        _cache["data"] = data
        _cache["expires"] = time.monotonic() + CACHE_SECONDS
        return data


def push_once(webhook: str, now: datetime | None = None) -> dict[str, Any]:
    """Run the 14:30 screen once for scheduled CI and push only a valid decision."""
    now = now or datetime.now(SHANGHAI)
    webhook = validate_wecom_webhook(webhook)
    analysis = get_analysis(force=True)
    today = now.strftime("%Y%m%d")
    if analysis.get("trade_date") != today:
        return {
            "sent": False,
            "reason": "行情不是当日数据，可能休市",
            "trade_date": analysis.get("trade_date", ""),
        }
    if analysis.get("phase") != "decision":
        return {
            "sent": False,
            "reason": "当前不在14:30-15:00决策窗口",
            "phase": analysis.get("phase", ""),
        }
    send_wecom_push(analysis, webhook=webhook)
    return {
        "sent": True,
        "trade_date": analysis.get("trade_date", ""),
        "headline": analysis.get("headline", ""),
        "position_pct": analysis.get("position_pct", 0),
    }


def push_scheduler_loop() -> None:
    while not _scheduler_stop.wait(30):
        now = datetime.now(SHANGHAI)
        minutes = now.hour * 60 + now.minute
        if now.weekday() >= 5 or not (14 * 60 + 31 <= minutes <= 14 * 60 + 50):
            continue
        config = load_push_config(include_secret=True)
        today = now.strftime("%Y%m%d")
        if not config.get("enabled") or not config.get("webhook") or config.get("last_sent") == today:
            continue
        try:
            analysis = get_analysis(force=True)
            if analysis.get("trade_date") != today or analysis.get("phase") != "decision":
                continue
            send_wecom_push(analysis)
            save_push_config({"last_sent": today, "last_result": f"{now:%Y-%m-%d %H:%M} 推送成功"})
        except Exception as error:
            save_push_config({"last_result": f"{now:%Y-%m-%d %H:%M} 推送失败：{error}"})


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json({"status": "ok", "time": datetime.now(SHANGHAI).isoformat(timespec="seconds")})
            return
        if parsed.path == "/api/push/status":
            self.send_json(load_push_config())
            return
        if parsed.path == "/api/analysis":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                self.send_json(get_analysis(force=query.get("refresh") == ["1"]))
            except Exception as error:
                self.send_json({"error": str(error), "generated_at": datetime.now(SHANGHAI).isoformat()}, status=503)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/api/push/config", "/api/push/test"):
            self.send_json({"error": "not found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16_384:
                raise ValueError("请求内容长度无效")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求必须是 JSON 对象")
            if parsed.path == "/api/push/config":
                self.send_json(save_push_config(payload))
            else:
                analysis = get_analysis(force=False)
                send_wecom_push(analysis, test=True)
                now = datetime.now(SHANGHAI)
                self.send_json(save_push_config({"last_result": f"{now:%Y-%m-%d %H:%M} 测试成功"}))
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, status=400)
        except Exception as error:
            self.send_json({"error": str(error)}, status=502)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {message % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--push-once", action="store_true", help="运行一次14:30筛选并推送，供定时任务使用")
    args = parser.parse_args()
    if args.push_once:
        webhook = os.environ.get("WECOM_WEBHOOK", "").strip()
        if not webhook:
            print(json.dumps({"sent": False, "error": "缺少 WECOM_WEBHOOK"}, ensure_ascii=False))
            return 2
        try:
            result = push_once(webhook)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except Exception as error:
            print(json.dumps({"sent": False, "error": str(error)}, ensure_ascii=False))
            return 1
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    scheduler = threading.Thread(target=push_scheduler_loop, name="wecom-push-scheduler", daemon=True)
    scheduler.start()
    print(f"A-share dashboard: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _scheduler_stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

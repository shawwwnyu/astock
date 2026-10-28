#!/usr/bin/env python3
"""A-share short-term dashboard and scheduled Feishu push assistant."""

from __future__ import annotations

import argparse
import base64
import copy
import fcntl
import hmac
import json
import hashlib
import math
import os
import secrets
import re
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo

from weekly_review import (
    DEFAULT_PARAMETERS, STRATEGY_VERSION, archive_formal_event, archive_path_for,
    build_formal_event, load_config as load_weekly_config, weekly_review,
)


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
SHANGHAI = ZoneInfo("Asia/Shanghai")
CAPITAL = 50_000
CACHE_SECONDS = 55
MAX_HOLDING_SESSIONS = 3
UNIVERSE_VERSION = "2026-08-25.1"
SCAN_WINDOWS = ((9 * 60 + 35, 11 * 60 + 30), (13 * 60, 15 * 60))
SCAN_REASON_CODES = {"buy": "strict-entry-v1", "sell": "holding-exit-v1"}
PENDING_SCHEMA = "astock.pending-report/v1"


class CorruptLocalData(RuntimeError):
    pass

WATCHLIST = [
    "300740", "000909", "002080", "300007", "002318", "300398", "300847",
    "300790", "001232",
    "603023", "002938", "603322", "600707", "300413", "605488", "300757",
    "000901", "600258", "300545", "300220", "003016", "600426", "603131",
    "002346", "000555", "002230", "601138", "603181", "688037", "600809",
    "300722", "002714", "300493", "561360", "600779", "603369", "601288",
    "601669", "000625", "600104", "600309", "000596", "601633", "601668",
    "300433",
]

BENCHMARKS = {
    "sh000300": "沪深300",
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
_state_lock = threading.Lock()
_scheduler_stop = threading.Event()
PUSH_CONFIG_PATH = DATA_DIR / "feishu-push.json"
TRADING_STATE_PATH = DATA_DIR / "trading-state.json"

# 直接推送目标：邢晓渝个人飞书私信
DEFAULT_RECEIVE_EMAIL = "xingxiaoyu@pjlab.org.cn"
FEISHU_OPEN_API_BASE = "https://open.feishu.cn/open-apis"
_token_cache: dict[str, Any] = {"token": "", "expires": 0.0}

SCHEDULED_STAGES = {
    "morning-summary": {
        "cutoff": 10 * 60,
        "push_time": 10 * 60,
        "label": "10:00 早盘概况",
        "entry": False,
    },
    "morning-decision": {
        "cutoff": 10 * 60 + 30,
        "push_time": 10 * 60 + 30,
        "label": "10:30 持仓与交易决策",
        "entry": True,
    },
    "tail-decision": {
        "cutoff": 14 * 60 + 30,
        "push_time": 14 * 60 + 30,
        "label": "14:30 尾盘交易决策",
        "entry": True,
    },
    "close-summary": {
        "cutoff": 15 * 60,
        "push_time": 15 * 60 + 30,
        "label": "15:30 收盘总结",
        "entry": False,
    },
}


def symbol_for(code: str) -> str:
    if code.startswith(("5", "6")):
        return f"sh{code}"
    if code.startswith(("4", "8", "9")):
        return f"bj{code}"
    return f"sz{code}"


def lot_size_for(code: str) -> int:
    return 200 if code.startswith("688") else 100


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


def validate_feishu_webhook(webhook: str) -> str:
    webhook = webhook.strip()
    parsed = urllib.parse.urlparse(webhook)
    token = parsed.path.rsplit("/", 1)[-1]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "open.feishu.cn"
        or parsed.path.count("/") != 5
        or not parsed.path.startswith("/open-apis/bot/v2/hook/")
        or len(token) < 20
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("必须使用飞书群自定义机器人生成的官方 Webhook 地址")
    return webhook


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    """获取飞书自建应用 tenant_access_token，带进程内缓存。"""
    app_id = app_id.strip()
    app_secret = app_secret.strip()
    if not app_id or not app_secret:
        raise ValueError("缺少飞书应用 app_id 或 app_secret")
    now = time.time()
    cached = _token_cache.get("token", "")
    if cached and now < _token_cache.get("expires", 0.0) - 120:
        return cached
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    request = urllib.request.Request(
        f"{FEISHU_OPEN_API_BASE}/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code") != 0:
        raise RuntimeError(f"获取飞书 tenant_access_token 失败：{result.get('msg', 'unknown')}")
    token = str(result.get("tenant_access_token", ""))
    if not token:
        raise RuntimeError("飞书返回的 tenant_access_token 为空")
    _token_cache["token"] = token
    _token_cache["expires"] = now + int(result.get("expire", 7200))
    return token


def send_feishu_direct(
    message_text: str,
    app_id: str,
    app_secret: str,
    receive_id: str = DEFAULT_RECEIVE_EMAIL,
    receive_id_type: str = "email",
) -> dict[str, Any]:
    """通过飞书自建应用直接给个人发私信（默认发给邢晓渝）。"""
    token = get_tenant_access_token(app_id, app_secret)
    receive_id = receive_id.strip() or DEFAULT_RECEIVE_EMAIL
    content = json.dumps({"text": message_text}, ensure_ascii=False)
    body = json.dumps(
        {"receive_id": receive_id, "msg_type": "text", "content": content},
        ensure_ascii=False,
    ).encode("utf-8")
    if len(body) > 50_000:
        raise ValueError("飞书私信消息超过 50 KB 限制")
    request = urllib.request.Request(
        f"{FEISHU_OPEN_API_BASE}/im/v1/messages?receive_id_type={receive_id_type}",
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code") != 0:
        raise RuntimeError(f"飞书私信发送失败：{result.get('msg', 'unknown')}")
    return result


def load_push_config(include_secret: bool = False) -> dict[str, Any]:
    with _push_lock:
        if not PUSH_CONFIG_PATH.exists():
            config = {
                "enabled": False, "webhook": "", "signing_secret": "",
                "app_id": "", "app_secret": "", "receive_id": "",
                "last_sent": "", "last_result": "尚未配置",
            }
        else:
            try:
                config = json.loads(PUSH_CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                config = {
                    "enabled": False, "webhook": "", "signing_secret": "",
                    "app_id": "", "app_secret": "", "receive_id": "",
                    "last_sent": "", "last_result": "配置文件损坏",
                }
    has_app = bool(config.get("app_id") and config.get("app_secret"))
    status = {
        "configured": bool(config.get("webhook")) or has_app,
        "signed": bool(config.get("signing_secret")),
        "enabled": bool(config.get("enabled")),
        "last_sent": str(config.get("last_sent", "")),
        "last_result": str(config.get("last_result", "")),
        "schedule": "交易日 10:00 / 10:30 / 14:30 / 15:30",
        "channel": "飞书私信（邢晓渝）" if has_app else "飞书群自定义机器人",
        "has_app": has_app,
    }
    if include_secret:
        status["webhook"] = str(config.get("webhook", ""))
        status["signing_secret"] = str(config.get("signing_secret", ""))
        status["app_id"] = str(config.get("app_id", ""))
        status["app_secret"] = str(config.get("app_secret", ""))
        status["receive_id"] = str(config.get("receive_id", ""))
    return status


def save_push_config(update: dict[str, Any]) -> dict[str, Any]:
    current = load_push_config(include_secret=True)
    webhook = str(update.get("webhook", "")).strip()
    if webhook:
        current["webhook"] = validate_feishu_webhook(webhook)
    if "signing_secret" in update and str(update["signing_secret"]).strip():
        current["signing_secret"] = str(update["signing_secret"]).strip()
    for key in ("app_id", "app_secret", "receive_id"):
        if key in update:
            current[key] = str(update[key]).strip()
    if "enabled" in update:
        current["enabled"] = bool(update["enabled"])
    has_app = bool(current.get("app_id") and current.get("app_secret"))
    if current.get("enabled") and not current.get("webhook") and not has_app:
        raise ValueError("启用自动推送前必须配置飞书应用凭据或 Webhook")
    for key in ("last_sent", "last_result"):
        if key in update:
            current[key] = str(update[key])
    stored = {
        "webhook": current.get("webhook", ""),
        "enabled": bool(current.get("enabled")),
        "last_sent": current.get("last_sent", ""),
        "last_result": current.get("last_result", ""),
        "signing_secret": current.get("signing_secret", ""),
        "app_id": current.get("app_id", ""),
        "app_secret": current.get("app_secret", ""),
        "receive_id": current.get("receive_id", ""),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = PUSH_CONFIG_PATH.with_suffix(".tmp")
    with _push_lock:
        temporary.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(PUSH_CONFIG_PATH)
        os.chmod(PUSH_CONFIG_PATH, 0o600)
    return load_push_config()


def empty_trading_state() -> dict[str, Any]:
    return {
        "version": 4,
        "updated_at": "",
        "holdings": [],
        "history": [],
        "processed_events": [],
        "committed_deliveries": [],
        "committed_delivery_tokens": {},
        "pending_report": None,
        "signal_trade_date": "",
        "signal_states": {},
    }


@contextmanager
def trading_state_transaction(path: Path | None = None):
    path = path or TRADING_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as lock_file:
            descriptor = -1
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield path
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_trading_state(path: Path | None = None) -> dict[str, Any]:
    path = path or TRADING_STATE_PATH
    with _state_lock:
        if not path.exists():
            return empty_trading_state()
        try:
            os.chmod(path, 0o600)
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"交易状态文件无法读取：{error}") from error
    if not isinstance(state, dict):
        raise RuntimeError("交易状态文件格式无效")
    normalized = empty_trading_state()
    normalized.update(state)
    for key in ("holdings", "history", "processed_events", "committed_deliveries"):
        if not isinstance(normalized.get(key), list):
            raise RuntimeError(f"交易状态字段 {key} 格式无效")
    committed_tokens = normalized.get("committed_delivery_tokens")
    if not isinstance(committed_tokens, dict):
        raise RuntimeError("交易状态字段 committed_delivery_tokens 格式无效")
    normalized["committed_delivery_tokens"] = {
        str(token): str(event_id)
        for token, event_id in committed_tokens.items()
        if isinstance(token, str) and isinstance(event_id, str)
    }
    for delivery in normalized["committed_deliveries"]:
        if not isinstance(delivery, dict):
            continue
        token = delivery.get("commit_token")
        event_id = delivery.get("event_id")
        if isinstance(token, str) and isinstance(event_id, str):
            normalized["committed_delivery_tokens"].setdefault(token, event_id)
    if normalized.get("pending_report") is not None and not isinstance(normalized["pending_report"], dict):
        raise CorruptLocalData("交易状态字段 pending_report 格式无效")
    if not isinstance(normalized.get("signal_states"), dict):
        raise RuntimeError("交易状态字段 signal_states 格式无效")
    normalized["signal_states"] = {
        str(code): value for code, value in normalized["signal_states"].items()
        if isinstance(code, str) and isinstance(value, dict)
        and value.get("side") in {"buy", "sell", "none"}
    }
    normalized["version"] = 4
    normalized["holdings"] = [
        item for item in normalized["holdings"]
        if isinstance(item, dict) and item.get("status", "open") == "open"
    ]
    return normalized


def save_trading_state(state: dict[str, Any], path: Path | None = None) -> None:
    path = path or TRADING_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with _state_lock:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                descriptor = -1
                json.dump(state, temporary_file, ensure_ascii=False, indent=2)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        temporary.replace(path)
        parent_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_descriptor)
        except OSError as error:
            raise RuntimeError(f"交易状态目录无法同步：{error}") from error
        finally:
            os.close(parent_descriptor)


def percent_text(value: Any) -> str:
    number = as_number(value)
    if number is None:
        return "无法计算"
    return f"{number:+.2f}%"


def format_push_message(analysis: dict[str, Any], test: bool = False) -> str:
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
        "持有计划：下一交易日10:30复核；无卖点可继续持有，第3个可卖交易日必须退出。",
        "仅供研究参考，不构成个性化投资建议。",
    ])
    return "\n".join(lines)[:3800]


def format_scheduled_message(report: dict[str, Any], test: bool = False) -> str:
    analysis = report["analysis"]
    prefix = "测试 · " if test else ""
    lines = [
        f"**{prefix}{analysis.get('trade_date', '')} {report.get('label', 'A股短线助手')}**",
        f">市场：{analysis.get('market', {}).get('label', '未核验')} · {analysis.get('market', {}).get('summary', '')}",
        f">数据：{analysis.get('data_as_of', '未核验')}",
        "",
    ]
    stage = report.get("stage")
    if stage in ("morning-summary", "close-summary"):
        lines.append("**主要指数**")
        for index in analysis.get("indices", []):
            lines.append(
                f">{index.get('name', '')} {index.get('price', '未核验')} · {percent_text(index.get('change_pct'))}"
            )
        breadth = analysis.get("breadth", {})
        lines.extend([
            "",
            f"自选池：上涨 {breadth.get('up', 0)} · 下跌 {breadth.get('down', 0)} · 平盘 {breadth.get('flat', 0)}",
            f"平均涨跌：{percent_text(breadth.get('average_change_pct'))}",
        ])
        if stage == "morning-summary":
            lines.append("10:00 仅报告早盘情况，10:30 再给持仓和交易决定。")
        else:
            lines.append("收盘总结使用截至15:00的行情，本条不产生交易操作。")
            for holding in report.get("manual_holding_reviews", []):
                current_price = holding.get("current_price")
                lines.extend([
                    "",
                    f"**持仓监控 · {holding.get('name', '')} {holding.get('code', '')}**",
                    f">买入日：{holding.get('buy_date', '')} · 成本：{holding.get('buy_price', 0):.3f} · 数量：{holding.get('shares', 0)} 股",
                ])
                if current_price is None:
                    lines.append(">收盘行情缺失，浮动盈亏无法计算。")
                else:
                    lines.extend([
                        f">收盘价：{current_price:.3f} · 浮动盈亏：{holding['floating_pnl']:+.2f} 元（{percent_text(holding['return_pct'])}）",
                    ])
            if report.get("manual_holding_reviews"):
                lines.append(">此处仅汇报价格与浮动盈亏，不生成该持仓的买卖指令。")
    else:
        reviews = report.get("holding_reviews", [])
        buys = report.get("buys", [])
        for item in reviews:
            holding = item["holding"]
            current_price = item.get("current_price")
            if item["decision"] == "sell":
                lines.extend([
                    f"**卖出 · {holding.get('name', '')} {holding.get('code', '')}**",
                    f">动作：卖出全部 {holding.get('shares', 0)} 股",
                    f">成本：{holding.get('buy_price', '未核验')} · 参考卖价：{current_price if current_price is not None else '未核验'}",
                    f">本次收益率：{percent_text(item.get('return_pct'))}（按参考价估算，未计费用与滑点）",
                    f">持有：第 {item.get('holding_session', 0)} / {MAX_HOLDING_SESSIONS} 个可卖交易日",
                    f">原因：{'；'.join(item.get('reasons', []))}",
                    "",
                ])
            else:
                lines.extend([
                    f"**持有 · {holding.get('name', '')} {holding.get('code', '')}**",
                    f">动作：继续持有，本次不卖",
                    f">成本：{holding.get('buy_price', '未核验')} · 现价：{current_price if current_price is not None else '未核验'}",
                    f">当前浮动收益率：{percent_text(item.get('return_pct'))}（未计费用与滑点）",
                    f">持有：第 {item.get('holding_session', 0)} / {MAX_HOLDING_SESSIONS} 个可卖交易日",
                    f">依据：{'；'.join(item.get('reasons', []))}",
                    "",
                ])
        for stock in buys:
            lines.extend([
                f"**买入 · {stock.get('name', '')} {stock.get('code', '')}**",
                f">参考买价：{stock.get('price', '未核验')} · 区间：{stock.get('entry_range', '未核验')}",
                f">数量：{stock.get('shares', 0)} 股 · 仓位：{stock.get('position_pct', 0)}% · 资金：{stock.get('capital_used', 0)} 元",
                f">失效参考：{stock.get('invalidation', '未核验')} · 目标参考：{stock.get('resistance', '未核验')}",
                ">安排：下一交易日10:30复核；无卖点继续持有，第3个可卖交易日强制退出。",
                "",
            ])
        if not reviews and not buys:
            lines.append("**本次没有买点和卖点，不操作。**")
        elif reviews and not any(item["decision"] == "sell" for item in reviews) and not buys:
            lines.append("本次没有卖点，也不新增买入。")
        notes = report.get("notes", [])
        for note in notes:
            lines.append(f">{note}")
        lines.extend([
            "",
            "飞书自定义机器人无法接收回复。本消息发送成功后，系统默认你按上述明确买入/卖出指令执行；持有、观察和空仓不产生交易。",
        ])
    lines.extend(["", "仅供研究参考，不构成个性化投资建议。"])
    return "\n".join(lines)[:3800]


def format_lightclaw_message(report: dict[str, Any], test: bool = False) -> str:
    message = format_scheduled_message(report, test=False)
    if test:
        message = message.replace("**", "**LightClaw 测试 · ", 1)
    return message.replace(
        "飞书自定义机器人无法接收回复。本消息发送成功后，系统默认你按上述明确买入/卖出指令执行；持有、观察和空仓不产生交易。",
        "本消息经当前飞书会话确认送达后，系统才会记录上述明确买入/卖出指令；持有、观察和空仓不产生交易。",
    )


def send_feishu_push(
    payload: dict[str, Any], test: bool = False, webhook: str | None = None,
    signing_secret: str | None = None,
    app_id: str | None = None, app_secret: str | None = None,
    receive_id: str | None = None,
) -> dict[str, Any]:
    config = load_push_config(include_secret=True)
    # 优先走自建应用私信通道（直接发给邢晓渝个人）
    app_id = (app_id or config.get("app_id") or os.environ.get("FEISHU_APP_ID", "")).strip()
    app_secret = (app_secret or config.get("app_secret") or os.environ.get("FEISHU_APP_SECRET", "")).strip()
    receive_id = (receive_id or config.get("receive_id") or os.environ.get("FEISHU_RECEIVE_ID", "")).strip()
    markdown_message = (
        format_scheduled_message(payload, test=test)
        if payload.get("report_type") == "scheduled"
        else format_push_message(payload, test=test)
    )
    message = re.sub(r"^>\s?", "", markdown_message, flags=re.MULTILINE)
    message = re.sub(r"\*\*(.*?)\*\*", r"\1", message)
    message = re.sub(r"`([^`]*)`", r"\1", message)
    if app_id and app_secret:
        return send_feishu_direct(
            message, app_id=app_id, app_secret=app_secret,
            receive_id=receive_id or DEFAULT_RECEIVE_EMAIL,
        )
    # 兼容旧通道：群自定义机器人 Webhook
    webhook = webhook or config.get("webhook", "")
    if not webhook:
        raise RuntimeError("尚未配置飞书应用凭据（FEISHU_APP_ID/APP_SECRET）或群 Webhook")
    webhook = validate_feishu_webhook(webhook)
    body_data: dict[str, Any] = {"msg_type": "text", "content": {"text": message}}
    signing_secret = signing_secret if signing_secret is not None else config.get("signing_secret", "")
    if signing_secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{signing_secret}"
        signature = base64.b64encode(
            hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest(),
        ).decode("utf-8")
        body_data.update({"timestamp": timestamp, "sign": signature})
    body = json.dumps(body_data, ensure_ascii=False).encode("utf-8")
    if len(body) > 20_000:
        raise ValueError("飞书机器人消息超过 20 KB 限制")
    request = urllib.request.Request(
        webhook,
        data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code") != 0:
        raise RuntimeError(f"飞书机器人返回错误：{result.get('msg', 'unknown')}")
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


def fetch_history_sina(symbol: str) -> dict[str, Any]:
    """新浪财经日线接口 fallback，返回与腾讯相同的二维数组格式。"""
    url = (
        f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={urllib.parse.quote(symbol)}"
        f"&scale=240&ma=no&datalen=70"
    )
    payload = fetch_json(url)
    if not isinstance(payload, list) or len(payload) < 35:
        raise RuntimeError(f"insufficient sina history for {symbol}")
    raw_rows = []
    for item in payload:
        raw_rows.append([
            item.get("day", ""),
            item.get("open", ""),
            item.get("close", ""),
            item.get("high", ""),
            item.get("low", ""),
            item.get("volume", ""),
        ])
    return {"symbol": symbol, "raw_rows": raw_rows}


def fetch_history(symbol: str) -> dict[str, Any]:
    param = urllib.parse.quote(f"{symbol},day,,,70,qfq")
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={param}"
    try:
        payload = fetch_json(url)
        node = payload.get("data", {}).get(symbol)
        if payload.get("code") != 0 or not isinstance(node, dict):
            raise RuntimeError(f"invalid history response for {symbol}")
        raw_rows = node.get("qfqday") or node.get("day") or []
        if not isinstance(raw_rows, list) or len(raw_rows) < 35:
            raise RuntimeError(f"insufficient history for {symbol}")
        return {"symbol": symbol, "raw_rows": raw_rows}
    except Exception:
        # 腾讯接口被 WAF 拦截或失败时，fallback 到新浪
        return fetch_history_sina(symbol)


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
        failures.append("最近30分钟冲高回落")
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
    if 9 * 60 + 15 <= minutes < 10 * 60 + 30:
        return "morning-summary", "早盘观察，等待10:30决策"
    if 10 * 60 + 30 <= minutes <= 11 * 60:
        return "morning-decision", "10:30 持仓与交易决策窗口"
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
        summary = "指数分化或最近30分钟结构不稳，只允许小仓位条件式方案"
    else:
        state, label = "green", "绿灯"
        summary = "宽基结构相对稳定，仍需通过全部个股硬门槛"
    return {"state": state, "label": label, "summary": summary, "average_change_pct": round(average, 2)}


def run_analysis(
    now: datetime | None = None,
    cutoff_minutes: int | None = None,
    stage: str | None = None,
    persist: bool = True,
    persistence_data: dict[str, Any] | None = None,
    mode: str = "scheduled",
) -> dict[str, Any]:
    now = now or datetime.now(SHANGHAI)
    if stage is not None and stage not in SCHEDULED_STAGES:
        raise ValueError(f"未知定时阶段：{stage}")
    if mode not in {"scheduled", "intraday-scan"}:
        raise ValueError(f"未知分析模式：{mode}")
    if mode == "intraday-scan" and stage is not None:
        raise ValueError("盘中扫描不得伪造固定阶段")
    current_minutes = now.hour * 60 + now.minute
    if cutoff_minutes is None:
        if stage:
            cutoff_minutes = SCHEDULED_STAGES[stage]["cutoff"]
        elif 11 * 60 + 30 < current_minutes < 13 * 60:
            cutoff_minutes = 11 * 60 + 30
        else:
            cutoff_minutes = min(max(current_minutes, 9 * 60 + 30), 15 * 60)
    if not 9 * 60 + 30 <= cutoff_minutes <= 15 * 60:
        raise ValueError("行情截点必须位于09:30-15:00")
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
    if stage:
        phase = stage
        phase_label = SCHEDULED_STAGES[stage]["label"]
    elif mode == "intraday-scan":
        phase = "intraday-scan"
        phase_label = f"盘中扫描 {minutes_to_hhmm(cutoff_minutes)}"
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
    trading_dates: set[str] = set()
    for symbol, name in BENCHMARKS.items():
        minute = parsed_minutes.get(symbol)
        history = history_raw.get(symbol)
        if not minute or not history:
            continue
        try:
            bars = synthesize_bars(history, minute)
            trading_dates.update(str(bar["date"]).replace("-", "") for bar in bars)
            closes = [float(bar["close"]) for bar in bars]
            row = {
                "symbol": symbol, "name": name, "price": round(minute["price"], 2),
                "change_pct": round(minute["change_pct"] or 0, 2),
                "tail_return_pct": round(minute["tail_return_pct"], 2),
                "last_time": minute["last_time"],
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
    entry_phase = phase in ("decision", "morning-decision", "tail-decision", "intraday-scan")
    actionable = entry_phase and market["state"] != "red"
    max_recommendations = 1 if market["state"] == "yellow" else 2
    total_limit = 30 if market["state"] == "yellow" else 60
    affordable = [
        item for item in strict_candidates
        if item["price"] * lot_size_for(item["code"]) <= CAPITAL * total_limit / 100
    ]
    recommendations = affordable[:max_recommendations] if actionable else []
    while recommendations:
        per_budget = CAPITAL * total_limit / 100 / len(recommendations)
        resized = [
            item for item in recommendations
            if item["price"] * lot_size_for(item["code"]) <= per_budget
        ]
        if not resized:
            recommendations = recommendations[:1]
            continue
        if len(resized) == len(recommendations):
            break
        recommendations = resized
    entry_time = minutes_to_hhmm(cutoff_minutes) if phase == "intraday-scan" else ("10:30" if phase == "morning-decision" else "14:30")
    if recommendations:
        decision = "buy"
        headline = f"{len(recommendations)} 个条件式买点"
        summary = f"仅在页面列出的{entry_time}触发条件仍成立时执行；任一取消条件出现则保持现金。"
        per_position = total_limit / len(recommendations)
    elif phase in ("observe", "morning-summary"):
        decision, headline = "watch", "观察，不在当前时点买入"
        summary = "排名仅用于缩小观察范围，等待固定决策时点确认。"
        per_position = 0
    elif entry_phase and market["state"] == "red":
        decision, headline = "cash", "空仓，仓位 0%"
        summary = "市场红灯覆盖个股排名，今天不建立隔夜仓位。"
        per_position = 0
    elif entry_phase:
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
        lot_size = lot_size_for(item["code"])
        shares = math.floor((CAPITAL * position_pct / 100) / entry / lot_size) * lot_size if entry else 0
        item["action"] = "条件式买入" if is_buy else "观察，不买"
        item["entry_time"] = entry_time
        item["position_pct"] = round(position_pct, 1)
        item["shares"] = shares
        item["capital_used"] = round(shares * entry, 2)
        item["planned_loss"] = round(shares * max(0, entry - item["invalidation"]), 2)
        item["entry_range"] = f"{entry * 0.997:.2f}-{entry * 1.003:.2f}" if is_buy else "未触发"
        item["no_chase"] = round(entry * 1.008, 3)
        item["exit_plan"] = [
            {"time": "T+1 10:30", "action": "出现失效、目标位或早盘走弱信号则卖出"},
            {"time": "T+1 / T+2", "action": "没有卖点可继续持有，不因时间焦虑强行交易"},
            {"time": "T+3 10:30", "action": "达到最长持有期，卖出全部可成交数量"},
        ]
        item.pop("minute_records", None)
    universe = []
    for item in securities:
        universe.append({
            key: item.get(key) for key in (
                "code", "name", "price", "change_pct", "score", "trend", "macd",
                "reward_risk", "last_time", "failures", "eligible", "vwap",
                "tail_return_pct", "tail_pullback_pct", "invalidation", "resistance",
                "ma5", "ma10", "ma20", "dif", "dea",
            )
        })
    data_as_of = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} {minutes_to_hhmm(cutoff_minutes)}"
    changes = [item["change_pct"] for item in universe if item.get("change_pct") is not None]
    breadth = {
        "up": sum(value > 0 for value in changes),
        "down": sum(value < 0 for value in changes),
        "flat": sum(value == 0 for value in changes),
        "average_change_pct": round(fmean(changes), 2) if changes else None,
    }
    response = {
        "generated_at": now.isoformat(timespec="seconds"),
        "data_as_of": data_as_of,
        "trade_date": trade_date,
        "phase": phase, "phase_label": phase_label,
        "decision": decision, "headline": headline, "summary": summary,
        "market": market, "indices": index_rows,
        "market_data_time": min(
            (row["last_time"] for row in index_rows if row.get("last_time")),
            default="",
        ),
        "breadth": breadth,
        "trading_dates": sorted(trading_dates),
        "capital": CAPITAL,
        "strategy_version": STRATEGY_VERSION,
        "parameters": {**DEFAULT_PARAMETERS, "capital": CAPITAL},
        "universe_version": UNIVERSE_VERSION,
        "stock_pool": list(WATCHLIST),
        "stock_pool_hash": hashlib.sha256(
            json.dumps(WATCHLIST, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
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
        "methodology": "1-3日短线策略 v2",
    }
    should_persist = phase in ("decision", "tail-decision", "intraday-scan") and trade_date == now.strftime("%Y%m%d")
    if persist and should_persist:
        persist_decision(response, parsed_minutes)
    elif persistence_data is not None and should_persist:
        persistence_data["minute_rows"] = parsed_minutes
        persistence_data["execution_rows"] = {
            symbol: {"records": [
                {"time": parts[0], "price": as_number(parts[1]), "volume": as_number(parts[2])}
                for raw in item.get("raw_records", [])
                if len(parts := str(raw).split()) >= 3
            ]}
            for symbol, item in minute_raw.items()
            if symbol not in BENCHMARKS
        }
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


def get_analysis(
    force: bool = False,
    now: datetime | None = None,
    cutoff_minutes: int | None = None,
    stage: str | None = None,
) -> dict[str, Any]:
    if now is not None or cutoff_minutes is not None or stage is not None:
        return run_analysis(now=now, cutoff_minutes=cutoff_minutes, stage=stage)
    now_monotonic = time.monotonic()
    with _cache_lock:
        if not force and _cache["data"] is not None and now_monotonic < _cache["expires"]:
            return _cache["data"]
        data = run_analysis()
        _cache["data"] = data
        _cache["expires"] = time.monotonic() + CACHE_SECONDS
        return data


def stock_lookup(analysis: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(stock.get("code")): stock
        for stock in analysis.get("universe", [])
        if stock.get("code")
    }


def load_manual_holdings(raw: str | None = None) -> list[dict[str, Any]]:
    raw = os.environ.get("ASTOCK_MANUAL_HOLDINGS", "") if raw is None else raw
    if not raw.strip():
        return []
    try:
        holdings = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"ASTOCK_MANUAL_HOLDINGS 必须是 JSON 数组：{error}") from error
    if not isinstance(holdings, list):
        raise ValueError("ASTOCK_MANUAL_HOLDINGS 必须是 JSON 数组")
    normalized = []
    seen_codes = set()
    for holding in holdings:
        if not isinstance(holding, dict) or set(holding) != {"code", "shares", "buy_price", "buy_date"}:
            raise ValueError("每笔人工持仓必须且只能包含 code、shares、buy_price、buy_date")
        code = str(holding["code"])
        shares = holding["shares"]
        buy_price = as_number(holding["buy_price"])
        buy_date = str(holding["buy_date"])
        try:
            parsed_date = datetime.strptime(buy_date, "%Y-%m-%d")
        except ValueError as error:
            raise ValueError("人工持仓 buy_date 必须使用 YYYY-MM-DD 日期") from error
        if (
            len(code) != 6 or not code.isdigit()
            or code in seen_codes
            or not isinstance(shares, int) or isinstance(shares, bool) or shares <= 0
            or buy_price is None or buy_price <= 0
            or parsed_date.strftime("%Y-%m-%d") != buy_date
        ):
            raise ValueError("人工持仓的股票代码、数量、成本或买入日期无效")
        seen_codes.add(code)
        normalized.append({
            "code": code,
            "shares": shares,
            "buy_price": buy_price,
            "buy_date": buy_date,
        })
    return normalized


def review_manual_holdings(
    holdings: list[dict[str, Any]], analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    stocks = stock_lookup(analysis)
    reviews = []
    for holding in holdings:
        stock = stocks.get(holding["code"], {})
        current_price = as_number(stock.get("price"))
        floating_pnl = (
            round((current_price - holding["buy_price"]) * holding["shares"], 2)
            if current_price is not None else None
        )
        return_pct = (
            round((current_price / holding["buy_price"] - 1) * 100, 2)
            if current_price is not None else None
        )
        reviews.append({
            **holding,
            "name": str(stock.get("name") or holding["code"]),
            "current_price": current_price,
            "floating_pnl": floating_pnl,
            "return_pct": return_pct,
        })
    return reviews


def holding_session_number(holding: dict[str, Any], analysis: dict[str, Any]) -> int:
    buy_date = str(holding.get("buy_date", "")).replace("-", "")
    trade_date = str(analysis.get("trade_date", "")).replace("-", "")
    trading_dates = {
        str(value).replace("-", "") for value in analysis.get("trading_dates", [])
    }
    return sum(buy_date < value <= trade_date for value in trading_dates)


def review_holdings(state: dict[str, Any], analysis: dict[str, Any]) -> list[dict[str, Any]]:
    stocks = stock_lookup(analysis)
    market = analysis.get("market", {})
    reviews = []
    for holding in state.get("holdings", []):
        stock = stocks.get(str(holding.get("code")))
        session_number = holding_session_number(holding, analysis)
        if not stock or as_number(stock.get("price")) is None:
            reviews.append({
                "holding": copy.deepcopy(holding),
                "decision": "hold",
                "current_price": None,
                "return_pct": None,
                "holding_session": session_number,
                "reasons": ["当前行情缺失，无法形成可执行卖点"],
            })
            continue
        current = float(stock["price"])
        buy_price = as_number(holding.get("buy_price"))
        invalidation = as_number(holding.get("invalidation"))
        target = as_number(holding.get("target_price"))
        vwap = as_number(stock.get("vwap"))
        return_pct = ((current / buy_price - 1) * 100) if buy_price else None
        sell_reasons = []
        if session_number >= MAX_HOLDING_SESSIONS:
            sell_reasons.append(f"已到第{MAX_HOLDING_SESSIONS}个可卖交易日，达到最长持有期")
        if invalidation is not None and current <= invalidation:
            sell_reasons.append("价格跌至交易逻辑失效位")
        if target is not None and current >= target:
            sell_reasons.append("价格达到计划目标位")
        if stock.get("trend") == "空头排列":
            sell_reasons.append("日线均线转为空头排列")
        early_weak = (
            vwap is not None
            and current < vwap * 0.995
            and as_number(stock.get("tail_return_pct"), 0) < -0.4
        )
        if early_weak:
            sell_reasons.append("盘中价格弱于分时均价且近期低点继续下移")
        if (
            market.get("state") == "red"
            and as_number(stock.get("change_pct"), 0) < 0
            and vwap is not None
            and current < vwap
        ):
            sell_reasons.append("市场红灯且个股弱于分时均价")
        if session_number <= 0:
            decision = "hold"
            reasons = ["当日买入受T+1限制，当前不可卖出"]
        elif sell_reasons:
            decision = "sell"
            reasons = list(dict.fromkeys(sell_reasons))
        else:
            decision = "hold"
            reasons = ["未触发失效位、目标位或盘中转弱卖点"]
        reviews.append({
            "holding": copy.deepcopy(holding),
            "decision": decision,
            "current_price": round(current, 3),
            "return_pct": round(return_pct, 2) if return_pct is not None else None,
            "holding_session": session_number,
            "reasons": reasons,
        })
    return reviews


def build_scheduled_report(
    stage: str,
    analysis: dict[str, Any],
    state: dict[str, Any],
    manual_holdings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if stage not in SCHEDULED_STAGES:
        raise ValueError(f"未知定时阶段：{stage}")
    reviews = review_holdings(state, analysis) if stage == "morning-decision" else []
    buys: list[dict[str, Any]] = []
    notes: list[str] = []
    open_holdings = state.get("holdings", [])
    if stage == "morning-decision":
        held_codes = {str(item.get("code")) for item in open_holdings}
        has_continuing_position = any(item["decision"] != "sell" for item in reviews)
        if has_continuing_position:
            notes.append("已有持仓继续占用本金池，本时点不新增买入。")
        else:
            buys = [
                copy.deepcopy(stock) for stock in analysis.get("candidates", [])
                if str(stock.get("code")) not in held_codes
                and int(as_number(stock.get("shares"), 0) or 0) > 0
            ]
    elif stage == "tail-decision":
        if open_holdings:
            notes.append("已有假定持仓，尾盘不重复占用5万元本金池。")
        else:
            buys = [
                copy.deepcopy(stock) for stock in analysis.get("candidates", [])
                if int(as_number(stock.get("shares"), 0) or 0) > 0
            ]
    report = {
        "report_type": "scheduled",
        "stage": stage,
        "label": SCHEDULED_STAGES[stage]["label"],
        "event_id": f"{analysis.get('trade_date', '')}:{stage}",
        "analysis": analysis,
        "holding_reviews": reviews,
        "manual_holding_reviews": (
            review_manual_holdings(manual_holdings or [], analysis)
            if stage == "close-summary" else []
        ),
        "buys": buys,
        "notes": notes,
    }
    report["trade_actions"] = (
        sum(item["decision"] == "sell" for item in reviews) + len(buys)
    )
    return report


def decorate_analysis_with_state(
    analysis: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    payload = copy.deepcopy(analysis)
    reviews = review_holdings(state, payload)
    payload["assumed_holdings"] = [
        {
            **item["holding"],
            "current_price": item.get("current_price"),
            "return_pct": item.get("return_pct"),
            "holding_session": item.get("holding_session", 0),
            "review_decision": item.get("decision"),
            "review_reasons": item.get("reasons", []),
        }
        for item in reviews
    ]
    payload["recent_trades"] = copy.deepcopy(state.get("history", [])[-10:])
    stage = None
    if payload.get("phase") == "morning-decision":
        stage = "morning-decision"
    elif payload.get("phase") == "decision":
        stage = "tail-decision"
    if not stage:
        return payload
    report = build_scheduled_report(stage, payload, state)
    payload["holding_reviews"] = report["holding_reviews"]
    payload["candidates"] = report["buys"]
    payload["position_pct"] = round(
        sum(as_number(item.get("position_pct"), 0) or 0 for item in report["buys"]), 1,
    )
    sells = sum(item["decision"] == "sell" for item in report["holding_reviews"])
    holds = len(report["holding_reviews"]) - sells
    if sells and report["buys"]:
        payload["decision"] = "buy"
        payload["headline"] = f"卖出 {sells} 只，并买入 {len(report['buys'])} 只"
        payload["summary"] = "飞书机器人推送成功后，按参考价默认记录上述明确指令。"
    elif sells:
        payload["decision"] = "cash"
        payload["headline"] = f"{sells} 个卖点，按计划退出"
        payload["summary"] = "页面收益率按当前参考价估算，未计费用与滑点。"
    elif holds:
        payload["decision"] = "watch"
        payload["headline"] = "继续持有，本次不卖"
        payload["summary"] = "没有触发卖点；已有持仓时不重复占用本金池。"
    elif not report["buys"]:
        payload["decision"] = "cash"
        payload["headline"] = "本次没有买点和卖点"
        payload["summary"] = "不操作，保持当前现金和持仓状态。"
    return payload


def apply_scheduled_report(
    state: dict[str, Any],
    report: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    updated = copy.deepcopy(state)
    sold_by_code = {
        str(item["holding"].get("code")): item
        for item in report.get("holding_reviews", [])
        if item.get("decision") == "sell"
    }
    remaining = []
    for holding in updated.get("holdings", []):
        review = sold_by_code.get(str(holding.get("code")))
        if not review:
            remaining.append(holding)
            continue
        closed = copy.deepcopy(holding)
        closed.update({
            "status": "closed",
            "sell_date": report["analysis"].get("trade_date", ""),
            "sell_time": report.get("scan_time", "1030"),
            "sell_price": review.get("current_price"),
            "return_pct": review.get("return_pct"),
            "sell_reason": "；".join(review.get("reasons", [])),
        })
        updated.setdefault("history", []).append(closed)
    updated["holdings"] = remaining
    existing_codes = {str(item.get("code")) for item in remaining}
    stage = report["stage"]
    buy_time = report.get("scan_time") or minutes_to_hhmm(SCHEDULED_STAGES[stage]["cutoff"]).replace(":", "")
    trade_date = report["analysis"].get("trade_date", "")
    for stock in report.get("buys", []):
        code = str(stock.get("code", ""))
        shares = int(as_number(stock.get("shares"), 0) or 0)
        price = as_number(stock.get("price"))
        if not code or code in existing_codes or shares <= 0 or price is None:
            continue
        updated["holdings"].append({
            "id": f"{trade_date}-{buy_time}-{code}",
            "code": code,
            "name": stock.get("name", ""),
            "buy_date": trade_date,
            "buy_time": buy_time,
            "buy_price": round(price, 3),
            "shares": shares,
            "capital_used": round(shares * price, 2),
            "invalidation": stock.get("invalidation"),
            "target_price": stock.get("resistance"),
            "source": "scan" if stage == "intraday-scan" else ("morning" if stage == "morning-decision" else "tail"),
            "status": "open",
        })
        existing_codes.add(code)
    processed = updated.setdefault("processed_events", [])
    if report["event_id"] not in processed:
        processed.append(report["event_id"])
    updated["processed_events"] = processed[-120:]
    updated["history"] = updated.get("history", [])[-200:]
    if report.get("stage") == "intraday-scan":
        updated["signal_trade_date"] = trade_date
        updated["signal_states"] = copy.deepcopy(report.get("signal_states", {}))
        for code in sold_by_code:
            generation = updated["signal_states"].get(code, {}).get("generation", 0)
            updated["signal_states"][code] = {
                "side": "none", "fingerprint": "", "generation": generation,
                "updated_at": now.isoformat(timespec="seconds"),
            }
    updated["updated_at"] = now.isoformat(timespec="seconds")
    updated["last_event"] = {
        "id": report["event_id"],
        "trade_actions": report.get("trade_actions", 0),
    }
    return updated


def scan_window_status(now: datetime) -> tuple[bool, str]:
    if now.weekday() >= 5:
        return False, "周末非交易日"
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 35:
        return False, "09:35前不生成盘中信号"
    if not any(start <= minutes <= end for start, end in SCAN_WINDOWS):
        return False, "当前不在盘中扫描窗口"
    return True, ""


def _signal_fingerprint(
    trade_date: str, code: str, side: str, holding_id: str = "",
) -> str:
    core = {
        "trade_date": trade_date,
        "symbol": code,
        "side": side,
        "strategy_version": STRATEGY_VERSION,
        "holding_id": holding_id,
    }
    return hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _notification_fingerprint(action: dict[str, Any]) -> str:
    core = {
        "signal_fingerprint": action["fingerprint"],
        "generation": action["generation"],
    }
    return hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_intraday_scan_report(
    analysis: dict[str, Any], state: dict[str, Any], now: datetime,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], set[str]]:
    trade_date = str(analysis.get("trade_date", ""))
    cutoff = now.strftime("%H%M")
    universe = stock_lookup(analysis)
    reviews = review_holdings(state, analysis)
    unavailable = {
        str(item["holding"].get("code")) for item in reviews
        if item.get("current_price") is None
        or hhmm_to_minutes(str(universe.get(str(item["holding"].get("code")), {}).get("last_time", "")))
        < hhmm_to_minutes(cutoff)
    }
    sells = [
        item for item in reviews
        if item.get("decision") == "sell" and str(item["holding"].get("code")) not in unavailable
    ]
    held_codes = {str(item.get("code")) for item in state.get("holdings", [])}
    bought_today = {
        str(item.get("code")) for item in state.get("history", [])
        if str(item.get("buy_date", "")).replace("-", "") == trade_date
    }
    buys = []
    if not sells and not unavailable and not held_codes:
        buys = [
            copy.deepcopy(item) for item in analysis.get("candidates", [])
            if str(item.get("code")) not in held_codes | bought_today
            and int(as_number(item.get("shares"), 0) or 0) > 0
            and hhmm_to_minutes(str(item.get("last_time", ""))) >= hhmm_to_minutes(cutoff)
        ]
    actions: list[dict[str, Any]] = []
    for review in sells:
        holding = review["holding"]
        code = str(holding.get("code", ""))
        shares = int(as_number(holding.get("shares"), 0) or 0)
        fingerprint = _signal_fingerprint(trade_date, code, "sell", str(holding.get("id", "")))
        actions.append({
            "side": "sell", "code": code, "name": holding.get("name", ""),
            "shares": shares, "price": review.get("current_price"),
            "holding_id": holding.get("id", ""), "holding": copy.deepcopy(holding),
            "holding_session": review.get("holding_session", 0),
            "reasons": review.get("reasons", []), "reason_code": SCAN_REASON_CODES["sell"],
            "fingerprint": fingerprint,
        })
    for stock in buys:
        code = str(stock.get("code", ""))
        shares = int(as_number(stock.get("shares"), 0) or 0)
        fingerprint = _signal_fingerprint(trade_date, code, "buy")
        actions.append({
            "side": "buy", "code": code, "name": stock.get("name", ""),
            "shares": shares, "price": stock.get("price"), "entry_range": stock.get("entry_range"),
            "invalidation": stock.get("invalidation"), "resistance": stock.get("resistance"),
            "reasons": ["通过市场、技术、分时、收益风险比与公告硬门槛"],
            "reason_code": SCAN_REASON_CODES["buy"], "fingerprint": fingerprint,
            "stock": stock,
        })
    actions.sort(key=lambda item: (0 if item["side"] == "sell" else 1, item["code"], item.get("holding_id", "")))

    previous = state.get("signal_states", {}) if state.get("signal_trade_date") == trade_date else {}
    next_states = copy.deepcopy(previous)
    evaluated: set[str] = set()

    def update_signal_state(code: str, action: dict[str, Any] | None) -> None:
        prior = previous.get(code, {})
        generation = prior.get("generation", 0)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            generation = 0
        if action is None:
            next_states[code] = {
                "side": "none", "fingerprint": "", "generation": generation,
                "updated_at": now.isoformat(timespec="seconds"),
            }
            return
        changed = prior.get("side") != action["side"] or prior.get("fingerprint") != action["fingerprint"]
        if changed:
            generation += 1
        action["generation"] = generation
        action["notification_fingerprint"] = _notification_fingerprint(action)
        next_states[code] = {
            "side": action["side"], "fingerprint": action["fingerprint"], "generation": generation,
            "updated_at": now.isoformat(timespec="seconds"),
        }

    if sells:
        for review in reviews:
            code = str(review["holding"].get("code", ""))
            if code in unavailable:
                continue
            evaluated.add(code)
            update_signal_state(code, next((item for item in actions if item["code"] == code and item["side"] == "sell"), None))
    else:
        for code, stock in universe.items():
            if hhmm_to_minutes(str(stock.get("last_time", ""))) < hhmm_to_minutes(cutoff):
                continue
            evaluated.add(code)
            update_signal_state(code, next((item for item in actions if item["code"] == code and item["side"] == "buy"), None))
        for review in reviews:
            code = str(review["holding"].get("code", ""))
            if code in unavailable:
                continue
            evaluated.add(code)
            update_signal_state(code, None)
    for action in actions:
        if action["code"] not in evaluated:
            evaluated.add(action["code"])
            update_signal_state(action["code"], action)

    changed_actions = [
        action for action in actions
        if previous.get(action["code"], {}).get("side") != action["side"]
        or previous.get(action["code"], {}).get("fingerprint") != action["fingerprint"]
    ]
    batch_fingerprint = hashlib.sha256(
        "|".join(item["notification_fingerprint"] for item in changed_actions).encode("ascii")
    ).hexdigest() if changed_actions else ""
    event_id = f"{trade_date}:scan:{cutoff}:{batch_fingerprint[:12]}" if batch_fingerprint else ""
    report = {
        "report_type": "intraday-scan", "stage": "intraday-scan", "label": "盘中信号状态变化",
        "event_id": event_id, "analysis": analysis, "decision_at": now.isoformat(timespec="seconds"),
        "scan_time": cutoff, "actions": changed_actions,
        "holding_reviews": [
            {
                "holding": item["holding"], "decision": "sell", "current_price": item["price"],
                "holding_session": item.get("holding_session", 0), "reasons": item.get("reasons", []),
            }
            for item in changed_actions if item["side"] == "sell"
        ],
        "buys": [item["stock"] for item in changed_actions if item["side"] == "buy"],
        "trade_actions": len(changed_actions), "signal_states": next_states,
    }
    return report, next_states, evaluated


def format_intraday_scan_message(report: dict[str, Any]) -> str:
    analysis = report["analysis"]
    lines = [
        f"**{analysis.get('trade_date', '')} 盘中信号变化**",
        f">决策时间：{report.get('decision_at', '')} · 市场：{analysis.get('market', {}).get('label', '未核验')}",
        ">持仓口径：仅基于本地假定持仓，不代表券商真实持仓。",
        "",
    ]
    for action in report.get("actions", []):
        side = "卖出" if action["side"] == "sell" else "买入"
        lines.extend([
            f"**{side} · {action.get('name', '')} {action.get('code', '')}**",
            f">side：{action['side']} · 参考价：{action.get('price', '未核验')}" + (f" · 区间：{action.get('entry_range')}" if action["side"] == "buy" else ""),
            f">数量：{action.get('shares', 0)} 股 · 规则：{action.get('reason_code', '')}",
            f">稳定原因：{'；'.join(action.get('reasons', []))}",
            ">T+1：当日买入不可卖；卖出仅针对本地假定持仓。" if action["side"] == "sell" else ">T+1：买入后当日不可卖出，下一交易日起复核。",
            "",
        ])
    lines.append("仅供研究参考，不构成个性化投资建议。")
    return "\n".join(lines)[:3800]


def _pending_integrity_hash(pending: dict[str, Any]) -> str:
    payload = {key: value for key, value in pending.items() if key != "integrity_hash"}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _canonical_execution_rows(report: dict[str, Any]) -> dict[str, Any]:
    source = report.get("analysis", {}).get("execution_evidence_source")
    if not isinstance(source, dict):
        raise CorruptLocalData("pending analysis execution evidence source 无效")
    decision_minute = (
        report.get("scan_time")
        if report.get("stage") == "intraday-scan"
        else report.get("analysis", {}).get("market_data_time", "")
    )
    cutoff = str(decision_minute).replace(":", "")
    if len(cutoff) != 4 or not cutoff.isdigit():
        raise CorruptLocalData("pending execution evidence decision minute 无效")
    minutes = int(cutoff[:2]) * 60 + int(cutoff[2:]) + 1
    expected = f"{minutes // 60:02d}{minutes % 60:02d}"
    codes = {
        str(item.get("code", "")) for item in report.get("buys", [])
        if isinstance(item, dict) and item.get("code")
    }
    codes.update(
        str(item.get("holding", {}).get("code", ""))
        for item in report.get("holding_reviews", [])
        if isinstance(item, dict) and item.get("decision") == "sell"
        and isinstance(item.get("holding"), dict) and item["holding"].get("code")
    )
    canonical: dict[str, Any] = {}
    for code in sorted(codes):
        symbol = symbol_for(code)
        node = source.get(symbol)
        if not isinstance(node, dict) or not isinstance(node.get("records"), list):
            canonical[symbol] = {"records": []}
            continue
        records = []
        for row in node["records"]:
            if not isinstance(row, dict) or str(row.get("time", "")).replace(":", "") != expected:
                continue
            records.append({
                key: row.get(key)
                for key in ("time", "price", "volume", "suspended", "at_limit")
                if key in row
            })
        canonical[symbol] = {"records": records}
    return canonical


def _build_pending(
    report: dict[str, Any], token: str, message: str, prepared_at: str,
    test_message: bool, execution_rows: dict[str, Any], kind: str,
) -> dict[str, Any]:
    report = copy.deepcopy(report)
    report.setdefault("analysis", {})["execution_evidence_source"] = copy.deepcopy(execution_rows)
    pending = {
        "schema": PENDING_SCHEMA, "kind": kind, "event_id": report["event_id"],
        "commit_token": token, "prepared_at": prepared_at, "test_message": test_message,
        "message": message, "report": report,
    }
    pending["execution_rows"] = _canonical_execution_rows(report)
    pending["integrity_hash"] = _pending_integrity_hash(pending)
    return pending


def _validate_pending_payload(pending: Any) -> dict[str, Any]:
    if not isinstance(pending, dict):
        raise CorruptLocalData("pending 数据格式无效")
    required_types = {
        "schema": str, "kind": str, "event_id": str, "commit_token": str,
        "prepared_at": str, "test_message": bool, "message": str, "report": dict,
        "execution_rows": dict, "integrity_hash": str,
    }
    for key, expected_type in required_types.items():
        if not isinstance(pending.get(key), expected_type):
            raise CorruptLocalData(f"pending 字段 {key} 格式无效")
    if pending["schema"] != PENDING_SCHEMA or pending["kind"] not in {"scheduled", "intraday-scan"}:
        raise CorruptLocalData("pending schema 或 kind 无效")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", pending["commit_token"]):
        raise CorruptLocalData("pending commit_token 格式无效")
    if not pending["message"].strip() or not pending["prepared_at"].strip():
        raise CorruptLocalData("pending message 或 prepared_at 为空")
    report = pending["report"]
    if report.get("event_id") != pending["event_id"] or not isinstance(report.get("stage"), str):
        raise CorruptLocalData("pending report identity 无效")
    if pending["kind"] == "intraday-scan":
        if pending["test_message"] is not False or report.get("stage") != "intraday-scan":
            raise CorruptLocalData("盘中 pending kind/test/stage 无效")
        actions = report.get("actions")
        states = report.get("signal_states")
        if not isinstance(actions, list) or not actions or not isinstance(states, dict):
            raise CorruptLocalData("盘中 pending actions/signal_states 无效")
        identities = []
        for action in actions:
            if not isinstance(action, dict) or action.get("side") not in {"buy", "sell"}:
                raise CorruptLocalData("盘中 pending action 格式无效")
            code = action.get("code")
            generation = action.get("generation")
            if not isinstance(code, str) or not code or not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
                raise CorruptLocalData("盘中 pending action identity 无效")
            expected_signal = _signal_fingerprint(
                str(report.get("analysis", {}).get("trade_date", "")), code, action["side"],
                str(action.get("holding_id", "")) if action["side"] == "sell" else "",
            )
            if action.get("fingerprint") != expected_signal or action.get("notification_fingerprint") != _notification_fingerprint(action):
                raise CorruptLocalData("盘中 pending action fingerprint 无效")
            signal_state = states.get(code)
            if not isinstance(signal_state, dict) or signal_state.get("side") != action["side"] or signal_state.get("fingerprint") != expected_signal or signal_state.get("generation") != generation:
                raise CorruptLocalData("盘中 pending signal state identity 无效")
            identities.append(action["notification_fingerprint"])
        if actions != sorted(actions, key=lambda item: (0 if item["side"] == "sell" else 1, item["code"], item.get("holding_id", ""))):
            raise CorruptLocalData("盘中 pending action 排序无效")
        batch = hashlib.sha256("|".join(identities).encode("ascii")).hexdigest()[:12]
        trade_date = str(report.get("analysis", {}).get("trade_date", ""))
        scan_time = report.get("scan_time")
        if pending["event_id"] != f"{trade_date}:scan:{scan_time}:{batch}":
            raise CorruptLocalData("盘中 pending event identity 无效")
    elif report.get("stage") not in SCHEDULED_STAGES or pending["event_id"] != f"{report.get('analysis', {}).get('trade_date')}:{report.get('stage')}":
        raise CorruptLocalData("定时 pending event identity 无效")
    if not secrets.compare_digest(pending["integrity_hash"], _pending_integrity_hash(pending)):
        raise CorruptLocalData("pending integrity hash 无效")
    return pending


def _validate_pending(pending: Any, state: dict[str, Any]) -> dict[str, Any]:
    validated = _validate_pending_payload(pending)
    report = validated["report"]
    if report.get("stage") == "intraday-scan":
        if not isinstance(report.get("decision_at"), str):
            raise CorruptLocalData("盘中 pending decision_at 无效")
        expected, _, _ = build_intraday_scan_report(
            report.get("analysis", {}), state, datetime.fromisoformat(report["decision_at"]),
        )
        if expected.get("event_id") != report.get("event_id") or expected.get("actions") != report.get("actions") or expected.get("signal_states") != report.get("signal_states"):
            raise CorruptLocalData("盘中 pending report 无法从 analysis/state 重算")
    else:
        expected = build_scheduled_report(report["stage"], report.get("analysis", {}), state)
        if expected != report:
            raise CorruptLocalData("定时 pending report 无法从 analysis/state 重算")
    expected_message = (
        format_intraday_scan_message(expected)
        if validated["kind"] == "intraday-scan"
        else format_lightclaw_message(expected, test=validated["test_message"])
    )
    if validated["message"].encode("utf-8") != expected_message.encode("utf-8"):
        raise CorruptLocalData("pending message 无法从 report 重建")
    if validated["execution_rows"] != _canonical_execution_rows(expected):
        raise CorruptLocalData("pending execution evidence 无法从 analysis 重建")
    return validated


def lightclaw_scan_prepare(
    now: datetime | None = None, state_path: Path | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(SHANGHAI)
    allowed, reason = scan_window_status(now)
    if not allowed:
        return {"status": "no_signal", "reason": reason, "decision_at": now.isoformat(timespec="seconds")}
    today = now.strftime("%Y%m%d")
    batch_prefix = f"{today}:scan:{now:%H%M}:"
    with trading_state_transaction(state_path) as locked_path:
        state = load_trading_state(locked_path)
        pending = state.get("pending_report")
        if pending is not None:
            pending = _validate_pending(pending, state)
            if str(pending.get("event_id", "")).startswith(batch_prefix):
                return {
                    "status": "already_pending", "event_id": pending.get("event_id", ""),
                    "commit_token": pending.get("commit_token", ""), "message": pending.get("message", ""),
                }
            return {
                "status": "conflict", "reason": "存在其他未送达 pending，禁止覆盖",
                "pending_event_id": pending.get("event_id", ""),
            }
        persistence_data: dict[str, Any] = {}
        cutoff = now.hour * 60 + now.minute
        analysis = run_analysis(
            now=now, cutoff_minutes=cutoff, persist=False, persistence_data=persistence_data,
            mode="intraday-scan",
        )
        if analysis.get("trade_date") != today:
            return {
                "status": "no_signal", "reason": "无已确认的当日交易行情，可能休市或行情陈旧",
                "trade_date": analysis.get("trade_date", ""),
            }
        if hhmm_to_minutes(str(analysis.get("market_data_time", ""))) < cutoff:
            return {
                "status": "no_signal", "reason": "整体行情尚未达到本轮截点，未更新信号状态",
                "market_data_time": analysis.get("market_data_time", ""), "required_time": minutes_to_hhmm(cutoff),
            }
        report, next_states, evaluated = build_intraday_scan_report(analysis, state, now)
        if not report["actions"]:
            if state.get("signal_trade_date") != today or next_states != state.get("signal_states", {}):
                state["signal_trade_date"] = today
                state["signal_states"] = next_states
                state["updated_at"] = now.isoformat(timespec="seconds")
                save_trading_state(state, locked_path)
            return {
                "status": "no_signal", "reason": "信号状态未变化",
                "trade_date": today, "evaluated_symbols": len(evaluated),
            }
        token = secrets.token_urlsafe(32)
        message = format_intraday_scan_message(report)
        state["pending_report"] = _build_pending(
            report, token, message, now.isoformat(timespec="seconds"), False,
            persistence_data.get("execution_rows", {}), "intraday-scan",
        )
        save_trading_state(state, locked_path)
    return {
        "status": "prepared", "event_id": report["event_id"], "commit_token": token,
        "message": message, "trade_actions": len(report["actions"]),
    }


def lightclaw_prepare(
    stage: str,
    now: datetime | None = None,
    state_path: Path | None = None,
    test_message: bool = False,
) -> dict[str, Any]:
    if stage not in SCHEDULED_STAGES:
        raise ValueError(f"未知定时阶段：{stage}")
    now = now or datetime.now(SHANGHAI)
    today = now.strftime("%Y%m%d")
    event_id = f"{today}:{stage}"
    with trading_state_transaction(state_path) as locked_path:
        state = load_trading_state(locked_path)
        pending = state.get("pending_report")
        if pending is not None:
            pending = _validate_pending(pending, state)
        if event_id in state.get("processed_events", []):
            return {"status": "already_committed", "event_id": event_id}
        if pending is not None:
            pending_test_message = pending["test_message"]
            if pending.get("event_id") != event_id or pending_test_message != test_message:
                return {
                    "status": "pending_conflict",
                    "event_id": event_id,
                    "test_message": test_message,
                    "pending_event_id": pending.get("event_id", ""),
                    "pending_test_message": pending_test_message,
                    "reason": "存在未完成且标识不一致的 pending，请先提交该 pending",
                }
            return {
                "status": "prepared",
                "event_id": event_id,
                "commit_token": pending["commit_token"],
                "message": pending["message"],
                "test_message": pending_test_message,
                "reused": True,
            }
        persistence_data: dict[str, Any] = {}
        analysis = run_analysis(
            now=now, stage=stage, persist=False, persistence_data=persistence_data,
        )
        if analysis.get("trade_date") != today:
            return {
                "status": "skipped",
                "reason": "行情不是当日数据，可能休市",
                "trade_date": analysis.get("trade_date", ""),
            }
        market_data_minutes = hhmm_to_minutes(str(analysis.get("market_data_time", "")))
        required_minutes = int(SCHEDULED_STAGES[stage]["cutoff"])
        if market_data_minutes < required_minutes:
            return {
                "status": "skipped",
                "reason": "行情尚未更新到该阶段截点",
                "market_data_time": analysis.get("market_data_time", ""),
                "required_time": minutes_to_hhmm(required_minutes),
            }
        report = build_scheduled_report(stage, analysis, state)
        message = format_lightclaw_message(report, test=test_message)
        token = secrets.token_urlsafe(32)
        state["pending_report"] = _build_pending(
            report, token, message, now.isoformat(timespec="seconds"), test_message,
            persistence_data.get("execution_rows", {}), "scheduled",
        )
        save_trading_state(state, locked_path)
    return {
        "status": "prepared",
        "event_id": event_id,
        "commit_token": token,
        "message": message,
        "test_message": test_message,
        "reused": False,
    }


def lightclaw_commit(
    commit_token: str,
    now: datetime | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    if not commit_token:
        raise ValueError("commit token 不能为空")
    now = now or datetime.now(SHANGHAI)
    with trading_state_transaction(state_path) as locked_path:
        state = load_trading_state(locked_path)
        pending = state.get("pending_report")
        if pending is not None:
            pending = _validate_pending(pending, state)
        committed_tokens = state.get("committed_delivery_tokens", {})
        if commit_token in committed_tokens:
            event_id = committed_tokens[commit_token]
            archive_path = archive_path_for(locked_path, event_id)
            if archive_path.exists():
                try:
                    archived = json.loads(archive_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise RuntimeError(f"正式归档无法读取：{error}") from error
                if archived.get("archive_status") == "pending":
                    committed_archive = {**archived, "archive_status": "committed"}
                    archive_formal_event(archive_path, committed_archive)
            return {
                "status": "already_committed",
                "event_id": event_id,
                "open_holdings": len(state.get("holdings", [])),
            }
        if pending is None:
            raise ValueError("commit token 无效或不存在对应 pending")
        if not secrets.compare_digest(pending["commit_token"], commit_token):
            raise ValueError("commit token 无效或不存在对应 pending")
        report = pending["report"]
        if report["event_id"] in state.get("processed_events", []):
            updated = copy.deepcopy(state)
        else:
            updated = apply_scheduled_report(state, report, now)
        updated["pending_report"] = None
        updated.setdefault("committed_delivery_tokens", {})[commit_token] = report["event_id"]
        archive_path = None
        formal_event = None
        if not pending.get("test_message", False):
            archive_path = archive_path_for(locked_path, report["event_id"])
            if archive_path.exists():
                try:
                    formal_event = json.loads(archive_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise RuntimeError(f"正式归档无法读取：{error}") from error
                if formal_event.get("archive_status") != "pending":
                    raise RuntimeError("正式归档状态与交易状态不一致")
                archive_formal_event(archive_path, formal_event)
            else:
                formal_event = build_formal_event(pending, now, pending.get("execution_rows"))
                archive_formal_event(archive_path, {**formal_event, "archive_status": "pending"})
        save_trading_state(updated, locked_path)
        if archive_path is not None and formal_event is not None:
            archive_formal_event(archive_path, {**formal_event, "archive_status": "committed"})
    return {
        "status": "committed",
        "event_id": report["event_id"],
        "trade_actions": report.get("trade_actions", 0),
        "open_holdings": len(updated.get("holdings", [])),
    }


def scheduled_push_conflict(state: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    pending = state.get("pending_report")
    if pending is not None:
        pending = _validate_pending(pending, state)
    if event_id in state.get("processed_events", []):
        return {
            "sent": False,
            "reason": "该阶段已经成功推送",
            "event_id": event_id,
        }
    if pending is not None:
        return {
            "sent": False,
            "reason": "存在未完成的 LightClaw pending，禁止机器人外发和状态应用",
            "status": "pending_conflict",
            "event_id": event_id,
            "pending_event_id": pending.get("event_id", ""),
        }
    return None


def scheduled_push(
    stage: str,
    webhook: str = "",
    now: datetime | None = None,
    state_path: Path | None = None,
    signing_secret: str | None = None,
    app_id: str | None = None,
    app_secret: str | None = None,
    receive_id: str | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(SHANGHAI)
    if webhook:
        webhook = validate_feishu_webhook(webhook)
    manual_holdings = load_manual_holdings()
    if stage not in SCHEDULED_STAGES:
        raise ValueError(f"未知定时阶段：{stage}")
    today = now.strftime("%Y%m%d")
    event_id = f"{today}:{stage}"
    with trading_state_transaction(state_path) as locked_path:
        conflict = scheduled_push_conflict(load_trading_state(locked_path), event_id)
        if conflict:
            return conflict
    persistence_data: dict[str, Any] = {}
    analysis = run_analysis(now=now, stage=stage, persist=False, persistence_data=persistence_data)
    if analysis.get("trade_date") != today:
        return {
            "sent": False,
            "reason": "行情不是当日数据，可能休市",
            "trade_date": analysis.get("trade_date", ""),
        }
    market_data_minutes = hhmm_to_minutes(str(analysis.get("market_data_time", "")))
    required_minutes = int(SCHEDULED_STAGES[stage]["cutoff"])
    if market_data_minutes < required_minutes:
        return {
            "sent": False,
            "reason": "行情尚未更新到该阶段截点",
            "market_data_time": analysis.get("market_data_time", ""),
            "required_time": minutes_to_hhmm(required_minutes),
        }
    with trading_state_transaction(state_path) as locked_path:
        state = load_trading_state(locked_path)
        conflict = scheduled_push_conflict(state, event_id)
        if conflict:
            return conflict
        report = build_scheduled_report(stage, analysis, state, manual_holdings)
        minute_rows = persistence_data.get("minute_rows")
        if minute_rows is not None:
            persist_decision(analysis, minute_rows)
        send_feishu_push(
            report, webhook=webhook or None, signing_secret=signing_secret,
            app_id=app_id, app_secret=app_secret, receive_id=receive_id,
        )
        updated = apply_scheduled_report(state, report, now)
        save_trading_state(updated, locked_path)
    return {
        "sent": True,
        "trade_date": analysis.get("trade_date", ""),
        "stage": stage,
        "trade_actions": report["trade_actions"],
        "open_holdings": len(updated.get("holdings", [])),
    }


def push_once(webhook: str = "", now: datetime | None = None) -> dict[str, Any]:
    """Backward-compatible alias for the 14:30 scheduled decision."""
    return scheduled_push("tail-decision", webhook, now=now)


def push_scheduler_loop() -> None:
    while not _scheduler_stop.wait(30):
        now = datetime.now(SHANGHAI)
        minutes = now.hour * 60 + now.minute
        if now.weekday() >= 5:
            continue
        stage = next(
            (name for name, config in SCHEDULED_STAGES.items() if config["push_time"] == minutes),
            None,
        )
        if not stage:
            continue
        config = load_push_config(include_secret=True)
        event_id = f"{now:%Y%m%d}:{stage}"
        has_app = bool(config.get("app_id") and config.get("app_secret"))
        has_webhook = bool(config.get("webhook"))
        if not config.get("enabled") or (not has_app and not has_webhook) or config.get("last_sent") == event_id:
            continue
        try:
            result = scheduled_push(
                stage, config.get("webhook", ""), now=now,
                signing_secret=config.get("signing_secret", ""),
                app_id=config.get("app_id", ""), app_secret=config.get("app_secret", ""),
                receive_id=config.get("receive_id", ""),
            )
            status = "推送成功" if result.get("sent") else f"跳过：{result.get('reason', '无需推送')}"
            save_push_config({"last_sent": event_id, "last_result": f"{now:%Y-%m-%d %H:%M} {status}"})
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
                analysis = get_analysis(force=query.get("refresh") == ["1"])
                self.send_json(decorate_analysis_with_state(analysis, load_trading_state()))
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
                send_feishu_push(analysis, test=True)
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
    parser.add_argument("--push-once", action="store_true", help="兼容旧命令：运行一次14:30决策推送")
    parser.add_argument(
        "--scheduled-push",
        choices=tuple(SCHEDULED_STAGES),
        help="运行指定阶段的定时分析与推送",
    )
    parser.add_argument(
        "--print-report",
        choices=tuple(SCHEDULED_STAGES),
        help="只生成指定阶段的报告文本输出到 stdout，不推送、不写交易状态",
    )
    lightclaw_action = parser.add_mutually_exclusive_group()
    lightclaw_action.add_argument(
        "--lightclaw-prepare",
        choices=tuple(SCHEDULED_STAGES),
        help="生成 LightClaw 待发送消息并返回 commit token；本入口绝不外发",
    )
    lightclaw_action.add_argument(
        "--lightclaw-scan-prepare",
        action="store_true",
        help="生成盘中状态变化待发送消息；本入口绝不外发",
    )
    lightclaw_action.add_argument(
        "--lightclaw-commit",
        metavar="TOKEN",
        help="仅在上层确认飞书送达后提交对应 token",
    )
    parser.add_argument(
        "--lightclaw-test-message",
        action="store_true",
        help="为并行观察期消息添加 LightClaw 测试标记",
    )
    parser.add_argument(
        "--weekly-review",
        action="store_true",
        help="只读生成正式提交事件的周复盘 JSON；不外发、不修改交易状态或策略参数",
    )
    parser.add_argument("--as-of", help="周复盘截止日期（ISO 日期或时间）")
    parser.add_argument("--weekly-config", type=Path, help="本地白名单 JSON 配置")
    parser.add_argument("--config", type=Path, help="--weekly-config 的等价别名")
    args = parser.parse_args()
    if args.weekly_review:
        try:
            if args.weekly_config is not None and args.config is not None:
                raise ValueError("--weekly-config 与 --config 不得同时使用")
            as_of = args.as_of or datetime.now(SHANGHAI).date().isoformat()
            config = load_weekly_config(args.weekly_config or args.config)
            archive_dir = Path(config.get("archive_dir", DATA_DIR / "formal-events"))
            result = weekly_review(as_of, archive_dir, config)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except ValueError as error:
            print(json.dumps({"status": "error", "schema": "astock.weekly-review/v1", "errors": [str(error)]}, ensure_ascii=False))
            return 1
        except Exception as error:
            print(json.dumps({"status": "corrupt_data", "schema": "astock.weekly-review/v1", "errors": [str(error)]}, ensure_ascii=False))
            return 2
    if args.lightclaw_prepare or args.lightclaw_scan_prepare or args.lightclaw_commit:
        try:
            if args.lightclaw_prepare:
                result = lightclaw_prepare(args.lightclaw_prepare, test_message=args.lightclaw_test_message)
            elif args.lightclaw_scan_prepare:
                if args.lightclaw_test_message:
                    raise ValueError("盘中正式扫描不支持测试消息模式")
                result = lightclaw_scan_prepare()
            else:
                result = lightclaw_commit(args.lightclaw_commit)
            print(json.dumps(result, ensure_ascii=False))
            return 3 if result.get("status") in {"pending_conflict", "conflict"} else 0
        except CorruptLocalData as error:
            print(json.dumps({"status": "corrupt_data", "error": str(error)}, ensure_ascii=False))
            return 2
        except Exception as error:
            print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
            return 1
    if args.print_report:
        try:
            now = datetime.now(SHANGHAI)
            stage = args.print_report
            if stage not in SCHEDULED_STAGES:
                print(f"未知阶段：{stage}")
                return 2
            manual_holdings = load_manual_holdings()
            persistence_data: dict[str, Any] = {}
            analysis = run_analysis(now=now, stage=stage, persist=False, persistence_data=persistence_data)
            today = now.strftime("%Y%m%d")
            if analysis.get("trade_date") != today:
                print(f"行情不是当日数据（{analysis.get('trade_date')}），可能休市")
                return 0
            state = load_trading_state()
            report = build_scheduled_report(stage, analysis, state, manual_holdings)

            # 构造飞书卡片 2.0 JSON
            market = analysis.get("market", {})
            market_label = market.get("label", "未核验")
            market_summary = market.get("summary", "")
            is_red = market.get("state") == "red"
            header_template = "red" if is_red else ("green" if market.get("state") == "green" else "yellow")

            indices = analysis.get("indices", [])
            breadth = analysis.get("breadth", {})

            def fmt_pct(v):
                if v is None:
                    return "—"
                sign = "+" if v > 0 else ""
                return f"{sign}{v:.2f}%"

            def pct_color(v):
                if v is None or v == 0:
                    return "grey"
                return "green" if v > 0 else "red"

            # 指数指标列（取前4个）
            index_columns = []
            for idx in indices[:4]:
                chg = idx.get("change_pct")
                color = pct_color(chg)
                index_columns.append({
                    "tag": "column", "width": "weighted", "weight": 1,
                    "background_style": f"{color}-50", "padding": "12px", "vertical_spacing": "2px",
                    "elements": [
                        {"tag": "markdown", "content": f"**{idx.get('name','')}**", "text_align": "center"},
                        {"tag": "markdown", "content": f"## <font color='{color}'>{idx.get('price','—')}</font>", "text_align": "center"},
                        {"tag": "markdown", "content": f"<font color='grey'>{fmt_pct(chg)}</font>", "text_align": "center", "text_size": "notation"},
                    ],
                })

            up = breadth.get("up", 0)
            down = breadth.get("down", 0)
            flat = breadth.get("flat", 0)
            avg_chg = breadth.get("average_change_pct")

            breadth_columns = [
                {"tag": "column", "width": "weighted", "weight": 1, "background_style": "grey-50", "padding": "12px", "vertical_spacing": "2px",
                 "elements": [{"tag": "markdown", "content": f"<font color='green'>上涨 {up}</font>", "text_align": "center"}]},
                {"tag": "column", "width": "weighted", "weight": 1, "background_style": "grey-50", "padding": "12px", "vertical_spacing": "2px",
                 "elements": [{"tag": "markdown", "content": f"<font color='red'>下跌 {down}</font>", "text_align": "center"}]},
                {"tag": "column", "width": "weighted", "weight": 1, "background_style": "grey-50", "padding": "12px", "vertical_spacing": "2px",
                 "elements": [{"tag": "markdown", "content": f"<font color='grey'>平盘 {flat}</font>", "text_align": "center"}]},
                {"tag": "column", "width": "weighted", "weight": 1, "background_style": "grey-50", "padding": "12px", "vertical_spacing": "2px",
                 "elements": [{"tag": "markdown", "content": f"**平均 {fmt_pct(avg_chg)}**", "text_align": "center"}]},
            ]

            elements = []
            if index_columns:
                elements.append({
                    "tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
                    "columns": index_columns,
                })
            elements.append({
                "tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
                "columns": breadth_columns,
            })

            # LLM 盘面解读
            try:
                from llm import daily_market_summary
                market_data = {
                    "market": market.get("summary", ""),
                    "indices": [{"name": i.get("name"), "price": i.get("price"),
                                 "change_pct": i.get("change_pct")} for i in indices],
                    "breadth": {"up": up, "down": down, "flat": flat, "avg_change_pct": avg_chg},
                }
                llm_text = daily_market_summary(market_data)
                if llm_text:
                    elements.append({
                        "tag": "markdown",
                        "content": f"**AI盘面解读：** {llm_text}",
                    })
            except Exception:
                pass

            elements.append({
                "tag": "markdown",
                "content": f"<font color='grey'>{market_summary} · 仅供研究参考，不构成个性化投资建议</font>",
                "text_size": "notation",
            })

            card = {
                "schema": "2.0",
                "config": {"update_multi": True, "width_mode": "default"},
                "header": {
                    "title": {"tag": "plain_text", "content": f"A股{report.get('label', '收盘总结')}"},
                    "subtitle": {"tag": "plain_text", "content": f"{analysis.get('trade_date','')} · 数据截至 {analysis.get('data_as_of','')}"},
                    "template": header_template,
                    "icon": {"tag": "standard_icon", "token": "calendar_colorful"},
                    "text_tag_list": [
                        {"tag": "text_tag", "text": {"tag": "plain_text", "content": market_label}, "color": header_template}
                    ],
                },
                "body": {
                    "direction": "vertical", "padding": "12px 12px 20px 12px",
                    "vertical_spacing": "12px", "elements": elements,
                },
            }
            print(json.dumps(card, ensure_ascii=False))
            return 0
        except Exception as error:
            print(f"生成报告失败：{error}")
            return 1
    if args.push_once or args.scheduled_push:
        app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        receive_id = os.environ.get("FEISHU_RECEIVE_ID", "").strip()
        webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
        if not (app_id and app_secret) and not webhook:
            print(json.dumps(
                {"sent": False, "error": "缺少 FEISHU_APP_ID/FEISHU_APP_SECRET 或 FEISHU_WEBHOOK"},
                ensure_ascii=False,
            ))
            return 2
        try:
            stage = args.scheduled_push or "tail-decision"
            result = scheduled_push(
                stage, webhook,
                signing_secret=os.environ.get("FEISHU_SIGNING_SECRET", ""),
                app_id=app_id, app_secret=app_secret, receive_id=receive_id,
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except CorruptLocalData as error:
            print(json.dumps({"sent": False, "error": str(error)}, ensure_ascii=False))
            return 2
        except Exception as error:
            print(json.dumps({"sent": False, "error": str(error)}, ensure_ascii=False))
            return 1
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    scheduler = threading.Thread(target=push_scheduler_loop, name="feishu-push-scheduler", daemon=True)
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

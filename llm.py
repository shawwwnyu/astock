"""DeepSeek LLM 调用封装。"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-flash"


def chat(system_prompt: str, user_prompt: str, temperature: float = 0.3) -> str:
    """调用 DeepSeek chat API，返回回复文本。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return ""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": 2000,
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        content = result["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("模型未返回有效文本")
        return content.strip()
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        raise RuntimeError(f"LLM 调用失败：{type(error).__name__}") from error


def analyze_buy_signal(stock_data: dict) -> str:
    """根据技术指标生成买入理由解读。"""
    sys_prompt = (
        "你是A股短线交易分析师。根据给出的技术指标数据，用100字以内生成一段买入理由，"
        "包括：当前趋势状态、为什么现在买、关键价位（止损/目标）、主要风险。"
        "仅解释提供的数据，明确不确定性，不编造事实，不将信号当成实际成交。"
    )
    user = json.dumps(stock_data, ensure_ascii=False)
    return chat(sys_prompt, user)


def analyze_sell_signal(stock_data: dict) -> str:
    """生成卖出理由。"""
    sys_prompt = (
        "你是A股短线交易分析师。根据给出的技术指标数据，用80字以内生成卖出理由，"
        "解释给定信号与风险，保留不确定性，不将信号当成实际成交。"
    )
    user = json.dumps(stock_data, ensure_ascii=False)
    return chat(sys_prompt, user)


def daily_market_summary(market_data: dict) -> str:
    """收盘盘面解读。"""
    sys_prompt = (
        "你是A股市场分析师。根据给出的指数和自选股数据，用150字以内写一段盘面解读，"
        "包括今天市场整体情况、自选池表现和风险；不编造新闻、驱动因素或确定的明日走势。"
    )
    user = json.dumps(market_data, ensure_ascii=False)
    return chat(sys_prompt, user)


def pick_top_picks(candidates: list[dict], market_context: dict) -> str:
    """让大模型从候选股票中选出 top 推荐。

    candidates: 前10只股票的关键指标（code, name, price, change_pct, trend,
                 macd, rs1, rs3, score, invalidation, resistance, reward_risk,
                 failures, warnings）
    market_context: 大盘状态（红/黄/绿灯）、指数表现
    """
    sys_prompt = (
        "你是A股短线交易专家。以下是自选池中评分最高的10只股票的技术指标数据。"
        "请综合考虑：均线排列、MACD形态、相对强度(RS)、盈亏比、大盘环境，"
        "选出最值得关注的3只股票，每只用一行输出：\n"
        "**股票名称(代码)** — 一句话理由（含关键价位）\n"
        "不要输出多余内容，最多3行。如果大盘红灯，明确提示风险。"
    )
    user = json.dumps({"market": market_context, "stocks": candidates}, ensure_ascii=False)
    return chat(sys_prompt, user, temperature=0.2)


def report_sections(analysis: dict) -> dict[str, str]:
    """可选 AI 解读不参与规则决策；失败保持报告可用并明确提示。"""
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return {}
    market = analysis.get("market", {})
    fields = (
        "code", "name", "price", "change_pct", "score", "trend", "macd", "rs1", "rs3",
        "invalidation", "resistance", "reward_risk", "warnings",
    )
    candidates = [
        {key: stock[key] for key in fields if key in stock}
        for stock in analysis.get("universe", [])
        if stock.get("eligible") and not stock.get("failures")
    ][:10]
    jobs = [("AI盘面解读", lambda: daily_market_summary({
        "market": market, "indices": analysis.get("indices", []), "breadth": analysis.get("breadth", {}),
    }))]
    sections = {}
    for label, generate in jobs:
        try:
            text = generate()
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError("模型未返回有效文本")
            sections[label] = text[:900]
        except RuntimeError as error:
            print(f"[LLM] {label}失败：{error}", file=sys.stderr)
            sections[label] = "暂不可用，本报告保留原始行情和规则结论。"
    return sections

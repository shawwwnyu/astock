"""DeepSeek LLM 调用封装。"""
from __future__ import annotations

import json
import os
import urllib.request

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-flash"


def chat(system_prompt: str, user_prompt: str, temperature: float = 0.3) -> str:
    """调用 DeepSeek chat API，返回回复文本。"""
    if not API_KEY:
        return ""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": 500,
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[LLM] 调用失败: {e}", flush=True)
        return ""


def analyze_buy_signal(stock_data: dict) -> str:
    """根据技术指标生成买入理由解读。"""
    sys_prompt = (
        "你是A股短线交易分析师。根据给出的技术指标数据，用100字以内生成一段买入理由，"
        "包括：当前趋势状态、为什么现在买、关键价位（止损/目标）、主要风险。"
        "语气专业简洁，不要说'建议''可能'等模糊词，直接给判断。"
    )
    user = json.dumps(stock_data, ensure_ascii=False)
    return chat(sys_prompt, user)


def analyze_sell_signal(stock_data: dict) -> str:
    """生成卖出理由。"""
    sys_prompt = (
        "你是A股短线交易分析师。根据给出的技术指标数据，用80字以内生成卖出理由，"
        "说明为什么应该卖出/止损。直接简洁。"
    )
    user = json.dumps(stock_data, ensure_ascii=False)
    return chat(sys_prompt, user)


def daily_market_summary(market_data: dict) -> str:
    """收盘盘面解读。"""
    sys_prompt = (
        "你是A股市场分析师。根据给出的指数和自选股数据，用150字以内写一段盘面解读，"
        "包括：今天市场整体情况、主要驱动因素、自选池表现、对明天的简短展望。"
        "语气专业，不要套话。"
    )
    user = json.dumps(market_data, ensure_ascii=False)
    return chat(sys_prompt, user)

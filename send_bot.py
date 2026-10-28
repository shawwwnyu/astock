#!/usr/bin/env python3
"""以飞书应用 bot 身份发 interactive 卡片到指定群。

用法：
  python3 send_bot.py <chat_id> <card_json_file_or_string>

card_json 可以是文件路径（@card.json）或直接传入 JSON 字符串。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
BASE = "https://open.feishu.cn/open-apis"


def http_post(url: str, data: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_tenant_token() -> str:
    result = http_post(f"{BASE}/auth/v3/tenant_access_token/internal", {
        "app_id": APP_ID,
        "app_secret": APP_SECRET,
    })
    if result.get("code") != 0:
        raise RuntimeError(f"获取 token 失败: {result}")
    return result["tenant_access_token"]


def send_card(chat_id: str, card_json: str, message_uuid: str | None = None) -> dict:
    token = get_tenant_token()
    payload = {"receive_id": chat_id, "msg_type": "interactive", "content": card_json}
    if message_uuid:
        payload["uuid"] = message_uuid
    result = http_post(
        f"{BASE}/im/v1/messages?receive_id_type=chat_id",
        payload,
        token=token,
    )
    if result.get("code") != 0 or not result.get("data", {}).get("message_id"):
        raise RuntimeError(f"飞书发送失败：code={result.get('code')} msg={result.get('msg', '')}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chat_id")
    parser.add_argument("card")
    parser.add_argument("--uuid", help="同一批次重试时保持不变的飞书去重标识")
    args = parser.parse_args()
    try:
        if args.card.startswith("@"):
            with open(args.card[1:], encoding="utf-8") as file:
                card_json = file.read()
        else:
            card_json = args.card
        json.loads(card_json)
        result = send_card(args.chat_id, card_json, args.uuid)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"发送失败: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

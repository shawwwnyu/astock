#!/usr/bin/env python3
"""以飞书应用 bot 身份发 interactive 卡片到指定群。

用法：
  python3 send_bot.py <chat_id> <card_json_file_or_string>

card_json 可以是文件路径（@card.json）或直接传入 JSON 字符串。
"""
from __future__ import annotations

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


def send_card(chat_id: str, card_json: str) -> dict:
    token = get_tenant_token()
    result = http_post(
        f"{BASE}/im/v1/messages?receive_id_type=chat_id",
        {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": card_json,
        },
        token=token,
    )
    return result


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: send_bot.py <chat_id> <card_json_file_or_string>")
        return 1

    chat_id = sys.argv[1]
    card_arg = sys.argv[2]

    if card_arg.startswith("@"):
        with open(card_arg[1:]) as f:
            card_json = f.read()
    else:
        card_json = card_arg

    # 校验 JSON
    json.loads(card_json)

    result = send_card(chat_id, card_json)
    if result.get("code") != 0:
        print(f"发送失败: {json.dumps(result, ensure_ascii=False)}", file=sys.stderr)
        return 1
    print(f"发送成功: message_id={result['data']['message_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

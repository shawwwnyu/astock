#!/usr/bin/env python3
"""以飞书应用 bot 身份发 interactive 卡片到指定群。

用法：
  python3 send_bot.py <chat_id> <card_json_file_or_string>

card_json 可以是文件路径（@card.json）或直接传入 JSON 字符串。
"""
from __future__ import annotations

import argparse
import json
import sys
from feishu_client import send_card


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

"""飞书应用机器人传输层；不依赖行情、策略或命令行入口。"""
from __future__ import annotations

import json
import os
import urllib.request

BASE = "https://open.feishu.cn/open-apis"


def target_chat_id() -> str:
    value = os.environ.get("FEISHU_CHAT_ID", "").strip()
    if not value.startswith("oc_"):
        raise ValueError("请配置 FEISHU_CHAT_ID（机器人所在会话的 oc_ 标识）")
    return value


def http_post(url: str, data: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read())
    if not isinstance(result, dict):
        raise RuntimeError("飞书接口未返回 JSON 对象")
    return result


def get_tenant_token(app_id: str | None = None, app_secret: str | None = None) -> str:
    app_id = (app_id or os.environ.get("FEISHU_APP_ID", "")).strip()
    app_secret = (app_secret or os.environ.get("FEISHU_APP_SECRET", "")).strip()
    if not app_id or not app_secret:
        raise ValueError("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET")
    result = http_post(f"{BASE}/auth/v3/tenant_access_token/internal", {
        "app_id": app_id, "app_secret": app_secret,
    })
    token = result.get("tenant_access_token")
    if result.get("code") != 0 or not isinstance(token, str) or not token:
        raise RuntimeError(f"获取飞书 token 失败: code={result.get('code')}")
    return token


def send_message(
    receive_id: str, msg_type: str, content: str, *, receive_id_type: str = "chat_id",
    message_uuid: str | None = None, app_id: str | None = None, app_secret: str | None = None,
) -> dict:
    if receive_id_type not in {"chat_id", "email"}:
        raise ValueError("不支持的飞书接收标识类型")
    if not receive_id.strip() or (receive_id_type == "chat_id" and not receive_id.startswith("oc_")):
        raise ValueError("接收会话必须为 oc_ 开头的 chat_id")
    payload = {"receive_id": receive_id, "msg_type": msg_type, "content": content}
    if message_uuid:
        payload["uuid"] = message_uuid
    limit = 30_000 if msg_type == "interactive" else 50_000
    if len(json.dumps(payload, ensure_ascii=False).encode()) > limit:
        raise ValueError("飞书消息超过大小限制")
    result = http_post(
        f"{BASE}/im/v1/messages?receive_id_type={receive_id_type}", payload,
        token=get_tenant_token(app_id, app_secret),
    )
    if result.get("code") != 0 or not result.get("data", {}).get("message_id"):
        raise RuntimeError(f"飞书发送失败：code={result.get('code')} msg={result.get('msg', '')}")
    return result


def send_card(
    chat_id: str, card_json: str, message_uuid: str | None = None, *,
    app_id: str | None = None, app_secret: str | None = None,
) -> dict:
    return send_message(
        chat_id, "interactive", card_json, message_uuid=message_uuid,
        app_id=app_id, app_secret=app_secret,
    )

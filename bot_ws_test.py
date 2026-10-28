#!/usr/bin/env python3
"""飞书 bot WebSocket 监听 - 调试版。"""
import sys
import time
from pathlib import Path

LOG = Path("/tmp/bot-debug.log")

def log(msg):
    with open(LOG, "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

log("=== 启动 ===")

try:
    import lark_oapi as lark
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    log("import 成功")
except Exception as e:
    log(f"import 失败: {e}")
    sys.exit(1)

import os
APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")


def do_message(data: P2ImMessageReceiveV1) -> None:
    try:
        msg = data.event.message
        sender = data.event.sender
        log(f"收到消息: chat={msg.chat_id} type={msg.message_type} "
            f"from={sender.sender_id.open_id} content={msg.content}")
    except Exception as e:
        log(f"回调解析错误: {e}")


try:
    log("创建 event_handler...")
    event_handler = EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(do_message).build()
    log("event_handler 创建成功")

    log("启动 WebSocket...")
    cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=event_handler, log_level=lark.LogLevel.INFO)
    log("ws client 启动中...")
    cli.start()
except Exception as e:
    log(f"启动异常: {type(e).__name__}: {e}")
    import traceback
    log(traceback.format_exc())

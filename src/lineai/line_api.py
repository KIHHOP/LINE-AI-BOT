"""
LINE API：Webhook 簽章驗證、回覆訊息、主動推播。

使用 requests + hmac 自行實作，不綁定特定 SDK 版本，
WebUI 修改金鑰後即時生效。
"""

import base64
import hmac
import hashlib

import requests

from .logbuffer import log

REPLY_URL = "https://api.line.me/v2/bot/message/reply"
PUSH_URL = "https://api.line.me/v2/bot/message/push"


def verify_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    """驗證 LINE Webhook 的 X-Line-Signature。"""
    if not channel_secret or not signature:
        return False
    mac = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply(access_token: str, reply_token: str, text: str) -> bool:
    """用 reply token 回覆訊息。"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    payload = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    try:
        resp = requests.post(REPLY_URL, headers=headers, json=payload, timeout=15)
        if resp.status_code != 200:
            log(f"reply 失敗 {resp.status_code}：{resp.text}", "WARNING")
            return False
        return True
    except Exception as e:
        log(f"reply 例外：{e}", "ERROR")
        return False


def push(access_token: str, to: str, text: str) -> bool:
    """用 push 主動推播（reply token 失效時的後備）。"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    payload = {"to": to, "messages": [{"type": "text", "text": text}]}
    try:
        resp = requests.post(PUSH_URL, headers=headers, json=payload, timeout=15)
        if resp.status_code != 200:
            log(f"push 失敗 {resp.status_code}：{resp.text}", "WARNING")
            return False
        return True
    except Exception as e:
        log(f"push 例外：{e}", "ERROR")
        return False

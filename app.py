"""
LINE AI 聊天機器人
流程：LINE 使用者 → LINE Platform → (Ngrok) → 本地 Flask → 本地 Ollama → 回覆 LINE

啟動方式：
    python app.py
"""

import os
import logging

import requests
from dotenv import load_dotenv
from flask import Flask, request, abort

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# ---------------------------------------------------------------------------
# 初始化設定
# ---------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
OLLAMA_SYSTEM_PROMPT = os.getenv("OLLAMA_SYSTEM_PROMPT", "").strip()

PORT = int(os.getenv("PORT", "5000"))

app = Flask(__name__)

# 若金鑰為空，給予安全的預設值避免 SDK 初始化失敗；實際缺值會在啟動時提醒。
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN or "dummy")
handler = WebhookHandler(LINE_CHANNEL_SECRET or "dummy")


# ---------------------------------------------------------------------------
# Ollama 呼叫
# ---------------------------------------------------------------------------
def ask_ollama(user_text: str) -> str:
    """呼叫本地 Ollama 的 /api/chat 端點，回傳模型產生的文字。"""
    url = f"{OLLAMA_BASE_URL}/api/chat"

    messages = []
    if OLLAMA_SYSTEM_PROMPT:
        messages.append({"role": "system", "content": OLLAMA_SYSTEM_PROMPT})
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,  # 一次拿到完整回覆，方便回傳給 LINE
    }

    try:
        # LINE reply token 約 60 秒內要回覆，本地模型推論可能較久，設定合理 timeout
        resp = requests.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        content = data.get("message", {}).get("content", "").strip()
        return content or "（模型沒有產生任何內容）"
    except requests.exceptions.ConnectionError:
        logger.exception("無法連線到 Ollama")
        return "無法連線到本地 Ollama，請確認 Ollama 服務已啟動（ollama serve）。"
    except requests.exceptions.Timeout:
        logger.exception("Ollama 回應逾時")
        return "模型回應逾時，請稍後再試或換用較小的模型。"
    except Exception:
        logger.exception("呼叫 Ollama 發生未預期錯誤")
        return "處理你的訊息時發生錯誤，請稍後再試。"


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def health():
    """健康檢查，方便用瀏覽器或 Ngrok 確認伺服器存活。"""
    return "LINE AI Bot is running.", 200


@app.route("/callback", methods=["POST"])
def callback():
    """LINE Webhook 進入點。"""
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    logger.info("收到 Webhook 請求：%s", body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.warning("簽章驗證失敗，請檢查 LINE_CHANNEL_SECRET 是否正確。")
        abort(400)

    return "OK"


# ---------------------------------------------------------------------------
# 事件處理
# ---------------------------------------------------------------------------
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    """處理使用者傳來的文字訊息。"""
    user_text = event.message.text
    logger.info("使用者訊息：%s", user_text)

    reply_text = ask_ollama(user_text)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)],
                )
            )
        except Exception:
            # reply token 失效（例如模型推論太久）時，改用 push message 補送
            logger.exception("reply_message 失敗，嘗試改用 push_message")
            user_id = getattr(event.source, "user_id", None)
            if user_id:
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=reply_text)],
                    )
                )


if __name__ == "__main__":
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
        logger.warning(
            "尚未設定 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_CHANNEL_SECRET，"
            "Webhook 將無法正常運作。請編輯 .env 填入正確的金鑰。"
        )
    logger.info("啟動 Flask 伺服器，連接埠：%s", PORT)
    logger.info("Ollama 位址：%s，模型：%s", OLLAMA_BASE_URL, OLLAMA_MODEL)
    app.run(host="0.0.0.0", port=PORT)

"""
核心邏輯模組
- 設定（讀寫 .env）
- Ollama 中轉（列出模型、聊天）
- LINE 簽章驗證與訊息回覆 / 推播
- 即時日誌緩衝（供 WebUI 顯示）

本模組不依賴 line-bot-sdk，改用 requests + hmac 自行實作，
好處是 WebUI 修改金鑰後可即時生效，且不被特定 SDK 版本綁死。
"""

import os
import time
import base64
import hmac
import hashlib
import logging
from collections import deque
from threading import Lock

import requests
from dotenv import load_dotenv, set_key

# ---------------------------------------------------------------------------
# 路徑與環境變數
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

# 預設值
DEFAULTS = {
    "LINE_CHANNEL_ACCESS_TOKEN": "",
    "LINE_CHANNEL_SECRET": "",
    "OLLAMA_BASE_URL": "http://localhost:11434",
    "OLLAMA_MODEL": "qwen35:latest",
    "OLLAMA_SYSTEM_PROMPT": "你是一個友善的繁體中文助理，請用繁體中文簡潔回答使用者的問題。",
    "NGROK_AUTHTOKEN": "",
    "PORT": "8080",
}


def ensure_env_file():
    """若 .env 不存在則用預設值建立。"""
    if not os.path.exists(ENV_PATH):
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            for k, v in DEFAULTS.items():
                f.write(f"{k}={v}\n")


def load_settings() -> dict:
    """從 .env 讀取設定，缺項補預設值。"""
    ensure_env_file()
    load_dotenv(ENV_PATH, override=True)
    settings = {}
    for k, default in DEFAULTS.items():
        settings[k] = os.getenv(k, default)
    return settings


def save_settings(values: dict):
    """把設定寫回 .env（逐鍵寫入，保留檔案）。"""
    ensure_env_file()
    for k, v in values.items():
        # set_key 會自動處理引號與更新
        set_key(ENV_PATH, k, str(v if v is not None else ""), quote_mode="never")
    # 重新載入到環境變數
    load_dotenv(ENV_PATH, override=True)


# ---------------------------------------------------------------------------
# 日誌：同時輸出到標準 logging 與一個環形緩衝，供 WebUI 即時顯示
# ---------------------------------------------------------------------------
_log_buffer: "deque[str]" = deque(maxlen=500)
_log_lock = Lock()

logger = logging.getLogger("line_ai_bot")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_h)


def log(message: str, level: str = "INFO"):
    """寫入日誌（同時進緩衝區）。"""
    ts = time.strftime("%H:%M:%S")
    line = f"{ts} [{level}] {message}"
    with _log_lock:
        _log_buffer.append(line)
    getattr(logger, level.lower(), logger.info)(message)


def get_logs() -> list:
    with _log_lock:
        return list(_log_buffer)


def clear_logs():
    with _log_lock:
        _log_buffer.clear()


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
def list_ollama_models(base_url: str) -> list:
    """列出本地 Ollama 已下載的模型名稱。"""
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return [m["name"] for m in data.get("models", [])]
    except Exception as e:
        log(f"無法取得 Ollama 模型清單：{e}", "WARNING")
        return []


def check_ollama(base_url: str) -> bool:
    """檢查 Ollama 服務是否存活。"""
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def ask_ollama(user_text: str, settings: dict) -> str:
    """呼叫本地 Ollama /api/chat，回傳模型文字。"""
    base_url = settings.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = settings.get("OLLAMA_MODEL", "qwen35:latest")
    system_prompt = (settings.get("OLLAMA_SYSTEM_PROMPT") or "").strip()

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_text})

    payload = {"model": model, "messages": messages, "stream": False}

    try:
        resp = requests.post(f"{base_url}/api/chat", json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        content = data.get("message", {}).get("content", "").strip()
        return content or "（模型沒有產生任何內容）"
    except requests.exceptions.ConnectionError:
        log("無法連線到 Ollama", "ERROR")
        return "無法連線到本地 Ollama，請確認 Ollama 服務已啟動。"
    except requests.exceptions.Timeout:
        log("Ollama 回應逾時", "ERROR")
        return "模型回應逾時，請稍後再試或換用較小的模型。"
    except Exception as e:
        log(f"呼叫 Ollama 發生錯誤：{e}", "ERROR")
        return "處理你的訊息時發生錯誤，請稍後再試。"


# ---------------------------------------------------------------------------
# LINE
# ---------------------------------------------------------------------------
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def verify_line_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    """驗證 LINE Webhook 的 X-Line-Signature。"""
    if not channel_secret or not signature:
        return False
    mac = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def line_reply(access_token: str, reply_token: str, text: str) -> bool:
    """用 reply token 回覆訊息。"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    payload = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    try:
        resp = requests.post(LINE_REPLY_URL, headers=headers, json=payload, timeout=15)
        if resp.status_code != 200:
            log(f"reply 失敗 {resp.status_code}：{resp.text}", "WARNING")
            return False
        return True
    except Exception as e:
        log(f"reply 例外：{e}", "ERROR")
        return False


def line_push(access_token: str, to: str, text: str) -> bool:
    """用 push 主動推播（reply token 失效時的後備）。"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    payload = {"to": to, "messages": [{"type": "text", "text": text}]}
    try:
        resp = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=15)
        if resp.status_code != 200:
            log(f"push 失敗 {resp.status_code}：{resp.text}", "WARNING")
            return False
        return True
    except Exception as e:
        log(f"push 例外：{e}", "ERROR")
        return False

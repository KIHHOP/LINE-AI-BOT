"""
Ollama 中轉：列出本地模型、檢查服務狀態、呼叫聊天端點。
"""

import requests

from .logbuffer import log


def list_models(base_url: str) -> list:
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


def check(base_url: str) -> bool:
    """檢查 Ollama 服務是否存活。"""
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def ask(user_text: str, settings: dict) -> str:
    """呼叫本地 Ollama /api/chat，回傳模型產生的文字。"""
    base_url = settings.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = settings.get("OLLAMA_MODEL", "qwen3")
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

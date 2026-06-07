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


def _chat(base_url: str, model: str, messages: list, timeout: int = 120,
          fmt: str = "") -> str:
    """共用的 /api/chat 呼叫，回傳模型文字（含錯誤處理）。

    fmt="json" 時要求 Ollama 以 JSON 物件輸出（structured output）。
    """
    payload = {"model": model, "messages": messages, "stream": False}
    if fmt:
        payload["format"] = fmt
    resp = requests.post(f"{base_url}/api/chat", json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return (data.get("message", {}).get("content", "") or "").strip()


def run_model(settings: dict, model: str, system_prompt: str, user_content: str,
              as_json: bool = False, timeout: int = 120) -> str:
    """以指定模型跑一次單輪對話（供多層流水線各層使用）。

    model 留空時回退用設定中的 OLLAMA_MODEL。as_json=True 會要求 JSON 輸出。
    回傳模型文字；發生錯誤時回傳空字串，由呼叫端決定後備行為。
    """
    base_url = settings.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = (model or "").strip() or settings.get("OLLAMA_MODEL", "qwen3")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_content})
    try:
        return _chat(base_url, model, messages, timeout=timeout,
                     fmt="json" if as_json else "")
    except Exception as e:
        log(f"模型 {model} 呼叫失敗：{e}", "ERROR")
        return ""


def ask(user_text: str, settings: dict, memory_summary: str = "") -> str:
    """呼叫本地 Ollama /api/chat，回傳模型產生的文字。

    memory_summary：該客戶過去對話的精華，會以系統訊息注入，達成跨對話記憶。
    """
    base_url = settings.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = settings.get("OLLAMA_MODEL", "qwen3")
    system_prompt = (settings.get("OLLAMA_SYSTEM_PROMPT") or "").strip()

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if memory_summary:
        messages.append({
            "role": "system",
            "content": "以下是你與這位顧客過去對話的重點摘要，請在回覆時參考，"
                       "但不要直接複述：\n" + memory_summary,
        })
    messages.append({"role": "user", "content": user_text})

    try:
        content = _chat(base_url, model, messages, timeout=120)
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


def summarize_conversation(
    settings: dict, history: list, previous_summary: str = ""
) -> str:
    """把一段對話（含先前精華）壓縮成新的重點摘要，供長期記憶使用。

    history：[{"role": "user"/"assistant", "content": ...}, ...]
    回傳壓縮後的精華文字；失敗時回傳原本的 previous_summary（不覆蓋既有記憶）。
    """
    base_url = settings.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = settings.get("OLLAMA_MODEL", "qwen3")

    convo_text = "\n".join(
        f"{'顧客' if m.get('role') == 'user' else '客服'}：{m.get('content', '')}"
        for m in history
    ).strip()
    if not convo_text:
        return previous_summary

    instruction = (
        "你是對話記憶整理員。請把【先前摘要】與【最新對話】合併，"
        "濃縮成精簡的繁體中文重點筆記，保留：顧客偏好、需求、詢問過的零件/價格、"
        "已成立或待辦的事項、稱呼與語氣偏好。去除寒暄與重複內容，"
        "以條列式輸出，總長度控制在 300 字內。"
    )
    user_block = (
        f"【先前摘要】\n{previous_summary or '（無）'}\n\n"
        f"【最新對話】\n{convo_text}\n\n請輸出更新後的重點筆記："
    )
    messages = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": user_block},
    ]
    try:
        content = _chat(base_url, model, messages, timeout=120)
        return content or previous_summary
    except Exception as e:
        log(f"產生對話精華失敗：{e}", "WARNING")
        return previous_summary

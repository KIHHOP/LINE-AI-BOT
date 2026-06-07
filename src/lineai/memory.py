"""
對話記憶：把每位客戶過去對話的「壓縮精華」存成檔案，
每次對談前用 LINE userId 取回，注入模型上下文，達成跨對話記憶。

- 儲存位置：MEMORY_DIR（預設專案根目錄下的 memory/）。
- 檔名：以 userId 經 sha256 雜湊，避免特殊字元與隱私外洩。
- 內容：JSON（summary 精華文字 + updated_at + turns 累計輪數）。
"""

import os
import json
import time
import hashlib

from .config import PROJECT_ROOT
from .logbuffer import log


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def is_enabled(settings: dict) -> bool:
    return _truthy(settings.get("MEMORY_ENABLED", "true"))


def memory_dir(settings: dict) -> str:
    """記憶資料夾的絕對路徑（不存在時建立）。"""
    raw = (settings.get("MEMORY_DIR") or "memory").strip()
    path = raw if os.path.isabs(raw) else os.path.join(PROJECT_ROOT, raw)
    os.makedirs(path, exist_ok=True)
    return path


def _file_for(settings: dict, user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
    return os.path.join(memory_dir(settings), f"{digest}.json")


def load_summary(settings: dict, user_id: str) -> str:
    """取回某客戶的對話精華文字；沒有則回空字串。"""
    if not is_enabled(settings) or not user_id:
        return ""
    path = _file_for(settings, user_id)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (data.get("summary") or "").strip()
    except Exception as e:
        log(f"讀取對話精華失敗：{e}", "WARNING")
        return ""


def save_summary(settings: dict, user_id: str, summary: str, turns: int = 0) -> bool:
    """寫入/覆蓋某客戶的對話精華。"""
    if not is_enabled(settings) or not user_id:
        return False
    summary = (summary or "").strip()
    if not summary:
        return False
    path = _file_for(settings, user_id)
    data = {
        "user_id_hash": os.path.splitext(os.path.basename(path))[0],
        "summary": summary,
        "turns": turns,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log(f"寫入對話精華失敗：{e}", "WARNING")
        return False


def meta(settings: dict, user_id: str) -> dict:
    """回傳精華的中繼資訊（turns / updated_at）。"""
    if not user_id:
        return {}
    path = _file_for(settings, user_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

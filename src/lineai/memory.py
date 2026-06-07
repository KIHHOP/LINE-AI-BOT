"""
長期記憶：每位客戶擁有一個『專屬資料夾』，以客戶 ID 雜湊後的代碼命名，
資料夾內存放該客戶的對話精華（summary.json）。

設計：
- 儲存根目錄：MEMORY_DIR（預設專案根目錄下的 memory/）。
- 每位客戶 → 一個子資料夾，名稱為 userId 經 sha256 雜湊的代碼，
  避免特殊字元與隱私外洩，也方便日後在同一資料夾擴充其他長期資料。
- 對話精華檔：<客戶資料夾>/summary.json（summary 文字 + updated_at + turns）。
- 向後相容：若還存在舊版單檔 <code>.json（直接放在 MEMORY_DIR 下），
  讀取時自動沿用、寫入時自動遷移到新的專屬資料夾。
"""

import os
import json
import time
import shutil
import hashlib

from .config import PROJECT_ROOT
from .logbuffer import log

SUMMARY_FILENAME = "summary.json"


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def is_enabled(settings: dict) -> bool:
    return _truthy(settings.get("MEMORY_ENABLED", "true"))


def memory_root(settings: dict) -> str:
    """記憶總根目錄的絕對路徑（不存在時建立）。"""
    raw = (settings.get("MEMORY_DIR") or "memory").strip()
    path = raw if os.path.isabs(raw) else os.path.join(PROJECT_ROOT, raw)
    os.makedirs(path, exist_ok=True)
    return path


def customer_code(user_id: str) -> str:
    """客戶 ID 的代碼（sha256 前 32 碼），作為專屬資料夾辨識名。"""
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]


def customer_dir(settings: dict, user_id: str, create: bool = True) -> str:
    """取得某客戶的專屬資料夾路徑（以客戶 ID 代碼命名）。"""
    path = os.path.join(memory_root(settings), customer_code(user_id))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _summary_file(settings: dict, user_id: str, create_dir: bool = True) -> str:
    return os.path.join(customer_dir(settings, user_id, create=create_dir),
                        SUMMARY_FILENAME)


def _legacy_file(settings: dict, user_id: str) -> str:
    """舊版單檔路徑：MEMORY_DIR/<code>.json（用於向後相容讀取與遷移）。"""
    return os.path.join(memory_root(settings), f"{customer_code(user_id)}.json")


def _read_summary_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"讀取長期記憶失敗：{e}", "WARNING")
        return {}


def _migrate_legacy_if_needed(settings: dict, user_id: str) -> None:
    """若仍是舊版單檔、且尚未有新版專屬資料夾，將其遷移過去。"""
    legacy = _legacy_file(settings, user_id)
    if not os.path.exists(legacy):
        return
    new_path = _summary_file(settings, user_id, create_dir=True)
    if os.path.exists(new_path):
        return
    try:
        shutil.move(legacy, new_path)
        log(f"長期記憶已遷移到專屬資料夾：{customer_code(user_id)}/")
    except Exception as e:
        log(f"遷移舊版長期記憶失敗：{e}", "WARNING")


def load_summary(settings: dict, user_id: str) -> str:
    """取回某客戶的對話精華文字；沒有則回空字串。"""
    if not is_enabled(settings) or not user_id:
        return ""
    _migrate_legacy_if_needed(settings, user_id)
    path = _summary_file(settings, user_id, create_dir=False)
    if os.path.exists(path):
        return (_read_summary_json(path).get("summary") or "").strip()
    # 後備：仍讀得到舊版單檔（遷移失敗時不影響取用）
    legacy = _legacy_file(settings, user_id)
    if os.path.exists(legacy):
        return (_read_summary_json(legacy).get("summary") or "").strip()
    return ""


def save_summary(settings: dict, user_id: str, summary: str, turns: int = 0) -> bool:
    """寫入/覆蓋某客戶的對話精華（存進其專屬資料夾）。"""
    if not is_enabled(settings) or not user_id:
        return False
    summary = (summary or "").strip()
    if not summary:
        return False
    path = _summary_file(settings, user_id, create_dir=True)
    data = {
        "customer_code": customer_code(user_id),
        "summary": summary,
        "turns": turns,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log(f"寫入長期記憶失敗：{e}", "WARNING")
        return False


def meta(settings: dict, user_id: str) -> dict:
    """回傳精華的中繼資訊（turns / updated_at）。"""
    if not user_id:
        return {}
    _migrate_legacy_if_needed(settings, user_id)
    path = _summary_file(settings, user_id, create_dir=False)
    if os.path.exists(path):
        return _read_summary_json(path)
    legacy = _legacy_file(settings, user_id)
    if os.path.exists(legacy):
        return _read_summary_json(legacy)
    return {}

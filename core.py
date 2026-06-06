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
import shutil
import subprocess
import threading
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
    # Cloudflare Tunnel 設定
    "PUBLIC_DOMAIN": "bot.linebotnanocat.com",   # 對外固定網域（DNS 已指到 tunnel）
    "CF_TUNNEL_NAME": "linebot",                  # cloudflared tunnel 的具名名稱
    "CLOUDFLARED_PATH": "cloudflared",            # cloudflared 執行檔路徑（在 PATH 上時填名稱即可）
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


# ---------------------------------------------------------------------------
# Cloudflare Tunnel（cloudflared 子行程）
# ---------------------------------------------------------------------------
# 以單一全域子行程管理，WebUI 可一鍵啟停。cloudflared 的輸出會被讀進日誌緩衝。
_cf_proc: "subprocess.Popen | None" = None
_cf_lock = Lock()


def _candidate_cloudflared_paths() -> list:
    """列出 Windows 上 cloudflared 可能的安裝位置（winget / 官方安裝包）。"""
    candidates = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        # winget 安裝預設會放在 WinGet\Packages\Cloudflare.cloudflared_* 下
        wg = os.path.join(local, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(wg):
            try:
                for name in os.listdir(wg):
                    if name.lower().startswith("cloudflare.cloudflared"):
                        candidates.append(os.path.join(wg, name, "cloudflared.exe"))
            except Exception:
                pass
        # winget 也可能放一份在 WinGet\Links
        candidates.append(os.path.join(local, "Microsoft", "WinGet", "Links", "cloudflared.exe"))
    for root in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        if root:
            candidates.append(os.path.join(root, "cloudflared", "cloudflared.exe"))
            candidates.append(os.path.join(root, "Cloudflare", "Cloudflared", "cloudflared.exe"))
    return candidates


def resolve_cloudflared(settings: dict) -> str:
    """解析出可用的 cloudflared 執行檔路徑；找不到回傳空字串。

    解析順序：
    1. 設定值（若為實際檔案路徑且存在 / 或為可在 PATH 找到的名稱）
    2. 系統 PATH（shutil.which）
    3. Windows winget / Program Files 常見安裝位置
    """
    configured = (settings.get("CLOUDFLARED_PATH") or "").strip()
    if configured:
        # 指定的是檔案路徑
        if os.path.sep in configured or configured.lower().endswith(".exe"):
            if os.path.isfile(configured):
                return configured
        else:
            found = shutil.which(configured)
            if found:
                return found

    # 預設名稱在 PATH 上
    found = shutil.which("cloudflared")
    if found:
        return found

    # 掃描常見安裝位置（解決「已安裝但 WebUI 啟動時 PATH 尚未更新」的情況）
    for cand in _candidate_cloudflared_paths():
        if os.path.isfile(cand):
            return cand

    return ""


def cloudflared_path(settings: dict) -> str:
    """取得 cloudflared 執行檔路徑（解析失敗時退回設定值或預設名稱）。"""
    resolved = resolve_cloudflared(settings)
    if resolved:
        return resolved
    return (settings.get("CLOUDFLARED_PATH") or "cloudflared").strip() or "cloudflared"


def is_cloudflared_installed(settings: dict) -> bool:
    """檢查 cloudflared 是否可被找到（設定值 / PATH / 常見安裝位置）。"""
    return bool(resolve_cloudflared(settings))


def public_url(settings: dict) -> str:
    """對外固定網址（https://<domain>）。"""
    domain = (settings.get("PUBLIC_DOMAIN") or "").strip().rstrip("/")
    if not domain:
        return ""
    if domain.startswith("http://") or domain.startswith("https://"):
        return domain
    return f"https://{domain}"


def webhook_url(settings: dict) -> str:
    """LINE 要填的 Webhook URL（結尾 /callback）。"""
    base = public_url(settings)
    return f"{base}/callback" if base else ""


def _pump_cloudflared_output(proc: "subprocess.Popen"):
    """背景執行緒：把 cloudflared 的輸出逐行讀進日誌緩衝。"""
    try:
        for raw in iter(proc.stdout.readline, ""):
            if not raw:
                break
            line = raw.rstrip("\n")
            if line:
                log(f"[cloudflared] {line}")
    except Exception:
        pass


def is_tunnel_running() -> bool:
    """目前是否有存活的 cloudflared 子行程。"""
    with _cf_lock:
        return _cf_proc is not None and _cf_proc.poll() is None


def start_cloudflared(settings: dict) -> tuple[bool, str]:
    """啟動 cloudflared tunnel run <name>。回傳 (是否成功, 訊息)。"""
    global _cf_proc
    with _cf_lock:
        if _cf_proc is not None and _cf_proc.poll() is None:
            return False, "Cloudflare Tunnel 已在執行"

        resolved = resolve_cloudflared(settings)
        if not resolved:
            return False, (
                "找不到 cloudflared 執行檔。已嘗試：設定值、系統 PATH、"
                "以及 winget/Program Files 常見安裝位置。請確認已安裝，"
                "或在設定的「cloudflared 執行檔路徑」直接填入完整路徑"
                "（例如 C:\\Program Files\\cloudflared\\cloudflared.exe）。"
            )
        log(f"使用 cloudflared：{resolved}")

        tunnel_name = (settings.get("CF_TUNNEL_NAME") or "").strip()
        if not tunnel_name:
            return False, "尚未設定 Tunnel 名稱（CF_TUNNEL_NAME）"

        path = cloudflared_path(settings)
        port = int(settings.get("PORT", "8080") or "8080")
        # 用 --url 直接指定本機服務埠，免去依賴 config.yml 的 ingress 設定。
        cmd = [path, "tunnel", "--url", f"http://localhost:{port}", "run", tunnel_name]
        try:
            # Windows 下隱藏額外的命令視窗
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            _cf_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except Exception as e:
            _cf_proc = None
            return False, f"啟動 cloudflared 失敗：{e}"

        threading.Thread(
            target=_pump_cloudflared_output, args=(_cf_proc,), daemon=True
        ).start()

    log(f"Cloudflare Tunnel 已啟動：{' '.join(cmd)}")
    return True, "Cloudflare Tunnel 已啟動"


def stop_cloudflared() -> tuple[bool, str]:
    """停止 cloudflared 子行程。回傳 (是否成功, 訊息)。"""
    global _cf_proc
    with _cf_lock:
        if _cf_proc is None or _cf_proc.poll() is not None:
            _cf_proc = None
            return False, "Cloudflare Tunnel 尚未啟動"
        proc = _cf_proc
        _cf_proc = None

    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception as e:
        log(f"停止 cloudflared 時發生例外：{e}", "WARNING")
        return False, f"停止時發生例外：{e}"

    log("Cloudflare Tunnel 已停止")
    return True, "Cloudflare Tunnel 已停止"

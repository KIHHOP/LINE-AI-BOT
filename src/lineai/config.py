"""
設定管理：集中定義所有設定鍵、預設值與讀寫邏輯。

設定來源為專案根目錄的 .env，WebUI 修改後即時寫回並重新載入。
敏感值（金鑰、密碼）不應提交到版本庫；.env 已列入 .gitignore。
"""

import os
import secrets
from dataclasses import dataclass

from dotenv import load_dotenv, set_key

# ---------------------------------------------------------------------------
# 路徑
# ---------------------------------------------------------------------------
# 專案根目錄 = 本檔案往上三層（src/lineai/config.py -> 專案根）
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(PACKAGE_DIR))
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")


# ---------------------------------------------------------------------------
# 設定鍵定義
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Setting:
    key: str
    default: str
    secret: bool = False   # 是否為敏感值（UI 以密碼欄顯示、日誌不外洩）
    desc: str = ""


SETTINGS: tuple = (
    # LINE
    Setting("LINE_CHANNEL_ACCESS_TOKEN", "", secret=True,
            desc="LINE Messaging API 的 Channel Access Token"),
    Setting("LINE_CHANNEL_SECRET", "", secret=True,
            desc="LINE Messaging API 的 Channel Secret"),
    # Ollama
    Setting("OLLAMA_BASE_URL", "http://localhost:11434",
            desc="本地 Ollama API 位址"),
    Setting("OLLAMA_MODEL", "qwen3",
            desc="使用的模型名稱（需先 ollama pull）"),
    Setting("OLLAMA_SYSTEM_PROMPT",
            "你是一個友善的繁體中文助理，請用繁體中文簡潔回答使用者的問題。",
            desc="系統提示詞，設定機器人個性與回答風格"),
    # Cloudflare Tunnel
    Setting("PUBLIC_DOMAIN", "",
            desc="對外固定網域，例如 bot.example.com"),
    Setting("CF_TUNNEL_NAME", "linebot",
            desc="cloudflared 具名 tunnel 的名稱"),
    Setting("CLOUDFLARED_PATH", "cloudflared",
            desc="cloudflared 執行檔路徑；在 PATH 上時填 cloudflared 即可"),
    # WebUI 安全性
    Setting("WEBUI_HOST", "127.0.0.1",
            desc="WebUI 綁定位址。預設只綁本機；要讓區網存取才改 0.0.0.0"),
    Setting("WEBUI_PORT", "8080",
            desc="WebUI 與 LINE Webhook 共用的服務埠"),
    Setting("WEBUI_PASSWORD", "", secret=True,
            desc="WebUI 登入密碼；留空時啟動會自動產生並印在終端機"),
    Setting("WEBUI_SECRET_KEY", "", secret=True,
            desc="登入 session 加密金鑰；留空時自動產生並寫回 .env"),
)

DEFAULTS = {s.key: s.default for s in SETTINGS}
SECRET_KEYS = {s.key for s in SETTINGS if s.secret}


# ---------------------------------------------------------------------------
# 讀寫
# ---------------------------------------------------------------------------
def ensure_env_file() -> None:
    """若 .env 不存在則用預設值建立。"""
    if not os.path.exists(ENV_PATH):
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write("# LINE AI Bot 設定檔（請勿提交到版本庫）\n")
            for s in SETTINGS:
                f.write(f"{s.key}={s.default}\n")


def load_settings() -> dict:
    """從 .env 讀取設定，缺項補預設值，並確保安全金鑰已產生。"""
    ensure_env_file()
    load_dotenv(ENV_PATH, override=True)
    settings = {s.key: os.getenv(s.key, s.default) for s in SETTINGS}

    # session 加密金鑰：缺少則自動產生並持久化，避免每次重啟登出所有人
    if not settings.get("WEBUI_SECRET_KEY"):
        settings["WEBUI_SECRET_KEY"] = secrets.token_urlsafe(32)
        set_key(ENV_PATH, "WEBUI_SECRET_KEY", settings["WEBUI_SECRET_KEY"],
                quote_mode="never")

    return settings


def save_settings(values: dict) -> None:
    """把設定逐鍵寫回 .env 並重新載入到環境變數。"""
    ensure_env_file()
    for k, v in values.items():
        set_key(ENV_PATH, k, str(v if v is not None else ""), quote_mode="never")
    load_dotenv(ENV_PATH, override=True)


def public_url(settings: dict) -> str:
    """對外固定網址（https://<domain>），未設定網域時回空字串。"""
    domain = (settings.get("PUBLIC_DOMAIN") or "").strip().rstrip("/")
    if not domain:
        return ""
    if domain.startswith(("http://", "https://")):
        return domain
    return f"https://{domain}"


def webhook_url(settings: dict) -> str:
    """LINE 要填的 Webhook URL（結尾 /callback）。"""
    base = public_url(settings)
    return f"{base}/callback" if base else ""

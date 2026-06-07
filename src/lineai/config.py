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
    Setting("LINE_REQUIRE_FRIEND", "true",
            desc="是否要求先加官方帳號好友才回覆（true/false）"),
    Setting("LINE_NOT_FRIEND_MESSAGE",
            "請先將我們加為好友後再傳訊息，謝謝！",
            desc="尚未加好友時自動回覆的訊息"),
    # Ollama
    Setting("OLLAMA_BASE_URL", "http://localhost:11434",
            desc="本地 Ollama API 位址"),
    Setting("OLLAMA_MODEL", "qwen3",
            desc="使用的模型名稱（需先 ollama pull）"),
    Setting("OLLAMA_SYSTEM_PROMPT",
            "你是一個友善的繁體中文助理，請用繁體中文簡潔回答使用者的問題。",
            desc="系統提示詞，設定機器人個性與回答風格"),
    # 對話記憶
    Setting("MEMORY_ENABLED", "true",
            desc="是否啟用跨對話記憶（將過去對話壓縮成精華，true/false）"),
    Setting("MEMORY_DIR", "memory",
            desc="對話精華儲存資料夾（相對於專案根目錄或絕對路徑）"),
    Setting("MEMORY_UPDATE_EVERY", "1",
            desc="每幾輪對話更新一次精華（1 表示每次都更新）"),
    # 兩層 AI 流水線（銷售→店長審查；庫存優先、缺貨才報原價屋）
    Setting("PIPELINE_ENABLED", "false",
            desc="是否啟用兩層 AI 流水線（false 時用單層回覆，true/false）"),
    Setting("PIPELINE_L1_MODEL", "",
            desc="第1層 L1 模型（銷售/客服/聊天/詢問共用；留空用主模型）"),
    Setting("PIPELINE_L1_CHAT_PROMPT",
            "你是 3C 電腦零件門市的『前線 AI』，同時扮演客服、聊天與一般詢問的角色，"
            "講話像台灣 LINE 客服一樣親切、口語、有人味，會適時用語助詞（喔、囉、唷）與 emoji。\n"
            "現在這則訊息不是在問特定商品的價格或庫存，請以親切的客服／閒聊方式回應。\n"
            "鐵則：你『沒有』查詢資料庫，因此絕對不可以自己講出任何商品的價格、庫存、"
            "是否現貨或到貨天數。若顧客想知道價格或貨況，請禮貌請對方提供完整型號，"
            "由系統查詢後再報，切勿臆測或捏造。可參考【顧客過去重點】維持上下文連貫。",
            desc="第1層 L1 一般對話（客服/聊天/詢問）系統提示詞（不查資料庫、不臆測價格貨況）"),
    Setting("PIPELINE_L1_PROMPT",
            "你是 3C 電腦零件門市的『金牌銷售』，講話像台灣 LINE 客服一樣親切、口語、"
            "有人味，會適時用語助詞（喔、囉、唷）與 emoji，但專業不浮誇。\n"
            "系統已用程式查好【貨況表】——有庫存就優先賣自家庫存（source=local），"
            "沒庫存才報原價屋的調貨價（source=coolpc）。請依貨況表的事實寫一段報價回覆：\n"
            "1) source=local（本店現貨）：強調『店內現貨、今天下單今天就能寄出/自取』，"
            "報未稅單價並積極引導下單。\n"
            "2) source=coolpc（需向原價屋調貨）：誠實說明需調貨、附預估天數與未稅參考價，"
            "語氣仍積極協助。\n"
            "3) source=none（查無資料）：禮貌說查不到，請顧客再確認型號。\n"
            "鐵則：所有價格一律為『未稅價』，且只能使用貨況表提供的數字，"
            "價格、品名、品牌、庫存絕不可捏造或臆測。直接輸出要回覆顧客的話，不要加 JSON。",
            desc="第1層 銷售 系統提示詞（依貨況表寫報價；庫存優先、缺貨報原價屋；報價皆未稅、不臆測）"),
    Setting("PIPELINE_L2_MODEL", "",
            desc="第2層 複查 模型（銷售/一般複查共用；留空用主模型）"),
    Setting("PIPELINE_L2_PROMPT",
            "你是最嚴苛的『店長』，是回覆送出前的最後一道防線。銷售已產出報價草稿，"
            "你要嚴格核對，通過才能送出：\n"
            "1) 價格是否竄改：草稿中的價格必須與『貨況表』提供的未稅價完全一致，"
            "不可被加價、減價或捏造；所有價格一律為『未稅價』。\n"
            "2) 來源與話術是否矛盾：source=local 才可說『現貨／可立即出貨』；"
            "source=coolpc 必須說明『需調貨』並附預估天數，不可謊稱現貨；"
            "source=none 不可硬報價。\n"
            "3) 有無臆測：不可猜測或捏造價格、貨況、規格，一切以貨況表事實為準。\n"
            "發現任何竄改、捏造或來源矛盾就直接改正；其餘（尤其口語化語氣）盡量保留"
            "銷售的人味，不要改得生硬。最後只輸出要傳給顧客的最終訊息，不要附加說明。",
            desc="第2層 店長 系統提示詞（核對價格未竄改、來源不矛盾、無臆測）"),
    Setting("PIPELINE_L2_CHAT_PROMPT",
            "你是『複查員』，負責在一般客服／聊天／詢問回覆送出前做最後把關。\n"
            "L1 已產出一段非報價的回覆草稿，請檢核並在必要時修正：\n"
            "1) 語氣是否親切得體、符合門市立場；\n"
            "2) 是否捏造了商品、價格、庫存、是否現貨或到貨天數，或做出無法兌現的承諾"
            "（這類資訊一律要請顧客提供型號、由系統查詢後再答，草稿中不可自行臆測）；\n"
            "3) 是否答非所問或偏離顧客的問題。\n"
            "通過則原樣輸出，需修正則改正後輸出。最後只輸出要傳給顧客的最終訊息，"
            "保留口語化人味，不要附加任何說明。\n"
            "（留空此提示詞代表停用一般稿複查，L1 的一般回覆會直接送出。）",
            desc="第2層 一般複查 系統提示詞（語氣得體、不捏造商品/價格/庫存/承諾；留空則停用）"),
    # 原價屋報價
    Setting("COOLPC_ENABLED", "true",
            desc="是否在缺貨時查原價屋報價（true/false）"),
    Setting("COOLPC_URL", "https://www.coolpc.com.tw/evaluate.php",
            desc="原價屋報價頁網址"),
    Setting("RESTOCK_LEAD_TIME", "3-7",
            desc="本店缺貨改走調貨時，告知顧客的預估天數（例如 3-7）"),
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
    # SQL Server
    Setting("SQLSERVER_ENABLED", "false",
            desc="是否啟用 SQL Server 連線（true/false）"),
    Setting("SQLSERVER_HOST", "localhost",
            desc="SQL Server 主機位址"),
    Setting("SQLSERVER_PORT", "1433",
            desc="SQL Server 連接埠"),
    Setting("SQLSERVER_DATABASE", "",
            desc="資料庫名稱"),
    Setting("SQLSERVER_USER", "",
            desc="登入帳號（使用 SQL 驗證時填寫）"),
    Setting("SQLSERVER_PASSWORD", "", secret=True,
            desc="登入密碼"),
    Setting("SQLSERVER_DRIVER", "ODBC Driver 17 for SQL Server",
            desc="ODBC 驅動名稱，例如 ODBC Driver 17/18 for SQL Server"),
    Setting("SQLSERVER_ENCRYPT", "false",
            desc="是否加密連線（Driver 18 預設要求，true/false）"),
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

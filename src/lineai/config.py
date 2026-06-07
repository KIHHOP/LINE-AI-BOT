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
    # 四層 AI 流水線（語言理解→庫管→銷售→店長審查）
    Setting("PIPELINE_ENABLED", "false",
            desc="是否啟用四層 AI 流水線（false 時用單層回覆，true/false）"),
    Setting("PIPELINE_L1_MODEL", "",
            desc="第1層 語言理解大師 模型（留空用主模型）"),
    Setting("PIPELINE_L1_PROMPT",
            "你是語言理解大師（電腦零件門市客服）。請從顧客訊息中萃取意圖與關鍵資訊，"
            "輸出 JSON：{\"intent\":\"詢價|查貨|閒聊|其他\","
            "\"items\":[{\"keyword\":\"產品關鍵字\",\"category\":\"類別\",\"brand\":\"品牌\",\"qty\":數量或null}],"
            "\"budget\":預算數字或null,\"notes\":\"其他需求\"}。"
            "category 請填產品類別，例如：顯示卡、處理器、主機板、記憶體、硬碟、"
            "電源、機殼、螢幕、筆電、桌機；若顧客只給型號（如 RTX5060）無法確定是"
            "單買零件或整台電腦，category 就留空字串（交由後續流程向顧客釐清）。"
            "brand 請填顧客明確指定的品牌（如 MSI、華碩、技嘉…），沒指定就留空字串。"
            "你只負責理解與擷取，不要自行臆測或補上顧客沒說的類別/品牌。"
            "只輸出 JSON，不要多餘文字。",
            desc="第1層 系統提示詞（輸出小抄 JSON，含類別與品牌擷取）"),
    Setting("PIPELINE_L2_MODEL", "",
            desc="第2層 最強庫管 模型（留空用主模型）"),
    Setting("PIPELINE_L2_PROMPT",
            "你是最強庫管。根據提供的『庫存查詢結果』與『原價屋報價結果』，"
            "彙整每項零件的貨況。輸出 JSON：{\"lines\":[{\"keyword\":\"\",\"in_stock\":true/false,"
            "\"best_price\":數字或null,\"source\":\"sql|coolpc|none\",\"options\":[...]}]}。"
            "只輸出 JSON。",
            desc="第2層 系統提示詞（彙整貨況 JSON）"),
    Setting("PIPELINE_L3_MODEL", "",
            desc="第3層 金牌銷售 模型（留空用主模型）"),
    Setting("PIPELINE_L3_PROMPT",
            "你是金牌銷售（電腦零件門市客服）。你要先判斷『資訊是否足夠報價』，再決定回應方式，"
            "輸出 JSON：{\"action\":\"ask|quote\",\"message\":\"要回覆顧客的話\"}。\n"
            "判斷規則（資訊不足就反問，一次只問一件事，循序漸進）：\n"
            "1) 若顧客只給型號、分不清要『單買零件』還是『整台電腦』"
            "（例如只說 5060，可能是顯示卡也可能是含該顯卡的筆電），action=ask，"
            "先反問是要單買零件還是整台電腦（例：請問是想詢問 5060 顯卡嗎？"
            "還是需要搭載 5060 顯卡的筆電呢？）。\n"
            "2) 若類別已確定、但貨況表顯示同型號有多個品牌、顧客又沒指定品牌，"
            "action=ask，反問偏好品牌並可附上目前有的品牌與現貨單價"
            "（例：請問有需要特定品牌嗎？我們現貨有 XXX，未稅單價 OOO）。\n"
            "3) 資訊足夠（類別明確、且品牌已指定或只有單一品牌）時 action=quote，"
            "提供貨況與報價：有貨就報未稅單價並引導下單；缺貨就說明可調貨、預估調貨天數與未稅參考價。\n"
            "鐵則：所有價格一律為『未稅價』，且只能使用貨況表提供的數字，"
            "不要捏造規格、價格或庫存；回覆的商品類別必須與顧客詢問一致"
            "（顧客問顯示卡就不要報整台筆電的價格）。"
            "語氣親切專業。只輸出 JSON，不要多餘文字。",
            desc="第3層 系統提示詞（自行判斷反問或報價，輸出 action JSON；報價皆未稅）"),
    Setting("PIPELINE_L4_MODEL", "",
            desc="第4層 最嚴苛的店長 模型（留空用主模型）"),
    Setting("PIPELINE_L4_PROMPT",
            "你是最嚴苛的店長，負責回覆送出前最後把關。第3層可能產出兩種草稿："
            "『反問』或『報價』，你要分別檢核：\n"
            "若是反問：確認這個反問是『必要』的——只有在資訊真的不足以報價時才該問"
            "（例如分不清要顯卡或筆電、或同型號多品牌而顧客沒指定）；問題要清楚、"
            "一次只問一件事、語氣親切。若其實資訊已足夠，就不要再問，改成直接報價。\n"
            "若是報價：核對草稿中每個價格與庫存是否與『貨況表』一致；所有價格必須是"
            "『未稅價』；商品類別必須與顧客詢問一致（顧客問顯示卡就不可報整台筆電）；"
            "缺貨要正確說明可調貨與預估天數。價格規則：『同一個品項』若同時有本店價與"
            "報價單價，採兩者中較高者；此『價高優先』僅限同一產品的不同來源比價，"
            "絕對不可把不同產品（例如把單買顯示卡換成含該顯卡的整台筆電）拿來比價或替換。\n"
            "發現任何亂猜、捏造、類別不符或價格錯誤就直接改正。"
            "最後只輸出要傳給顧客的最終訊息，不要附加任何說明或 JSON。",
            desc="第4層 系統提示詞（檢核第3層的反問或報價；未稅、同品項價高優先、不可跨產品）"),
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

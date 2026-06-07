"""
原價屋（coolpc）報價爬蟲與快取。

設計重點（依使用者建議，避免把對方網站爬爛、並提升查詢速度）：
- 爬蟲帶 User-Agent，禮貌存取。
- 解析 evaluate.php 的 <option> 清單，擷取品項名稱與價格。
- 結果寫入 SQL Server 的 coolpc_cache 表；建議每天大爬一次。
- AI 查詢時優先讀「快取表」，速度快且不增加對方負載；
  快取無資料或未啟用 DB 時，才即時爬一次當後備。

注意：evaluate.php 為第三方頁面，版面改版可能導致解析失效；
本模組以盡力而為（best-effort）方式處理，失敗時回傳空結果而非中斷主流程。
"""

import re
import html

import requests

from . import db
from .logbuffer import log

COOLPC_URL = "https://www.coolpc.com.tw/evaluate.php"

# 禮貌的瀏覽器 User-Agent，降低被防火牆擋下的機率
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# 從一段文字中擷取價格：抓 $ 後的數字，或字串中最後一個看起來像價格的數字
_PRICE_PATTERNS = [
    re.compile(r"\$\s*([0-9][0-9,]*)"),
    re.compile(r"(?:NT\$|NTD)\s*([0-9][0-9,]*)", re.IGNORECASE),
]
# 原價屋 evaluate.php 的實際結構（重點）：
# - 每個產品大分類是一個 <SELECT name=nN>，N 是分類編號（n1..n30）。
# - 分類中文標題在 select 前面，形如：<TD class=w>5<TD class=t>主機板 MB ...
# - <OPTION> 沒有對應的 </OPTION> 結束標籤，內容延續到下一個 < 為止。
# 因此用「分類標題表 + select 區塊」對應，種類即可 100% 由結構決定（不需臆測）。
_HEADER_RE = re.compile(
    r"<TD\s+class=w>\s*(\d+)\s*<TD\s+class=t>\s*([^<]+?)\s*(?:<a|<TD|<)",
    re.IGNORECASE,
)
_SELECT_RE = re.compile(
    r"<SELECT[^>]*\bname=n(\d+)\b[^>]*>(.*?)</SELECT>",
    re.IGNORECASE | re.DOTALL,
)
# 無結束標籤的 OPTION：抓到下一個 < 之前的文字
_OPTION_TEXT_RE = re.compile(r"<OPTION\b[^>]*>([^<]*)", re.IGNORECASE)

# 舊版（保留作為後備）：標準 optgroup / option（含結束標籤、雙引號 label）
_OPTGROUP_RE = re.compile(
    r"<optgroup[^>]*label=[\"']([^\"']*)[\"'][^>]*>(.*?)</optgroup>",
    re.IGNORECASE | re.DOTALL,
)
_OPTION_RE = re.compile(r"<option[^>]*>(.*?)</option>", re.IGNORECASE | re.DOTALL)

# coolpc 分類標題 → 正規化種類；用關鍵字比對，避免分類編號變動。
# 順序即優先序（較專一的關鍵字放前面）。
_CATEGORY_NORMALIZE = (
    ("筆電", "筆電"), ("平板", "筆電"), ("穿戴", "筆電"),
    ("套裝", "主機"), ("品牌", "主機"), ("AIO", "主機"),
    ("準系統", "主機"), ("迷你", "主機"),
    ("處理器", "處理器"), ("CPU", "處理器"),
    ("主機板", "主機板"), ("MB", "主機板"),
    ("記憶體", "記憶體"), ("RAM", "記憶體"),
    ("固態硬碟", "硬碟"), ("SSD", "硬碟"), ("M.2", "硬碟"),
    ("傳統", "硬碟"), ("HDD", "硬碟"),
    ("隨身", "儲存"), ("記憶卡", "儲存"),
    ("水冷", "散熱"), ("散熱", "散熱"),
    ("顯示卡", "顯示卡"), ("VGA", "顯示卡"),
    ("螢幕", "螢幕"), ("投影", "螢幕"), ("壁掛", "螢幕"),
    ("機殼風扇", "機殼配件"), ("機殼配件", "機殼配件"),
    ("機殼", "機殼"), ("CASE", "機殼"),
    ("電源", "電源"), ("PSU", "電源"),
    ("鍵盤", "鍵盤"),
    ("滑鼠", "滑鼠"), ("鼠墊", "滑鼠"), ("數位板", "滑鼠"),
    ("分享器", "網通"), ("網卡", "網通"), ("網通", "網通"),
    ("NAS", "網通"), ("IPCAM", "網通"),
    ("音效", "影音"), ("電視卡", "影音"),
    ("喇叭", "喇叭耳機"), ("耳機", "喇叭耳機"), ("麥克風", "喇叭耳機"),
    ("燒錄", "光碟機"),
    ("週邊", "週邊"), ("讀卡機", "週邊"), ("硬碟座", "週邊"),
    ("行車", "週邊"), ("視訊鏡頭", "週邊"),
    ("UPS", "週邊"), ("印表機", "週邊"), ("掃描", "週邊"),
    ("擴充卡", "介面卡"), ("Raid", "介面卡"),
    ("傳輸線", "線材"), ("轉頭", "線材"), ("KVM", "線材"),
    ("軟體", "軟體"), ("禮物卡", "軟體"),
    ("福利品", "福利品"),
)


def _normalize_category(title: str) -> str:
    """把 coolpc 分類標題正規化成簡短種類；對不到時回退為去除開頭編號的標題。"""
    t = (title or "").strip()
    up = t.upper()
    for kw, norm in _CATEGORY_NORMALIZE:
        if kw.upper() in up:
            return norm
    return re.sub(r"^\s*\d+\s*", "", t).strip()


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def allowed_category_nums(settings: dict) -> set:
    """解析 COOLPC_CATEGORY_NUMS 設定成分類編號集合；留空代表不過濾（全部）。"""
    raw = (settings.get("COOLPC_CATEGORY_NUMS") or "").strip()
    if not raw:
        return set()
    nums = set()
    for tok in re.split(r"[,\s]+", raw):
        tok = tok.strip()
        if tok.isdigit():
            nums.add(int(tok))
    return nums


def is_enabled(settings: dict) -> bool:
    return _truthy(settings.get("COOLPC_ENABLED", "true"))


def _parse_price(text: str):
    """從文字擷取價格（整數）；找不到回 None。"""
    for pat in _PRICE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _clean(text: str) -> str:
    """去除 HTML 標籤與多餘空白，並還原 HTML escape。"""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# 常見品牌關鍵字（用於從品名推斷品牌，方便第二層分辨同型號的不同產品）
_BRANDS = (
    "ASUS", "華碩", "ROG", "TUF", "GIGABYTE", "技嘉", "AORUS", "MSI", "微星",
    "ASROCK", "華擎", "ZOTAC", "索泰", "PNY", "PALIT", "GAINWARD", "INNO3D",
    "EVGA", "ACER", "宏碁", "PREDATOR", "INTEL", "AMD", "NVIDIA", "GALAX",
    "影馳", "技嘉", "微星", "MICRON", "美光", "KINGSTON", "金士頓", "ADATA",
    "威剛", "CORSAIR", "海盜船", "SEAGATE", "希捷", "WD", "威騰", "LENOVO",
    "聯想", "THINKPAD", "HP", "惠普", "DELL", "戴爾", "APPLE", "蘋果",
)


def _guess_brand(text: str) -> str:
    """從品名文字推斷品牌；找不到回空字串。"""
    up = (text or "").upper()
    for b in _BRANDS:
        if b.upper() in up:
            return b
    return ""


# 種類判斷規則（不依賴 LLM，確保每筆都能被分類）。
# 螢幕尺寸（用來輔助辨識筆電 vs 螢幕）：例如 16吋、15.6 inch
_SCREEN_RE = re.compile(r"\d{2}(?:\.\d)?\s*(?:吋|inch)", re.IGNORECASE)
_LAPTOP_RE = re.compile(r"筆電|筆記型|laptop|notebook", re.IGNORECASE)
# 整機/準系統：含「全配/套裝/主機/桌機」等字樣
_WHOLE_RE = re.compile(r"全配|套裝|準系統|整機|桌機|主機(?!板)", re.IGNORECASE)
# 各零件類別關鍵字（順序即優先序）
_COMPONENT_RULES = (
    ("顯示卡", re.compile(
        r"顯示卡|顯卡|GeForce|Radeon|\bRTX\s?\d|\bGTX\s?\d|\bRX\s?\d{3,4}"
        r"|\bARC\s?[AB]?\d", re.IGNORECASE)),
    ("主機板", re.compile(
        r"主機板|主板|晶片組|\b[ZBHX]\d{3}\b|\bTRX\d{2}\b|\bWRX\d{2}\b"
        r"|E-ATX|Mini-ITX|Micro-ATX|\bM-ATX\b", re.IGNORECASE)),
    ("處理器", re.compile(
        r"處理器|\bCPU\b|\bi[3579]\b|\bUltra\s?[3579]\b|Ryzen|Threadripper"
        r"|Core\s?Ultra|\b\d{4,5}[KFXTHG]+\b", re.IGNORECASE)),
    ("記憶體", re.compile(r"記憶體|\bDDR[345]\b|\bRAM\b", re.IGNORECASE)),
    ("硬碟", re.compile(
        r"固態硬碟|硬碟|\bSSD\b|\bNVMe\b|\bHDD\b|\bM\.?2\b", re.IGNORECASE)),
    ("電源", re.compile(
        r"電源供應器|電源|\bPSU\b|\d{3,4}\s?W\b|80\s?PLUS", re.IGNORECASE)),
    ("散熱", re.compile(r"水冷|散熱|塔散|風扇|\bAIO\b|cooler", re.IGNORECASE)),
    ("機殼", re.compile(r"機殼|機箱|\bCASE\b", re.IGNORECASE)),
)
_MONITOR_RE = re.compile(r"螢幕|顯示器|monitor|曲面", re.IGNORECASE)


def _scan_category(s: str) -> str:
    """純文字規則判斷種類；無法判斷回空字串。"""
    s = s or ""
    matches = [name for name, rgx in _COMPONENT_RULES if rgx.search(s)]
    has_screen = bool(_SCREEN_RE.search(s))
    # 筆電：明寫筆電，或「螢幕尺寸 + 多個零件規格」的組合（原價屋筆電常不寫「筆電」）
    if _LAPTOP_RE.search(s) or (has_screen and len(matches) >= 2):
        return "筆電"
    # 整機：明寫全配/主機，或同時含 3 種以上零件（組合機/套裝）
    if _WHOLE_RE.search(s) or len(matches) >= 3:
        return "主機"
    if matches:
        return matches[0]
    if has_screen or _MONITOR_RE.search(s):
        return "螢幕"
    return ""


def _guess_category(text: str, label: str = "") -> str:
    """從品名（與 optgroup 標籤後備）推斷種類；找不到回空字串。"""
    return _scan_category(text) or _scan_category(label)


def _clean_item_name(text: str) -> str:
    """把 OPTION 文字整理成乾淨品名：去價格與後面的活動符號（◆ ★ 等）。"""
    text = html.unescape(text or "")
    # 砍掉價格與其後的活動標記（$12990 ◆ ★ 熱賣…）
    text = re.split(r",?\s*\$\d", text, maxsplit=1)[0]
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _append_item(items: list, category: str, raw: str):
    """把一筆 OPTION 文字轉成品項並加入 items（無價格者略過）。"""
    full = re.sub(r"\s+", " ", html.unescape(raw or "")).strip()
    if not full or len(full) < 2:
        return
    price = _parse_price(full)
    if price is None:
        return  # 跳過「共有商品 N 樣」等無價格的標頭/說明列
    name = _clean_item_name(full) or full
    items.append({
        "category": category,
        "brand": _guess_brand(full),
        "item_name": name,
        "price": price,
        "raw_text": full[:1000],
    })


def parse_options(html_text: str, allowed_nums: set | None = None) -> list:
    """解析 evaluate.php，回傳 [{category, brand, item_name, price, raw_text}, ...]。

    主路徑（依原價屋實際結構，種類由結構決定、不需臆測）：
      1) 先用 <TD class=w>N<TD class=t>標題 建立『分類編號→中文標題』對照表。
      2) 每個 <SELECT name=nN> 區塊裡的 <OPTION> 即該分類的商品；
         分類標題經 _normalize_category() 正規化成簡短種類。
    後備路徑：舊式 optgroup/option，或最後退回純規則 _guess_category()。

    allowed_nums：若提供（非空），主路徑只擷取分類編號在此集合內的 SELECT，
                  其餘分類略過；同時停用無編號資訊的後備路徑，避免寫入未指定分類。
    """
    items = []
    allowed_nums = allowed_nums or None

    # 主路徑：select name=nN + 分類標題表
    cat_titles = {int(m.group(1)): m.group(2).strip()
                  for m in _HEADER_RE.finditer(html_text)}
    selects = list(_SELECT_RE.finditer(html_text))
    if selects:
        for sm in selects:
            num = int(sm.group(1))
            # 只寫入指定分類編號（未指定則全收）
            if allowed_nums and num not in allowed_nums:
                continue
            category = _normalize_category(cat_titles.get(num, ""))
            for raw in _OPTION_TEXT_RE.findall(sm.group(2)):
                _append_item(items, category, raw)
        if items:
            return items
        # 有指定分類但主路徑沒抓到任何品項：不退到無編號後備，直接回空
        if allowed_nums:
            return items

    # 指定了分類編號時，後備路徑沒有編號資訊、無法過濾，故不啟用
    if allowed_nums:
        return items

    # 後備一：標準 optgroup（含結束標籤）
    groups = _OPTGROUP_RE.findall(html_text)
    if groups:
        for label, block in groups:
            category = _normalize_category(_clean(label)) or _guess_category("", _clean(label))
            for raw in _OPTION_RE.findall(block):
                text = _clean(raw)
                category2 = category or _guess_category(text)
                _append_item(items, category2, text)
        if items:
            return items

    # 後備二：直接抓所有 option，種類用純規則猜
    for raw in _OPTION_RE.findall(html_text):
        text = _clean(raw)
        _append_item(items, _guess_category(text), text)
    return items


def fetch(settings: dict, timeout: int = 30) -> list:
    """即時抓取並解析原價屋報價，回傳品項清單（不寫入快取）。

    依 COOLPC_CATEGORY_NUMS 設定，只擷取指定分類編號的品項。
    """
    url = settings.get("COOLPC_URL", COOLPC_URL) or COOLPC_URL
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
        resp.raise_for_status()
        # evaluate.php 編碼偵測（多為 UTF-8，保險起見以 apparent_encoding 後備）
        if not resp.encoding or resp.encoding.lower() in ("iso-8859-1",):
            resp.encoding = resp.apparent_encoding or "utf-8"
        items = parse_options(resp.text, allowed_category_nums(settings))
        log(f"原價屋爬取完成，解析到 {len(items)} 筆品項")
        return items
    except Exception as e:
        log(f"原價屋爬取失敗：{e}", "ERROR")
        return []


# ---------------------------------------------------------------------------
# 快取（存 SQL Server coolpc_cache）
# ---------------------------------------------------------------------------
def refresh_cache(settings: dict) -> tuple[bool, str]:
    """大爬一次並覆蓋 coolpc_cache。建議每天排程執行一次。

    種類/品牌/品名在 parse_options() 解析時即依原價屋網頁結構決定（快速、準確），
    不需任何 AI 後處理。
    """
    if not db.is_enabled(settings) or not db.PYODBC_AVAILABLE:
        return False, "未啟用 SQL Server，無法寫入報價快取。"
    items = fetch(settings)
    if not items:
        return False, "未取得任何報價，快取未更新。"
    try:
        conn = db.get_connection(settings, timeout=30)
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM dbo.coolpc_cache")
            cur.fast_executemany = True
            cur.executemany(
                "INSERT INTO dbo.coolpc_cache (category, brand, item_name, price, raw_text) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (it["category"], it.get("brand", ""), it["item_name"],
                     it["price"], it["raw_text"])
                    for it in items
                ],
            )
            conn.commit()
        finally:
            conn.close()
        log(f"報價快取已更新：{len(items)} 筆")
        return True, f"報價快取已更新：{len(items)} 筆品項。"
    except Exception as e:
        log(f"更新報價快取失敗：{e}", "ERROR")
        return False, f"更新報價快取失敗：{e}"


def cache_count(settings: dict) -> int:
    """回傳快取目前筆數（失敗回 -1）。"""
    if not db.is_enabled(settings) or not db.PYODBC_AVAILABLE:
        return -1
    try:
        conn = db.get_connection(settings, timeout=10)
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM dbo.coolpc_cache")
            return int(cur.fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return -1


def search_cache(settings: dict, keyword: str, limit: int = 10,
                 category: str = "") -> list:
    """在快取表用關鍵字模糊比對品項，回傳含類別/品牌/價格的清單（價高優先）。

    category：可選的類別過濾（例如「顯示卡」），收斂結果避免抓到含相同
              型號的整台筆電/桌機。
    """
    keyword = (keyword or "").strip()
    if not keyword or not db.is_enabled(settings) or not db.PYODBC_AVAILABLE:
        return []
    category = (category or "").strip()
    sql = (
        "SELECT TOP (?) category, brand, item_name, price, raw_text "
        "FROM dbo.coolpc_cache "
        "WHERE item_name LIKE ? AND price IS NOT NULL "
    )
    params = [limit, f"%{keyword}%"]
    if category:
        sql += "AND category LIKE ? "
        params.append(f"%{category}%")
    sql += "ORDER BY price DESC"
    try:
        conn = db.get_connection(settings, timeout=10)
        try:
            cur = conn.cursor()
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            return [
                {"category": r[0], "brand": r[1], "item_name": r[2],
                 "price": float(r[3]) if r[3] is not None else None,
                 "raw_text": r[4], "source": "coolpc"}
                for r in rows
            ]
        finally:
            conn.close()
    except Exception as e:
        log(f"查詢報價快取失敗：{e}", "ERROR")
        return []


def search(settings: dict, keyword: str, limit: int = 10,
           category: str = "") -> list:
    """查報價：優先讀快取；快取無資料時即時爬一次當後備（不寫入快取）。

    category：可選的類別過濾，與 search_cache 一致。
    """
    results = search_cache(settings, keyword, limit, category=category)
    if results:
        return results
    # 後備：即時爬（僅在快取空或未啟用 DB 時）
    kw = (keyword or "").strip()
    if not kw:
        return []
    cat = (category or "").strip().lower()
    items = fetch(settings)
    matched = [
        it for it in items
        if it.get("price") is not None and kw.lower() in it["item_name"].lower()
        and (not cat or cat in (it.get("category", "") or "").lower())
    ]
    matched.sort(key=lambda x: x["price"], reverse=True)
    for it in matched:
        it["source"] = "coolpc"
    return matched[:limit]

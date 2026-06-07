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
# <optgroup label="分類"> ... </optgroup>
_OPTGROUP_RE = re.compile(
    r'<optgroup[^>]*label="([^"]*)"[^>]*>(.*?)</optgroup>',
    re.IGNORECASE | re.DOTALL,
)
# <option ...>內容</option>
_OPTION_RE = re.compile(r"<option[^>]*>(.*?)</option>", re.IGNORECASE | re.DOTALL)


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


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


def parse_options(html_text: str) -> list:
    """解析 evaluate.php 內容，回傳 [{category, brand, item_name, price, raw_text}, ...]。"""
    items = []

    def handle_block(category: str, block: str):
        for raw in _OPTION_RE.findall(block):
            text = _clean(raw)
            if not text or len(text) < 2:
                continue
            price = _parse_price(text)
            items.append({
                "category": category or "",
                "brand": _guess_brand(text),
                "item_name": text,
                "price": price,
                "raw_text": text[:1000],
            })

    groups = _OPTGROUP_RE.findall(html_text)
    if groups:
        for label, block in groups:
            handle_block(_clean(label), block)
    else:
        # 沒有 optgroup 時，退而求其次直接抓全部 option
        handle_block("", html_text)

    return items


def fetch(settings: dict, timeout: int = 30) -> list:
    """即時抓取並解析原價屋報價，回傳品項清單（不寫入快取）。"""
    url = settings.get("COOLPC_URL", COOLPC_URL) or COOLPC_URL
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
        resp.raise_for_status()
        # evaluate.php 編碼偵測（多為 UTF-8，保險起見以 apparent_encoding 後備）
        if not resp.encoding or resp.encoding.lower() in ("iso-8859-1",):
            resp.encoding = resp.apparent_encoding or "utf-8"
        items = parse_options(resp.text)
        log(f"原價屋爬取完成，解析到 {len(items)} 筆品項")
        return items
    except Exception as e:
        log(f"原價屋爬取失敗：{e}", "ERROR")
        return []


# ---------------------------------------------------------------------------
# 快取（存 SQL Server coolpc_cache）
# ---------------------------------------------------------------------------
def refresh_cache(settings: dict) -> tuple[bool, str]:
    """大爬一次並覆蓋 coolpc_cache。建議每天排程執行一次。"""
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

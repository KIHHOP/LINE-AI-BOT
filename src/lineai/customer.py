"""
客戶資料存取層：以 LINE userId 為主鍵，操作 SQL Server 上的
customers / conversations / purchases / parts_prices 等表。

所有函式都先確認 SQL Server 已啟用且 pyodbc 可用，否則回傳安全的預設值，
不讓資料庫問題影響 LINE 回覆主流程。
"""

import os

from . import db
from .config import PROJECT_ROOT
from .logbuffer import log

SCHEMA_PATH = os.path.join(PROJECT_ROOT, "sql", "schema.sql")


def _split_batches(sql_text: str) -> list:
    """以單獨一行的 GO 為界，把腳本切成多個批次（pyodbc 不認得 GO）。"""
    batches = []
    current = []
    for line in sql_text.splitlines():
        if line.strip().upper() == "GO":
            batch = "\n".join(current).strip()
            if batch:
                batches.append(batch)
            current = []
        else:
            current.append(line)
    tail = "\n".join(current).strip()
    if tail:
        batches.append(tail)
    return batches


def ensure_schema(settings: dict) -> tuple[bool, str]:
    """讀取 sql/schema.sql 並在資料庫建立所有資料表（可重複執行）。"""
    if not db.PYODBC_AVAILABLE:
        return False, "未安裝 pyodbc，無法初始化資料表。"
    if not os.path.exists(SCHEMA_PATH):
        return False, f"找不到結構檔：{SCHEMA_PATH}"
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            sql_text = f.read()
    except Exception as e:
        return False, f"讀取結構檔失敗：{e}"

    try:
        conn = db.get_connection(settings, timeout=15)
        try:
            cur = conn.cursor()
            for batch in _split_batches(sql_text):
                cur.execute(batch)
            conn.commit()
        finally:
            conn.close()
        log("資料表初始化完成（customers / purchases / parts_prices / conversations）")
        return True, "資料表初始化完成。"
    except Exception as e:
        log(f"初始化資料表失敗：{e}", "ERROR")
        return False, f"初始化資料表失敗：{e}"


def upsert_customer(settings: dict, profile: dict) -> bool:
    """新增或更新客戶資料。profile 至少需含 line_user_id。

    使用 MERGE 達成「有則更新、無則新增」；同時更新 last_seen_at。
    """
    if not db.is_enabled(settings) or not db.PYODBC_AVAILABLE:
        return False
    user_id = (profile.get("line_user_id") or "").strip()
    if not user_id:
        return False

    sql = """
MERGE dbo.customers AS target
USING (SELECT ? AS line_user_id) AS src
ON target.line_user_id = src.line_user_id
WHEN MATCHED THEN
    UPDATE SET
        display_name   = COALESCE(?, target.display_name),
        picture_url    = COALESCE(?, target.picture_url),
        status_message = COALESCE(?, target.status_message),
        language       = COALESCE(?, target.language),
        is_friend      = ?,
        last_seen_at   = SYSDATETIME()
WHEN NOT MATCHED THEN
    INSERT (line_user_id, display_name, picture_url, status_message, language, is_friend)
    VALUES (?, ?, ?, ?, ?, ?);
"""
    params = (
        user_id,
        profile.get("display_name"),
        profile.get("picture_url"),
        profile.get("status_message"),
        profile.get("language"),
        1 if profile.get("is_friend", True) else 0,
        user_id,
        profile.get("display_name"),
        profile.get("picture_url"),
        profile.get("status_message"),
        profile.get("language"),
        1 if profile.get("is_friend", True) else 0,
    )
    try:
        conn = db.get_connection(settings, timeout=10)
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log(f"寫入客戶資料失敗：{e}", "ERROR")
        return False


def get_customer(settings: dict, user_id: str) -> dict | None:
    """依 userId 取回客戶資料；找不到回傳 None。"""
    if not db.is_enabled(settings) or not db.PYODBC_AVAILABLE:
        return None
    sql = (
        "SELECT line_user_id, display_name, picture_url, status_message, "
        "language, is_friend, first_seen_at, last_seen_at, note "
        "FROM dbo.customers WHERE line_user_id = ?"
    )
    try:
        conn = db.get_connection(settings, timeout=10)
        try:
            cur = conn.cursor()
            cur.execute(sql, (user_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, row))
        finally:
            conn.close()
    except Exception as e:
        log(f"讀取客戶資料失敗：{e}", "ERROR")
        return None


def log_message(settings: dict, user_id: str, role: str, content: str) -> bool:
    """把一則對話訊息寫進 conversations 表。"""
    if not db.is_enabled(settings) or not db.PYODBC_AVAILABLE:
        return False
    if not user_id or not content:
        return False
    sql = (
        "INSERT INTO dbo.conversations (line_user_id, role, content) "
        "VALUES (?, ?, ?)"
    )
    try:
        conn = db.get_connection(settings, timeout=10)
        try:
            cur = conn.cursor()
            cur.execute(sql, (user_id, role, content))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log(f"寫入對話紀錄失敗：{e}", "ERROR")
        return False


def search_parts(settings: dict, keyword: str, limit: int = 10,
                 category: str = "") -> list:
    """在 parts_prices 表用關鍵字模糊比對零件，回傳含庫存與價格的清單。

    回傳每筆含 category（類別）、brand（品牌）、part_name（品名）、
    in_stock（庫存>0）與 source='sql'，價高優先排序。

    category：可選的類別過濾（例如「顯示卡」），避免「RTX5060」這類關鍵字
              誤抓到含相同型號的整台筆電/桌機。傳入時以類別 LIKE 收斂結果。
    """
    keyword = (keyword or "").strip()
    if not keyword or not db.is_enabled(settings) or not db.PYODBC_AVAILABLE:
        return []
    category = (category or "").strip()
    sql = (
        "SELECT TOP (?) part_no, category, brand, part_name, spec, unit, price, "
        "currency, stock_qty, is_active "
        "FROM dbo.parts_prices "
        "WHERE is_active = 1 AND (part_name LIKE ? OR part_no LIKE ? OR spec LIKE ?) "
    )
    like = f"%{keyword}%"
    params = [limit, like, like, like]
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
            result = []
            for r in rows:
                stock = r[8]
                result.append({
                    "part_no": r[0],
                    "category": r[1],
                    "brand": r[2],
                    "part_name": r[3],
                    "spec": r[4],
                    "unit": r[5],
                    "price": float(r[6]) if r[6] is not None else None,
                    "currency": r[7],
                    "stock_qty": int(stock) if stock is not None else None,
                    "in_stock": bool(stock and stock > 0),
                    "source": "sql",
                })
            return result
        finally:
            conn.close()
    except Exception as e:
        log(f"查詢零件失敗：{e}", "ERROR")
        return []


def recent_messages(settings: dict, user_id: str, limit: int = 20) -> list:
    """取回某客戶最近的對話（時間正序），供產生精華使用。"""
    if not db.is_enabled(settings) or not db.PYODBC_AVAILABLE:
        return []
    sql = (
        "SELECT role, content FROM ("
        "  SELECT TOP (?) role, content, created_at "
        "  FROM dbo.conversations WHERE line_user_id = ? "
        "  ORDER BY created_at DESC"
        ") AS t ORDER BY t.created_at ASC"
    )
    try:
        conn = db.get_connection(settings, timeout=10)
        try:
            cur = conn.cursor()
            cur.execute(sql, (limit, user_id))
            rows = cur.fetchall()
            return [{"role": r[0], "content": r[1]} for r in rows]
        finally:
            conn.close()
    except Exception as e:
        log(f"讀取對話紀錄失敗：{e}", "ERROR")
        return []

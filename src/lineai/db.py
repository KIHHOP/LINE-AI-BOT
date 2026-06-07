"""
SQL Server 連線：以 pyodbc 連線、測試與查詢。

pyodbc 為選用相依；未安裝時相關功能會回傳友善訊息而不致讓整個 WebUI 崩潰。
實際連線資訊來自設定（SQLSERVER_*）。
"""

from .logbuffer import log

try:
    import pyodbc
    PYODBC_AVAILABLE = True
except Exception:
    PYODBC_AVAILABLE = False


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def is_enabled(settings: dict) -> bool:
    return _truthy(settings.get("SQLSERVER_ENABLED", "false"))


def build_connection_string(settings: dict) -> str:
    """依設定組出 pyodbc 連線字串。"""
    driver = (settings.get("SQLSERVER_DRIVER") or "ODBC Driver 17 for SQL Server").strip()
    host = (settings.get("SQLSERVER_HOST") or "localhost").strip()
    port = (settings.get("SQLSERVER_PORT") or "").strip()
    database = (settings.get("SQLSERVER_DATABASE") or "").strip()
    user = (settings.get("SQLSERVER_USER") or "").strip()
    password = settings.get("SQLSERVER_PASSWORD") or ""
    encrypt = "yes" if _truthy(settings.get("SQLSERVER_ENCRYPT", "false")) else "no"

    # 具名執行個體（host 內含反斜線，如 localhost\SQLEXPRESS）由 SQL Browser
    # 決定動態埠，不可再附加埠號；只有明確指定埠且非具名執行個體時才加 ,port。
    is_named_instance = "\\" in host
    if port and not is_named_instance:
        server = f"{host},{port}"
    else:
        server = host

    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={server}",
    ]
    if database:
        parts.append(f"DATABASE={database}")
    if user:
        # SQL 驗證
        parts.append(f"UID={user}")
        parts.append(f"PWD={password}")
    else:
        # 未填帳號時改用 Windows 整合驗證
        parts.append("Trusted_Connection=yes")
    parts.append(f"Encrypt={encrypt}")
    parts.append("TrustServerCertificate=yes")
    return ";".join(parts) + ";"


def _safe_conn_string_for_log(settings: dict) -> str:
    """產生不含密碼的連線字串供日誌顯示。"""
    s = dict(settings)
    if s.get("SQLSERVER_PASSWORD"):
        s["SQLSERVER_PASSWORD"] = "***"
    return build_connection_string(s)


def get_connection(settings: dict, timeout: int = 5):
    """建立並回傳一個 pyodbc 連線（呼叫端負責關閉）。失敗時拋出例外。"""
    if not PYODBC_AVAILABLE:
        raise RuntimeError("未安裝 pyodbc，請先 pip install pyodbc")
    conn_str = build_connection_string(settings)
    return pyodbc.connect(conn_str, timeout=timeout)


def test_connection(settings: dict) -> tuple[bool, str]:
    """測試 SQL Server 連線，回傳 (成功, 訊息)。"""
    if not PYODBC_AVAILABLE:
        return False, "未安裝 pyodbc。請執行：pip install pyodbc，並確認已安裝對應的 ODBC Driver。"
    try:
        conn = get_connection(settings, timeout=5)
        try:
            cur = conn.cursor()
            cur.execute("SELECT @@VERSION")
            row = cur.fetchone()
            version = (row[0].splitlines()[0] if row and row[0] else "未知版本").strip()
        finally:
            conn.close()
        log(f"SQL Server 連線成功：{version}")
        return True, f"連線成功：{version}"
    except Exception as e:
        log(f"SQL Server 連線失敗：{e}", "ERROR")
        return False, f"連線失敗：{e}"


def list_drivers() -> list:
    """列出本機可用的 ODBC 驅動名稱（供 UI 下拉）。"""
    if not PYODBC_AVAILABLE:
        return []
    try:
        return [d for d in pyodbc.drivers() if "SQL Server" in d]
    except Exception:
        return []


def run_query(settings: dict, sql: str, max_rows: int = 50) -> tuple[bool, str]:
    """執行一段唯讀查詢並回傳格式化結果文字（限制筆數，供測試用）。"""
    if not PYODBC_AVAILABLE:
        return False, "未安裝 pyodbc"
    sql = (sql or "").strip()
    if not sql:
        return False, "請輸入 SQL 查詢"
    try:
        conn = get_connection(settings, timeout=10)
        try:
            cur = conn.cursor()
            cur.execute(sql)
            if cur.description is None:
                conn.commit()
                return True, f"執行成功，影響 {cur.rowcount} 列"
            cols = [c[0] for c in cur.description]
            rows = cur.fetchmany(max_rows)
            lines = [" | ".join(cols), "-" * 40]
            for r in rows:
                lines.append(" | ".join("" if v is None else str(v) for v in r))
            more = "" if len(rows) < max_rows else f"\n…（僅顯示前 {max_rows} 列）"
            return True, "\n".join(lines) + more
        finally:
            conn.close()
    except Exception as e:
        log(f"查詢失敗：{e}", "ERROR")
        return False, f"查詢失敗：{e}"

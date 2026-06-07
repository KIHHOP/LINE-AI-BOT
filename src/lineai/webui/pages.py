"""
WebUI 頁面版面：
- /login          登入頁
- /               主控制台（以選項卡分頁：總覽 / Cloudflare / LINE / Ollama / SQL Server / 安全性 / 測試 / 日誌）

UI 只負責呈現與呼叫；實際邏輯都在 config / ollama / line_api / tunnel / db。
"""

from nicegui import ui, run

from .. import config, ollama, tunnel, db, customer, memory, pipeline, pricing
from ..logbuffer import get_logs, clear_logs, log
from .server import settings, verify_password

# UI 元件參照（供刷新用）
refs: dict = {}


# ---------------------------------------------------------------------------
# 登入頁
# ---------------------------------------------------------------------------
@ui.page("/login")
def login_page():
    from nicegui import app as nicegui_app

    ui.colors(primary="#06C755")

    # 已登入就直接進主頁
    if nicegui_app.storage.user.get("authenticated", False):
        ui.navigate.to("/")
        return

    def do_login():
        if verify_password(pw.value):
            nicegui_app.storage.user["authenticated"] = True
            ui.navigate.to("/")
        else:
            ui.notify("密碼錯誤", type="negative")

    with ui.card().classes("absolute-center w-96"):
        ui.label("LINE AI Bot 控制台").classes("text-xl font-bold")
        ui.label("請輸入登入密碼").classes("text-sm text-grey-7")
        pw = ui.input("密碼", password=True, password_toggle_button=True).classes("w-full")
        pw.on("keydown.enter", do_login)
        ui.button("登入", on_click=do_login).props("color=primary").classes("w-full")


# ---------------------------------------------------------------------------
# 狀態刷新
# ---------------------------------------------------------------------------
def refresh_status():
    ollama_ok = ollama.check(settings.get("OLLAMA_BASE_URL", ""))
    refs["ollama_badge"].text = "Ollama：連線正常" if ollama_ok else "Ollama：無法連線"
    refs["ollama_badge"].props(f'color={"positive" if ollama_ok else "negative"}')

    cf_on = tunnel.is_running()
    refs["tunnel_badge"].text = "Tunnel：執行中" if cf_on else "Tunnel：未啟動"
    refs["tunnel_badge"].props(f'color={"positive" if cf_on else "grey"}')

    line_ready = bool(
        settings.get("LINE_CHANNEL_ACCESS_TOKEN") and settings.get("LINE_CHANNEL_SECRET")
    )
    refs["line_badge"].text = "LINE 金鑰：已設定" if line_ready else "LINE 金鑰：未設定"
    refs["line_badge"].props(f'color={"positive" if line_ready else "warning"}')

    sql_on = db.is_enabled(settings)
    refs["sql_badge"].text = "SQL Server：已啟用" if sql_on else "SQL Server：未啟用"
    refs["sql_badge"].props(f'color={"positive" if sql_on else "grey"}')

    url = config.webhook_url(settings)
    refs["webhook_input"].value = url or "（尚未設定對外網域）"

    refresh_wizard()


def refresh_logs():
    refs["log_area"].value = "\n".join(get_logs())


# ---------------------------------------------------------------------------
# 安裝精靈
# ---------------------------------------------------------------------------
def refresh_wizard():
    st = tunnel.status(settings)

    def mark(badge_key, ok, ok_text, no_text):
        refs[badge_key].text = ok_text if ok else no_text
        refs[badge_key].props(f'color={"positive" if ok else "grey"}')

    mark("wiz_install", st["installed"], "已安裝 cloudflared", "尚未偵測到 cloudflared")
    mark("wiz_login", st["logged_in"], "已登入 Cloudflare", "尚未登入")
    mark("wiz_tunnel", st["tunnel_created"], "Tunnel 已建立", "尚未建立 Tunnel")

    if st["exe"]:
        refs["wiz_exe"].text = f"執行檔：{st['exe']}"
    else:
        refs["wiz_exe"].text = "執行檔：找不到，請安裝或於下方填入完整路徑"

    all_ready = st["installed"] and st["logged_in"] and st["tunnel_created"]
    refs["start_btn"].set_enabled(all_ready)


def wizard_login():
    settings["CLOUDFLARED_PATH"] = refs["cloudflared_path_input"].value.strip() or "cloudflared"
    ok, msg = tunnel.login(settings)
    ui.notify(msg, type="positive" if ok else "warning")
    refresh_logs()


def wizard_create():
    settings["CF_TUNNEL_NAME"] = refs["tunnel_name_input"].value.strip() or "linebot"
    ok, msg = tunnel.create(settings, settings["CF_TUNNEL_NAME"])
    ui.notify(msg, type="positive" if ok else "negative")
    refresh_logs()
    refresh_wizard()


def wizard_route():
    settings["CF_TUNNEL_NAME"] = refs["tunnel_name_input"].value.strip() or "linebot"
    settings["PUBLIC_DOMAIN"] = refs["domain_input"].value.strip()
    ok, msg = tunnel.route_dns(settings, settings["CF_TUNNEL_NAME"], settings["PUBLIC_DOMAIN"])
    ui.notify(msg, type="positive" if ok else "negative")
    refresh_logs()
    refresh_status()


# ---------------------------------------------------------------------------
# Tunnel 啟停
# ---------------------------------------------------------------------------
def start_tunnel():
    ok, msg = tunnel.start(settings)
    ui.notify(msg, type="positive" if ok else "warning")
    refresh_status()


def stop_tunnel():
    ok, msg = tunnel.stop()
    ui.notify(msg, type="positive" if ok else "warning")
    refresh_status()


# ---------------------------------------------------------------------------
# Ollama / 設定 / 測試
# ---------------------------------------------------------------------------
def reload_models():
    models = ollama.list_models(settings.get("OLLAMA_BASE_URL", ""))
    if models:
        refs["model_select"].options = models
        if settings.get("OLLAMA_MODEL") not in models:
            refs["model_select"].value = models[0]
        refs["model_select"].update()
        ui.notify(f"已載入 {len(models)} 個模型", type="positive")
    else:
        ui.notify("找不到模型，請確認 Ollama 已啟動並下載模型", type="warning")


def save_all():
    settings["LINE_CHANNEL_ACCESS_TOKEN"] = refs["token_input"].value.strip()
    settings["LINE_CHANNEL_SECRET"] = refs["secret_input"].value.strip()
    settings["LINE_REQUIRE_FRIEND"] = "true" if refs["require_friend"].value else "false"
    settings["LINE_NOT_FRIEND_MESSAGE"] = refs["not_friend_msg"].value.strip()
    settings["OLLAMA_BASE_URL"] = refs["ollama_url_input"].value.strip()
    settings["OLLAMA_MODEL"] = refs["model_select"].value or ""
    settings["OLLAMA_SYSTEM_PROMPT"] = refs["prompt_input"].value
    settings["MEMORY_ENABLED"] = "true" if refs["memory_enabled"].value else "false"
    # 四層流水線
    if "pipeline_enabled" in refs:
        settings["PIPELINE_ENABLED"] = "true" if refs["pipeline_enabled"].value else "false"
        for n in (1, 2, 3, 4):
            settings[f"PIPELINE_L{n}_MODEL"] = refs[f"l{n}_model"].value or ""
            settings[f"PIPELINE_L{n}_PROMPT"] = refs[f"l{n}_prompt"].value
        settings["COOLPC_ENABLED"] = "true" if refs["coolpc_enabled"].value else "false"
        settings["COOLPC_URL"] = refs["coolpc_url"].value.strip()
    settings["PUBLIC_DOMAIN"] = refs["domain_input"].value.strip()
    settings["CF_TUNNEL_NAME"] = refs["tunnel_name_input"].value.strip()
    settings["CLOUDFLARED_PATH"] = refs["cloudflared_path_input"].value.strip()
    settings["WEBUI_PASSWORD"] = refs["password_input"].value.strip() or settings.get("WEBUI_PASSWORD", "")
    # SQL Server
    settings["SQLSERVER_ENABLED"] = "true" if refs["sql_enabled"].value else "false"
    settings["SQLSERVER_HOST"] = refs["sql_host"].value.strip()
    settings["SQLSERVER_PORT"] = str(refs["sql_port"].value or 1433)
    settings["SQLSERVER_DATABASE"] = refs["sql_database"].value.strip()
    settings["SQLSERVER_USER"] = refs["sql_user"].value.strip()
    settings["SQLSERVER_PASSWORD"] = refs["sql_password"].value
    settings["SQLSERVER_DRIVER"] = refs["sql_driver"].value or ""
    settings["SQLSERVER_ENCRYPT"] = "true" if refs["sql_encrypt"].value else "false"
    config.save_settings(settings)
    log("設定已儲存")
    ui.notify("設定已儲存", type="positive")
    refresh_status()


async def run_test_chat():
    text = refs["test_input"].value.strip()
    if not text:
        ui.notify("請先輸入測試訊息", type="warning")
        return
    refs["test_output"].value = "模型思考中…"
    reply = await run.io_bound(ollama.ask, text, settings)
    refs["test_output"].value = reply
    refresh_logs()


# ---------------------------------------------------------------------------
# SQL Server
# ---------------------------------------------------------------------------
def _collect_sql_settings():
    """把畫面上的 SQL Server 欄位收進 settings。"""
    settings["SQLSERVER_ENABLED"] = "true" if refs["sql_enabled"].value else "false"
    settings["SQLSERVER_HOST"] = refs["sql_host"].value.strip()
    settings["SQLSERVER_PORT"] = str(refs["sql_port"].value or 1433)
    settings["SQLSERVER_DATABASE"] = refs["sql_database"].value.strip()
    settings["SQLSERVER_USER"] = refs["sql_user"].value.strip()
    settings["SQLSERVER_PASSWORD"] = refs["sql_password"].value
    settings["SQLSERVER_DRIVER"] = refs["sql_driver"].value or ""
    settings["SQLSERVER_ENCRYPT"] = "true" if refs["sql_encrypt"].value else "false"


def sql_test_connection():
    _collect_sql_settings()
    refs["sql_result"].value = "連線測試中…"
    ok, msg = db.test_connection(settings)
    refs["sql_result"].value = msg
    ui.notify("連線成功" if ok else "連線失敗", type="positive" if ok else "negative")
    refresh_logs()


def sql_run_query():
    _collect_sql_settings()
    refs["sql_result"].value = "查詢中…"
    ok, msg = db.run_query(settings, refs["sql_query"].value)
    refs["sql_result"].value = msg
    refresh_logs()


def sql_reload_drivers():
    drivers = db.list_drivers()
    if drivers:
        refs["sql_driver"].options = drivers
        if settings.get("SQLSERVER_DRIVER") not in drivers:
            refs["sql_driver"].value = drivers[0]
        refs["sql_driver"].update()
        ui.notify(f"找到 {len(drivers)} 個 SQL Server ODBC 驅動", type="positive")
    else:
        ui.notify("找不到 ODBC 驅動，請確認已安裝 pyodbc 與 Microsoft ODBC Driver", type="warning")


def sql_init_schema():
    """在資料庫建立所有資料表（customers / purchases / parts_prices / conversations）。"""
    _collect_sql_settings()
    refs["sql_result"].value = "初始化資料表中…"
    ok, msg = customer.ensure_schema(settings)
    refs["sql_result"].value = msg
    ui.notify(msg, type="positive" if ok else "negative")
    refresh_logs()


# ---------------------------------------------------------------------------
# AI 流水線 / 原價屋
# ---------------------------------------------------------------------------
def _collect_pipeline_settings():
    settings["PIPELINE_ENABLED"] = "true" if refs["pipeline_enabled"].value else "false"
    for n in (1, 2, 3, 4):
        settings[f"PIPELINE_L{n}_MODEL"] = refs[f"l{n}_model"].value or ""
        settings[f"PIPELINE_L{n}_PROMPT"] = refs[f"l{n}_prompt"].value
    settings["COOLPC_ENABLED"] = "true" if refs["coolpc_enabled"].value else "false"
    settings["COOLPC_URL"] = refs["coolpc_url"].value.strip()


async def run_pipeline_test():
    text = refs["pipe_test_input"].value.strip()
    if not text:
        ui.notify("請先輸入測試訊息", type="warning")
        return
    _collect_pipeline_settings()
    refs["pipe_test_output"].value = "流水線執行中（會依序呼叫四個模型，請稍候）…"
    # 阻塞的多次模型呼叫丟到背景執行緒，避免卡住事件迴圈導致前端斷線
    result = await run.io_bound(pipeline.run, settings, text)
    refs["pipe_test_output"].value = result
    refresh_logs()


async def coolpc_refresh_cache():
    _collect_sql_settings()
    _collect_pipeline_settings()
    refs["coolpc_result"].value = "正在爬取原價屋並更新快取（資料量大，請稍候）…"
    ok, msg = await run.io_bound(pricing.refresh_cache, settings)
    refs["coolpc_result"].value = msg
    ui.notify(msg, type="positive" if ok else "negative")
    refresh_coolpc_count()
    refresh_logs()


async def coolpc_search_test():
    _collect_sql_settings()
    _collect_pipeline_settings()
    kw = refs["coolpc_keyword"].value.strip()
    if not kw:
        ui.notify("請先輸入關鍵字", type="warning")
        return
    refs["coolpc_result"].value = "查詢中…"
    hits = await run.io_bound(pricing.search, settings, kw, 10)
    if not hits:
        refs["coolpc_result"].value = "查無結果（快取為空時會即時爬取，仍無則表示沒有符合品項）"
        return
    lines = [f"{h['item_name']}　＝　{int(h['price']) if h.get('price') else '—'}"
             for h in hits]
    refs["coolpc_result"].value = "\n".join(lines)
    refresh_logs()


def refresh_coolpc_count():
    if "coolpc_count" not in refs:
        return
    n = pricing.cache_count(settings)
    refs["coolpc_count"].text = (
        "報價快取：尚未啟用 SQL Server" if n < 0 else f"報價快取：{n} 筆品項"
    )


# ---------------------------------------------------------------------------
# 主頁（選項卡版面）
# ---------------------------------------------------------------------------
@ui.page("/")
def main_page():
    ui.colors(primary="#06C755")

    with ui.header().classes("items-center"):
        ui.icon("smart_toy").classes("text-2xl")
        ui.label("LINE AI Bot 控制台").classes("text-xl font-bold")
        ui.space()
        # 狀態徽章放在標題列右側，所有分頁都看得到
        refs["ollama_badge"] = ui.badge("Ollama：檢查中").props("color=grey")
        refs["tunnel_badge"] = ui.badge("Tunnel：未啟動").props("color=grey")
        refs["line_badge"] = ui.badge("LINE：未設定").props("color=warning")
        refs["sql_badge"] = ui.badge("SQL Server：未啟用").props("color=grey")

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        with ui.tabs().classes("w-full") as tabs:
            tab_overview = ui.tab("總覽", icon="dashboard")
            tab_cf = ui.tab("Cloudflare", icon="cloud")
            tab_line = ui.tab("LINE", icon="chat")
            tab_ollama = ui.tab("Ollama", icon="smart_toy")
            tab_sql = ui.tab("SQL Server", icon="storage")
            tab_pipeline = ui.tab("AI 流水線", icon="account_tree")
            tab_coolpc = ui.tab("原價屋報價", icon="price_change")
            tab_security = ui.tab("安全性", icon="lock")
            tab_test = ui.tab("測試", icon="science")
            tab_logs = ui.tab("日誌", icon="article")

        with ui.tab_panels(tabs, value=tab_overview).classes("w-full"):

            # ---- 總覽 ----
            with ui.tab_panel(tab_overview):
                with ui.card().classes("w-full"):
                    ui.label("啟動服務").classes("text-lg font-bold")
                    with ui.row().classes("items-center gap-2"):
                        refs["start_btn"] = ui.button("啟動 Tunnel", icon="play_arrow", on_click=start_tunnel).props("color=positive")
                        ui.button("停止 Tunnel", icon="stop", on_click=stop_tunnel).props("color=negative outline")
                        ui.button("重新整理狀態", icon="sync", on_click=lambda: (refresh_status(), refresh_logs())).props("flat")
                    ui.label("把下面網址貼到 LINE Developers 的 Webhook URL（固定不變）：").classes("text-sm text-grey-7 mt-2")
                    refs["webhook_input"] = ui.input(
                        "Webhook URL", value="（尚未設定對外網域）",
                    ).classes("w-full").props("readonly")
                with ui.card().classes("w-full"):
                    ui.label("快速指引").classes("text-lg font-bold")
                    ui.markdown(
                        "1. 到 **Cloudflare** 分頁完成安裝精靈（登入 / 建立 / 綁定）。\n"
                        "2. 到 **LINE** 分頁填入金鑰。\n"
                        "3. 到 **Ollama** 分頁選擇模型。\n"
                        "4. 回此頁按「啟動 Tunnel」，把 Webhook URL 貼到 LINE 後台。\n"
                        "5. （選用）到 **SQL Server** 分頁設定資料庫連線。"
                    ).classes("text-sm text-grey-8")

            # ---- Cloudflare ----
            with ui.tab_panel(tab_cf):
                with ui.card().classes("w-full"):
                    ui.label("Cloudflare Tunnel 安裝精靈").classes("text-lg font-bold")
                    ui.markdown(
                        "需先把網域託管在 **Cloudflare**（網域的 nameserver 指向 Cloudflare），"
                        "並安裝 cloudflared 執行檔。Windows 安裝指令："
                    ).classes("text-sm text-grey-8")
                    ui.code("winget install --id Cloudflare.cloudflared", language="powershell").classes("w-full")

                    with ui.row().classes("gap-2 flex-wrap mt-2"):
                        refs["wiz_install"] = ui.badge("偵測中").props("color=grey")
                        refs["wiz_login"] = ui.badge("偵測中").props("color=grey")
                        refs["wiz_tunnel"] = ui.badge("偵測中").props("color=grey")
                    refs["wiz_exe"] = ui.label("").classes("text-xs text-grey-6")

                    refs["cloudflared_path_input"] = ui.input(
                        "cloudflared 執行檔路徑（在 PATH 上時填 cloudflared 即可）",
                        value=settings.get("CLOUDFLARED_PATH", "cloudflared"),
                    ).classes("w-full")

                    with ui.row().classes("w-full items-center gap-2"):
                        refs["tunnel_name_input"] = ui.input(
                            "Tunnel 名稱", value=settings.get("CF_TUNNEL_NAME", "linebot"),
                        ).classes("flex-grow")
                        refs["domain_input"] = ui.input(
                            "對外網域（例如 bot.example.com）",
                            value=settings.get("PUBLIC_DOMAIN", ""),
                        ).classes("flex-grow")

                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.button("步驟1 登入 Cloudflare", icon="login", on_click=wizard_login).props("outline")
                        ui.button("步驟2 建立 Tunnel", icon="add_circle", on_click=wizard_create).props("outline")
                        ui.button("步驟3 綁定網域", icon="dns", on_click=wizard_route).props("outline")
                        ui.button("重新檢查", icon="refresh", on_click=lambda: (refresh_wizard(), refresh_logs())).props("flat")
                    ui.label(
                        "提示：步驟1 會開啟瀏覽器要你選網域並授權，完成後回到此頁按「重新檢查」。"
                    ).classes("text-xs text-grey-6")

            # ---- LINE ----
            with ui.tab_panel(tab_line):
                with ui.card().classes("w-full"):
                    ui.label("LINE 設定").classes("text-lg font-bold")
                    refs["token_input"] = ui.input(
                        "Channel Access Token",
                        value=settings.get("LINE_CHANNEL_ACCESS_TOKEN", ""),
                        password=True, password_toggle_button=True,
                    ).classes("w-full")
                    refs["secret_input"] = ui.input(
                        "Channel Secret",
                        value=settings.get("LINE_CHANNEL_SECRET", ""),
                        password=True, password_toggle_button=True,
                    ).classes("w-full")
                    ui.button("儲存全部設定", icon="save", on_click=save_all).props("color=primary")

                with ui.card().classes("w-full"):
                    ui.label("加好友檢查").classes("text-lg font-bold")
                    refs["require_friend"] = ui.switch(
                        "未加好友時不回覆，改傳加好友提示",
                        value=db._truthy(settings.get("LINE_REQUIRE_FRIEND", "true")),
                    )
                    refs["not_friend_msg"] = ui.textarea(
                        "尚未加好友時的自動回覆訊息",
                        value=settings.get("LINE_NOT_FRIEND_MESSAGE",
                                           "請先將我們加為好友後再傳訊息，謝謝！"),
                    ).classes("w-full")
                    ui.label(
                        "原理：收到訊息後以 LINE profile API 查詢，回 404 即視為尚未加好友。"
                    ).classes("text-xs text-grey-6")
                    ui.button("儲存全部設定", icon="save", on_click=save_all).props("color=primary")

            # ---- Ollama ----
            with ui.tab_panel(tab_ollama):
                with ui.card().classes("w-full"):
                    ui.label("Ollama 模型設定").classes("text-lg font-bold")
                    refs["ollama_url_input"] = ui.input(
                        "Ollama API 位址",
                        value=settings.get("OLLAMA_BASE_URL", "http://localhost:11434"),
                    ).classes("w-full")
                    with ui.row().classes("w-full items-center gap-2"):
                        models = ollama.list_models(settings.get("OLLAMA_BASE_URL", ""))
                        if settings.get("OLLAMA_MODEL") and settings["OLLAMA_MODEL"] not in models:
                            models = models + [settings["OLLAMA_MODEL"]]
                        refs["model_select"] = ui.select(
                            options=models or [settings.get("OLLAMA_MODEL", "")],
                            value=settings.get("OLLAMA_MODEL", ""),
                            label="模型",
                        ).classes("flex-grow")
                        ui.button("重新載入模型", icon="refresh", on_click=reload_models).props("outline")
                    refs["prompt_input"] = ui.textarea(
                        "系統提示詞（System Prompt）",
                        value=settings.get("OLLAMA_SYSTEM_PROMPT", ""),
                    ).classes("w-full")
                    refs["memory_enabled"] = ui.switch(
                        "啟用跨對話記憶（將過去對話壓縮成精華，下次對談前載入）",
                        value=memory.is_enabled(settings),
                    )
                    ui.label(
                        f"記憶儲存於：{memory.memory_dir(settings)}"
                    ).classes("text-xs text-grey-6")
                    ui.button("儲存全部設定", icon="save", on_click=save_all).props("color=primary")

            # ---- SQL Server ----
            with ui.tab_panel(tab_sql):
                with ui.card().classes("w-full"):
                    ui.label("SQL Server 連線").classes("text-lg font-bold")
                    if not db.PYODBC_AVAILABLE:
                        ui.markdown(
                            "⚠️ 尚未安裝 **pyodbc**。請先執行 `pip install pyodbc`，"
                            "並安裝 [Microsoft ODBC Driver for SQL Server]"
                            "(https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)。"
                        ).classes("text-sm text-negative")
                    refs["sql_enabled"] = ui.switch(
                        "啟用 SQL Server 連線",
                        value=db.is_enabled(settings),
                    )
                    with ui.row().classes("w-full items-center gap-2"):
                        refs["sql_host"] = ui.input(
                            "主機", value=settings.get("SQLSERVER_HOST", "localhost"),
                        ).classes("flex-grow")
                        refs["sql_port"] = ui.number(
                            "埠", value=int(settings.get("SQLSERVER_PORT", "1433") or "1433"), format="%d",
                        ).classes("w-32")
                    refs["sql_database"] = ui.input(
                        "資料庫名稱", value=settings.get("SQLSERVER_DATABASE", ""),
                    ).classes("w-full")
                    with ui.row().classes("w-full items-center gap-2"):
                        refs["sql_user"] = ui.input(
                            "帳號（留空用 Windows 整合驗證）",
                            value=settings.get("SQLSERVER_USER", ""),
                        ).classes("flex-grow")
                        refs["sql_password"] = ui.input(
                            "密碼",
                            value=settings.get("SQLSERVER_PASSWORD", ""),
                            password=True, password_toggle_button=True,
                        ).classes("flex-grow")
                    with ui.row().classes("w-full items-center gap-2"):
                        drivers = db.list_drivers()
                        cur_driver = settings.get("SQLSERVER_DRIVER", "")
                        if cur_driver and cur_driver not in drivers:
                            drivers = drivers + [cur_driver]
                        refs["sql_driver"] = ui.select(
                            options=drivers or [cur_driver or "ODBC Driver 17 for SQL Server"],
                            value=cur_driver or (drivers[0] if drivers else "ODBC Driver 17 for SQL Server"),
                            label="ODBC 驅動",
                        ).classes("flex-grow")
                        ui.button("重新載入驅動", icon="refresh", on_click=sql_reload_drivers).props("outline")
                    refs["sql_encrypt"] = ui.switch(
                        "加密連線（Driver 18 預設要求）",
                        value=db._truthy(settings.get("SQLSERVER_ENCRYPT", "false")),
                    )
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.button("測試連線", icon="link", on_click=sql_test_connection).props("color=primary")
                        ui.button("初始化資料表", icon="build", on_click=sql_init_schema).props("color=secondary")
                        ui.button("儲存全部設定", icon="save", on_click=save_all).props("outline")

                with ui.card().classes("w-full"):
                    ui.label("資料表結構").classes("text-lg font-bold")
                    ui.markdown(
                        "「初始化資料表」會在所選資料庫建立以下資料表（可重複執行，不會覆蓋既有資料）：\n"
                        "- **customers**：客戶訊息（以 LINE userId 為主鍵，含暱稱、是否好友）\n"
                        "- **purchases**：購買訊息（客戶的購買紀錄）\n"
                        "- **parts_prices**：零件價格（零件編號、類別、品牌、品名、單價、庫存）\n"
                        "- **conversations**：對話紀錄（供產生跨對話記憶精華）"
                    ).classes("text-sm text-grey-8")

                with ui.card().classes("w-full"):
                    ui.label("查詢測試（唯讀，最多顯示 50 列）").classes("text-lg font-bold")
                    refs["sql_query"] = ui.textarea(
                        "SQL 查詢", value="SELECT TOP 5 name FROM sys.tables;",
                    ).classes("w-full")
                    ui.button("執行查詢", icon="play_arrow", on_click=sql_run_query).props("color=primary")
                    refs["sql_result"] = ui.textarea("結果", value="").classes("w-full").props(
                        "readonly autogrow input-style='font-family: monospace'"
                    )

            # ---- AI 流水線 ----
            with ui.tab_panel(tab_pipeline):
                pipe_models = ollama.list_models(settings.get("OLLAMA_BASE_URL", ""))
                with ui.card().classes("w-full"):
                    ui.label("四層 AI 流水線").classes("text-lg font-bold")
                    ui.markdown(
                        "啟用後，每則訊息會依序經過四個角色（可各自指定模型，留空則用 Ollama 分頁的主模型），"
                        "最大限度降低錯誤：\n"
                        "1. **語言理解大師**：把顧客的話解析成結構化小抄（含類別、品牌），"
                        "只擷取不臆測。\n"
                        "2. **最強庫管**：用關鍵字＋類別查 SQL 庫存（類別過濾可避免"
                        "「問顯卡卻撈到整台筆電」），同時查原價屋報價單比價，並彙整可選品牌與調貨天數。\n"
                        "3. **金牌銷售**：**自行判斷資訊是否足夠**——不足就**主動反問**逐步釐清"
                        "（先確認要顯卡或筆電，再確認品牌），足夠才報價。\n"
                        "4. **最嚴苛店長**：**檢核**第3層的反問或報價是否正確、有無亂猜，確認後才送出。\n"
                        "規則：所有報價皆為**未稅價**；**價高優先＝同一個品項的本店價與報價單價取較高者**，"
                        "不會把不同產品（單買零件 vs 整台電腦）拿來比價。"
                    ).classes("text-sm text-grey-8")
                    refs["pipeline_enabled"] = ui.switch(
                        "啟用四層流水線（關閉則使用單層回覆）",
                        value=pipeline.is_enabled(settings),
                    )

                _layer_meta = [
                    (1, "第1層 語言理解大師", "psychology"),
                    (2, "第2層 最強庫管", "inventory"),
                    (3, "第3層 金牌銷售", "support_agent"),
                    (4, "第4層 最嚴苛的店長", "verified_user"),
                ]
                for n, title, icon in _layer_meta:
                    with ui.card().classes("w-full"):
                        with ui.row().classes("items-center gap-2"):
                            ui.icon(icon)
                            ui.label(title).classes("text-lg font-bold")
                        cur_model = settings.get(f"PIPELINE_L{n}_MODEL", "")
                        opts = [""] + pipe_models
                        if cur_model and cur_model not in opts:
                            opts = opts + [cur_model]
                        refs[f"l{n}_model"] = ui.select(
                            options=opts, value=cur_model,
                            label="模型（留空＝用主模型）",
                        ).classes("w-full")
                        refs[f"l{n}_prompt"] = ui.textarea(
                            "系統提示詞",
                            value=settings.get(f"PIPELINE_L{n}_PROMPT", ""),
                        ).classes("w-full")
                with ui.card().classes("w-full"):
                    ui.button("儲存全部設定", icon="save", on_click=save_all).props("color=primary")
                    ui.separator()
                    ui.label("流水線測試（直接跑四層，不經過 LINE）").classes("text-md font-bold")
                    refs["pipe_test_input"] = ui.input("測試訊息，例如：我要買 RTX 4070").classes("w-full")
                    ui.button("執行流水線測試", icon="play_arrow", on_click=run_pipeline_test).props("color=secondary")
                    refs["pipe_test_output"] = ui.textarea("最終回覆", value="").classes("w-full").props(
                        "readonly autogrow"
                    )

            # ---- 原價屋報價 ----
            with ui.tab_panel(tab_coolpc):
                with ui.card().classes("w-full"):
                    ui.label("原價屋報價快取").classes("text-lg font-bold")
                    ui.markdown(
                        "為避免頻繁爬取拖垮原價屋網站、並加快查詢速度，報價會存進 SQL Server 的 "
                        "`coolpc_cache` 表。建議每天排程「更新報價快取」一次，AI 查詢時直接讀此表。\n"
                        "（需先在 SQL Server 分頁啟用連線並完成初始化資料表。）"
                    ).classes("text-sm text-grey-8")
                    refs["coolpc_enabled"] = ui.switch(
                        "缺貨時查原價屋報價",
                        value=pricing.is_enabled(settings),
                    )
                    refs["coolpc_url"] = ui.input(
                        "原價屋報價頁網址",
                        value=settings.get("COOLPC_URL", pricing.COOLPC_URL),
                    ).classes("w-full")
                    refs["coolpc_count"] = ui.label("報價快取：—").classes("text-sm text-grey-7")
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.button("立即更新報價快取", icon="cloud_download", on_click=coolpc_refresh_cache).props("color=primary")
                        ui.button("儲存全部設定", icon="save", on_click=save_all).props("outline")
                with ui.card().classes("w-full"):
                    ui.label("報價查詢測試").classes("text-lg font-bold")
                    refs["coolpc_keyword"] = ui.input("關鍵字，例如：4070").classes("w-full")
                    ui.button("查詢", icon="search", on_click=coolpc_search_test).props("color=secondary")
                    refs["coolpc_result"] = ui.textarea("結果", value="").classes("w-full").props(
                        "readonly autogrow input-style='font-family: monospace'"
                    )

            # ---- 安全性 ----
            with ui.tab_panel(tab_security):
                with ui.card().classes("w-full"):
                    ui.label("控制台密碼").classes("text-lg font-bold")
                    refs["password_input"] = ui.input(
                        "控制台登入密碼（留空則沿用目前密碼）",
                        password=True, password_toggle_button=True,
                    ).classes("w-full")
                    ui.label(
                        f"目前綁定位址：{settings.get('WEBUI_HOST', '127.0.0.1')}:"
                        f"{settings.get('WEBUI_PORT', '8080')}"
                    ).classes("text-sm text-grey-7")
                    ui.markdown(
                        "綁定 `0.0.0.0` 時控制台可被同網段裝置存取，**務必設定強密碼**。"
                        "修改綁定位址需編輯 `.env` 的 `WEBUI_HOST` 後重啟。"
                    ).classes("text-sm text-grey-8")
                    ui.button("儲存全部設定", icon="save", on_click=save_all).props("color=primary")

            # ---- 測試 ----
            with ui.tab_panel(tab_test):
                with ui.card().classes("w-full"):
                    ui.label("測試對話（直接打本地模型，不需經過 LINE）").classes("text-lg font-bold")
                    refs["test_input"] = ui.input("輸入測試訊息").classes("w-full")
                    ui.button("送出測試", icon="send", on_click=run_test_chat).props("color=primary")
                    refs["test_output"] = ui.textarea("模型回覆", value="").classes("w-full").props("readonly autogrow")

            # ---- 日誌 ----
            with ui.tab_panel(tab_logs):
                with ui.card().classes("w-full"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label("即時日誌").classes("text-lg font-bold")
                        ui.button("清除", icon="delete", on_click=lambda: (clear_logs(), refresh_logs())).props("flat dense")
                    refs["log_area"] = ui.textarea(value="").classes("w-full").props(
                        "readonly autogrow input-style='font-family: monospace; min-height: 320px'"
                    )

    refresh_status()
    refresh_logs()
    refresh_coolpc_count()
    ui.timer(2.0, refresh_logs)
    ui.timer(5.0, refresh_status)

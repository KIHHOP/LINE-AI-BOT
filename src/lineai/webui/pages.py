"""
WebUI 頁面版面：
- /login          登入頁
- /               主控制台（安裝精靈 → LINE → Ollama → 啟動/狀態 → 測試 → 日誌）

UI 只負責呈現與呼叫；實際邏輯都在 config / ollama / line_api / tunnel。
"""

from nicegui import ui

from .. import config, ollama, tunnel
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
    # 先把目前畫面上的 cloudflared 路徑存起來，確保偵測得到
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
    settings["OLLAMA_BASE_URL"] = refs["ollama_url_input"].value.strip()
    settings["OLLAMA_MODEL"] = refs["model_select"].value or ""
    settings["OLLAMA_SYSTEM_PROMPT"] = refs["prompt_input"].value
    settings["PUBLIC_DOMAIN"] = refs["domain_input"].value.strip()
    settings["CF_TUNNEL_NAME"] = refs["tunnel_name_input"].value.strip()
    settings["CLOUDFLARED_PATH"] = refs["cloudflared_path_input"].value.strip()
    settings["WEBUI_PASSWORD"] = refs["password_input"].value.strip() or settings.get("WEBUI_PASSWORD", "")
    config.save_settings(settings)
    log("設定已儲存")
    ui.notify("設定已儲存", type="positive")
    refresh_status()


def run_test_chat():
    text = refs["test_input"].value.strip()
    if not text:
        ui.notify("請先輸入測試訊息", type="warning")
        return
    refs["test_output"].value = "模型思考中…"
    reply = ollama.ask(text, settings)
    refs["test_output"].value = reply
    refresh_logs()


# ---------------------------------------------------------------------------
# 主頁
# ---------------------------------------------------------------------------
@ui.page("/")
def main_page():
    ui.colors(primary="#06C755")

    with ui.header().classes("items-center"):
        ui.icon("smart_toy").classes("text-2xl")
        ui.label("LINE AI Bot 控制台").classes("text-xl font-bold")

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):

        # 狀態列
        with ui.card().classes("w-full"):
            ui.label("服務狀態").classes("text-lg font-bold")
            with ui.row().classes("gap-2 flex-wrap"):
                refs["ollama_badge"] = ui.badge("Ollama：檢查中").props("color=grey")
                refs["tunnel_badge"] = ui.badge("Tunnel：未啟動").props("color=grey")
                refs["line_badge"] = ui.badge("LINE 金鑰：未設定").props("color=warning")

        # ① 安裝精靈
        with ui.card().classes("w-full"):
            ui.label("① Cloudflare Tunnel 安裝精靈").classes("text-lg font-bold")
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

        # ② LINE 設定
        with ui.card().classes("w-full"):
            ui.label("② LINE 設定").classes("text-lg font-bold")
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

        # ③ Ollama 設定
        with ui.card().classes("w-full"):
            ui.label("③ Ollama 模型設定").classes("text-lg font-bold")
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

        # ④ 啟動與 Webhook
        with ui.card().classes("w-full"):
            ui.label("④ 啟動服務").classes("text-lg font-bold")
            with ui.row().classes("items-center gap-2"):
                refs["start_btn"] = ui.button("啟動 Tunnel", icon="play_arrow", on_click=start_tunnel).props("color=positive")
                ui.button("停止 Tunnel", icon="stop", on_click=stop_tunnel).props("color=negative outline")
            ui.label("把下面網址貼到 LINE Developers 的 Webhook URL（固定不變）：").classes("text-sm text-grey-7 mt-2")
            refs["webhook_input"] = ui.input(
                "Webhook URL", value="（尚未設定對外網域）",
            ).classes("w-full").props("readonly")

        # 設定操作 + 安全性
        with ui.card().classes("w-full"):
            ui.label("⑤ 控制台密碼與儲存").classes("text-lg font-bold")
            refs["password_input"] = ui.input(
                "控制台登入密碼（留空則沿用目前密碼）",
                password=True, password_toggle_button=True,
            ).classes("w-full")
            with ui.row().classes("w-full gap-2"):
                ui.button("儲存全部設定", icon="save", on_click=save_all).props("color=primary")
                ui.button("重新整理狀態", icon="sync", on_click=lambda: (refresh_status(), refresh_logs())).props("outline")

        # ⑥ 測試對話
        with ui.card().classes("w-full"):
            ui.label("⑥ 測試對話（直接打本地模型，不需經過 LINE）").classes("text-lg font-bold")
            refs["test_input"] = ui.input("輸入測試訊息").classes("w-full")
            ui.button("送出測試", icon="send", on_click=run_test_chat).props("color=primary")
            refs["test_output"] = ui.textarea("模型回覆", value="").classes("w-full").props("readonly autogrow")

        # 即時日誌
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("即時日誌").classes("text-lg font-bold")
                ui.button("清除", icon="delete", on_click=lambda: (clear_logs(), refresh_logs())).props("flat dense")
            refs["log_area"] = ui.textarea(value="").classes("w-full").props(
                "readonly autogrow input-style='font-family: monospace; min-height: 180px'"
            )

    refresh_status()
    refresh_logs()
    ui.timer(2.0, refresh_logs)
    ui.timer(5.0, refresh_status)

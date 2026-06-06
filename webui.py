"""
LINE AI 聊天機器人 — WebUI 控制台
以 NiceGUI（FastAPI + uvicorn）打造，所有操作都在網頁完成：
- 填寫 / 儲存 LINE 與 Ollama 設定
- 從本地 Ollama 自動帶出模型清單
- 一鍵啟動 / 停止 Cloudflare Tunnel，對外為固定網域
- 即時日誌
- 內建測試對話（不需經過 LINE 即可驗證模型）

LINE Webhook 端點與本 UI 掛在同一個服務、同一個埠。
對外網址為固定網域（例如 https://bot.linebotnanocat.com），
由 Cloudflare Tunnel 反向代理到本機，Webhook URL 設定一次即可、不會變動。

啟動：
    python webui.py
然後瀏覽器開 http://localhost:8080
"""

import json

from fastapi import Request, Response
from nicegui import app as nicegui_app, ui

import core

# ---------------------------------------------------------------------------
# 全域狀態
# ---------------------------------------------------------------------------
settings = core.load_settings()


# ---------------------------------------------------------------------------
# LINE Webhook 端點（掛在 NiceGUI 底層的 FastAPI）
# ---------------------------------------------------------------------------
@nicegui_app.get("/callback")
async def callback_health():
    return Response(content="LINE webhook endpoint is alive.", status_code=200)


@nicegui_app.post("/callback")
async def line_callback(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    secret = settings.get("LINE_CHANNEL_SECRET", "")
    if not core.verify_line_signature(secret, body, signature):
        core.log("Webhook 簽章驗證失敗（檢查 Channel Secret）", "WARNING")
        return Response(content="Bad signature", status_code=400)

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return Response(content="Bad request", status_code=400)

    access_token = settings.get("LINE_CHANNEL_ACCESS_TOKEN", "")

    for event in payload.get("events", []):
        if event.get("type") == "message" and event.get("message", {}).get("type") == "text":
            user_text = event["message"]["text"]
            reply_token = event.get("replyToken", "")
            user_id = event.get("source", {}).get("userId", "")
            core.log(f"收到訊息：{user_text}")

            reply_text = core.ask_ollama(user_text, settings)

            ok = core.line_reply(access_token, reply_token, reply_text)
            if not ok and user_id:
                core.log("reply 失敗，改用 push 補送", "WARNING")
                core.line_push(access_token, user_id, reply_text)

    return Response(content="OK", status_code=200)


# ---------------------------------------------------------------------------
# Cloudflare Tunnel 控制
# ---------------------------------------------------------------------------
def start_tunnel():
    ok, msg = core.start_cloudflared(settings)
    ui.notify(msg, type="positive" if ok else "warning")
    refresh_status()


def stop_tunnel():
    ok, msg = core.stop_cloudflared()
    ui.notify(msg, type="positive" if ok else "warning")
    refresh_status()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
# UI 元件參照（供刷新用）
ui_refs = {}


def refresh_status():
    """更新狀態列：Ollama、Cloudflare Tunnel、Webhook URL。"""
    ollama_ok = core.check_ollama(settings.get("OLLAMA_BASE_URL", ""))
    ui_refs["ollama_badge"].text = "Ollama：連線正常" if ollama_ok else "Ollama：無法連線"
    ui_refs["ollama_badge"].props(f'color={"positive" if ollama_ok else "negative"}')

    cf_on = core.is_tunnel_running()
    ui_refs["tunnel_badge"].text = "Cloudflare Tunnel：執行中" if cf_on else "Cloudflare Tunnel：未啟動"
    ui_refs["tunnel_badge"].props(f'color={"positive" if cf_on else "grey"}')

    url = core.webhook_url(settings)
    ui_refs["webhook_input"].value = url or "（尚未設定對外網域）"

    line_ready = bool(settings.get("LINE_CHANNEL_ACCESS_TOKEN") and settings.get("LINE_CHANNEL_SECRET"))
    ui_refs["line_badge"].text = "LINE 金鑰：已設定" if line_ready else "LINE 金鑰：未設定"
    ui_refs["line_badge"].props(f'color={"positive" if line_ready else "warning"}')


def refresh_logs():
    ui_refs["log_area"].value = "\n".join(core.get_logs())


def reload_models():
    models = core.list_ollama_models(settings.get("OLLAMA_BASE_URL", ""))
    if models:
        ui_refs["model_select"].options = models
        if settings.get("OLLAMA_MODEL") not in models:
            ui_refs["model_select"].value = models[0]
        ui_refs["model_select"].update()
        ui.notify(f"已載入 {len(models)} 個模型", type="positive")
    else:
        ui.notify("找不到模型，請確認 Ollama 已啟動並下載模型", type="warning")


def save_all():
    settings["LINE_CHANNEL_ACCESS_TOKEN"] = ui_refs["token_input"].value.strip()
    settings["LINE_CHANNEL_SECRET"] = ui_refs["secret_input"].value.strip()
    settings["OLLAMA_BASE_URL"] = ui_refs["ollama_url_input"].value.strip()
    settings["OLLAMA_MODEL"] = ui_refs["model_select"].value or ""
    settings["OLLAMA_SYSTEM_PROMPT"] = ui_refs["prompt_input"].value
    settings["PUBLIC_DOMAIN"] = ui_refs["domain_input"].value.strip()
    settings["CF_TUNNEL_NAME"] = ui_refs["tunnel_name_input"].value.strip()
    settings["CLOUDFLARED_PATH"] = ui_refs["cloudflared_path_input"].value.strip()
    settings["PORT"] = str(ui_refs["port_input"].value or 8080)
    core.save_settings(settings)
    core.log("設定已儲存")
    ui.notify("設定已儲存", type="positive")
    refresh_status()


def run_test_chat():
    text = ui_refs["test_input"].value.strip()
    if not text:
        ui.notify("請先輸入測試訊息", type="warning")
        return
    ui_refs["test_output"].value = "模型思考中…"
    reply = core.ask_ollama(text, settings)
    ui_refs["test_output"].value = reply
    refresh_logs()


@ui.page("/")
def main_page():
    ui.colors(primary="#06C755")  # LINE 綠

    with ui.header().classes("items-center"):
        ui.icon("smart_toy").classes("text-2xl")
        ui.label("LINE AI 機器人 控制台").classes("text-xl font-bold")

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):

        # 狀態列
        with ui.card().classes("w-full"):
            ui.label("服務狀態").classes("text-lg font-bold")
            with ui.row().classes("gap-2 flex-wrap"):
                ui_refs["ollama_badge"] = ui.badge("Ollama：檢查中").props("color=grey")
                ui_refs["tunnel_badge"] = ui.badge("Cloudflare Tunnel：未啟動").props("color=grey")
                ui_refs["line_badge"] = ui.badge("LINE 金鑰：未設定").props("color=warning")

        # LINE 設定
        with ui.card().classes("w-full"):
            ui.label("① LINE 設定").classes("text-lg font-bold")
            ui_refs["token_input"] = ui.input(
                "Channel Access Token",
                value=settings.get("LINE_CHANNEL_ACCESS_TOKEN", ""),
                password=True, password_toggle_button=True,
            ).classes("w-full")
            ui_refs["secret_input"] = ui.input(
                "Channel Secret",
                value=settings.get("LINE_CHANNEL_SECRET", ""),
                password=True, password_toggle_button=True,
            ).classes("w-full")

        # Ollama 設定
        with ui.card().classes("w-full"):
            ui.label("② Ollama 模型設定").classes("text-lg font-bold")
            ui_refs["ollama_url_input"] = ui.input(
                "Ollama API 位址",
                value=settings.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            ).classes("w-full")
            with ui.row().classes("w-full items-center gap-2"):
                models = core.list_ollama_models(settings.get("OLLAMA_BASE_URL", ""))
                if settings.get("OLLAMA_MODEL") and settings["OLLAMA_MODEL"] not in models:
                    models = models + [settings["OLLAMA_MODEL"]]
                ui_refs["model_select"] = ui.select(
                    options=models or [settings.get("OLLAMA_MODEL", "")],
                    value=settings.get("OLLAMA_MODEL", ""),
                    label="模型",
                ).classes("flex-grow")
                ui.button("重新載入模型", icon="refresh", on_click=reload_models).props("outline")
            ui_refs["prompt_input"] = ui.textarea(
                "系統提示詞（System Prompt）",
                value=settings.get("OLLAMA_SYSTEM_PROMPT", ""),
            ).classes("w-full")

        # Cloudflare Tunnel 設定與控制
        with ui.card().classes("w-full"):
            ui.label("③ Cloudflare Tunnel 穿透").classes("text-lg font-bold")
            ui.label(
                "需先安裝 cloudflared 並完成一次性設定（login / tunnel create / route dns）。"
                "詳見 README。"
            ).classes("text-sm text-grey-7")
            ui_refs["domain_input"] = ui.input(
                "對外網域（例如 bot.linebotnanocat.com）",
                value=settings.get("PUBLIC_DOMAIN", ""),
            ).classes("w-full")
            with ui.row().classes("w-full items-center gap-2"):
                ui_refs["tunnel_name_input"] = ui.input(
                    "Tunnel 名稱", value=settings.get("CF_TUNNEL_NAME", "linebot"),
                ).classes("flex-grow")
                ui_refs["port_input"] = ui.number(
                    "服務埠", value=int(settings.get("PORT", "8080") or "8080"), format="%d",
                ).classes("w-32")
            ui_refs["cloudflared_path_input"] = ui.input(
                "cloudflared 執行檔路徑（在 PATH 上時填 cloudflared 即可）",
                value=settings.get("CLOUDFLARED_PATH", "cloudflared"),
            ).classes("w-full")
            with ui.row().classes("items-center gap-2"):
                ui.button("啟動 Tunnel", icon="play_arrow", on_click=start_tunnel).props("color=positive")
                ui.button("停止 Tunnel", icon="stop", on_click=stop_tunnel).props("color=negative outline")
            ui.label("把下面網址貼到 LINE Developers 的 Webhook URL（固定不變）：").classes("text-sm text-grey-7 mt-2")
            ui_refs["webhook_input"] = ui.input(
                "Webhook URL", value="（尚未設定對外網域）",
            ).classes("w-full").props("readonly")

        # 操作按鈕
        with ui.row().classes("w-full gap-2"):
            ui.button("儲存全部設定", icon="save", on_click=save_all).props("color=primary")
            ui.button("重新整理狀態", icon="sync", on_click=lambda: (refresh_status(), refresh_logs())).props("outline")

        # 測試對話
        with ui.card().classes("w-full"):
            ui.label("④ 測試對話（直接打本地模型，不需經過 LINE）").classes("text-lg font-bold")
            ui_refs["test_input"] = ui.input("輸入測試訊息").classes("w-full")
            ui.button("送出測試", icon="send", on_click=run_test_chat).props("color=primary")
            ui_refs["test_output"] = ui.textarea("模型回覆", value="").classes("w-full").props("readonly autogrow")

        # 即時日誌
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("即時日誌").classes("text-lg font-bold")
                ui.button("清除", icon="delete", on_click=lambda: (core.clear_logs(), refresh_logs())).props("flat dense")
            ui_refs["log_area"] = ui.textarea(value="").classes("w-full").props(
                "readonly autogrow input-style='font-family: monospace; min-height: 180px'"
            )

    # 初始化狀態 + 定時刷新
    refresh_status()
    refresh_logs()
    ui.timer(2.0, refresh_logs)
    ui.timer(5.0, refresh_status)


def on_shutdown():
    """關閉時停掉 cloudflared 子行程。"""
    try:
        core.stop_cloudflared()
    except Exception:
        pass


nicegui_app.on_shutdown(on_shutdown)


if __name__ in {"__main__", "__mp_main__"}:
    core.log("WebUI 啟動中…")
    port = int(settings.get("PORT", "8080") or "8080")
    ui.run(
        host="0.0.0.0",
        port=port,
        title="LINE AI 機器人 控制台",
        reload=False,
        show=False,
    )

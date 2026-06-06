"""
LINE AI 聊天機器人 — WebUI 控制台
以 NiceGUI（FastAPI + uvicorn）打造，所有操作都在網頁完成：
- 填寫 / 儲存 LINE 與 Ollama 設定
- 從本地 Ollama 自動帶出模型清單
- 一鍵啟動 / 停止 Ngrok，並顯示要貼到 LINE 的 Webhook URL
- 即時日誌
- 內建測試對話（不需經過 LINE 即可驗證模型）

LINE Webhook 端點與本 UI 掛在同一個服務、同一個埠。

啟動：
    python webui.py
然後瀏覽器開 http://localhost:8080
"""

import json

from fastapi import Request, Response
from nicegui import app as nicegui_app, ui

import core

try:
    from pyngrok import ngrok, conf as ngrok_conf
    PYNGROK_AVAILABLE = True
except Exception:
    PYNGROK_AVAILABLE = False

# ---------------------------------------------------------------------------
# 全域狀態
# ---------------------------------------------------------------------------
settings = core.load_settings()

state = {
    "ngrok_tunnel": None,     # pyngrok tunnel 物件
    "public_url": "",         # ngrok 對外網址
}


def webhook_url() -> str:
    return f"{state['public_url']}/callback" if state["public_url"] else ""


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
        # 臨時除錯：比對收到與計算出的簽章，協助找出金鑰不符問題
        import hmac as _hmac, hashlib as _hashlib, base64 as _base64
        computed = ""
        if secret:
            _mac = _hmac.new(secret.encode("utf-8"), body, _hashlib.sha256).digest()
            computed = _base64.b64encode(_mac).decode("utf-8")
        core.log(
            f"簽章不符 | secret長度={len(secret)} | 收到簽章={signature} | "
            f"計算簽章={computed} | body={body.decode('utf-8', 'replace')[:200]}",
            "WARNING",
        )
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
# Ngrok 控制
# ---------------------------------------------------------------------------
def start_ngrok():
    if not PYNGROK_AVAILABLE:
        ui.notify("pyngrok 未安裝", type="negative")
        return
    if state["ngrok_tunnel"]:
        ui.notify("Ngrok 已在執行", type="warning")
        return
    try:
        authtoken = settings.get("NGROK_AUTHTOKEN", "").strip()
        if authtoken:
            ngrok_conf.get_default().auth_token = authtoken

        # 先清掉任何殘留的 ngrok（避免免費版固定網域被舊連線佔用，ERR_NGROK_334）
        try:
            for t in ngrok.get_tunnels():
                ngrok.disconnect(t.public_url)
        except Exception:
            pass
        try:
            ngrok.kill()
        except Exception:
            pass

        port = int(settings.get("PORT", "8080") or "8080")
        tunnel = ngrok.connect(port, "http")
        state["ngrok_tunnel"] = tunnel
        state["public_url"] = tunnel.public_url
        core.log(f"Ngrok 已啟動：{tunnel.public_url}")
        ui.notify("Ngrok 已啟動", type="positive")
    except Exception as e:
        msg = str(e)
        core.log(f"Ngrok 啟動失敗：{msg}", "ERROR")
        if "ERR_NGROK_334" in msg or "already online" in msg:
            hint = (
                "偵測到舊的 ngrok 連線仍佔用網域。請先按「停止 Ngrok」，"
                "或到 https://dashboard.ngrok.com/agents 結束既有 Agent 後再試。"
            )
            core.log(hint, "WARNING")
            ui.notify(hint, type="negative", timeout=8000)
        else:
            ui.notify(f"Ngrok 啟動失敗：{msg}", type="negative")
    refresh_status()


def stop_ngrok():
    if not state["ngrok_tunnel"]:
        ui.notify("Ngrok 尚未啟動", type="warning")
        return
    try:
        ngrok.disconnect(state["ngrok_tunnel"].public_url)
        ngrok.kill()
    except Exception as e:
        core.log(f"停止 Ngrok 時發生例外：{e}", "WARNING")
    state["ngrok_tunnel"] = None
    state["public_url"] = ""
    core.log("Ngrok 已停止")
    ui.notify("Ngrok 已停止", type="positive")
    refresh_status()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
# UI 元件參照（供刷新用）
ui_refs = {}


def refresh_status():
    """更新狀態列：Ollama、Ngrok、Webhook URL。"""
    ollama_ok = core.check_ollama(settings.get("OLLAMA_BASE_URL", ""))
    ui_refs["ollama_badge"].text = "Ollama：連線正常" if ollama_ok else "Ollama：無法連線"
    ui_refs["ollama_badge"].props(f'color={"positive" if ollama_ok else "negative"}')

    ng_on = state["ngrok_tunnel"] is not None
    ui_refs["ngrok_badge"].text = "Ngrok：執行中" if ng_on else "Ngrok：未啟動"
    ui_refs["ngrok_badge"].props(f'color={"positive" if ng_on else "grey"}')

    url = webhook_url()
    ui_refs["webhook_input"].value = url or "（尚未啟動 Ngrok）"

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
    settings["NGROK_AUTHTOKEN"] = ui_refs["ngrok_token_input"].value.strip()
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
                ui_refs["ngrok_badge"] = ui.badge("Ngrok：未啟動").props("color=grey")
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

        # Ngrok 設定與控制
        with ui.card().classes("w-full"):
            ui.label("③ Ngrok 穿透").classes("text-lg font-bold")
            ui_refs["ngrok_token_input"] = ui.input(
                "Ngrok Authtoken（首次使用需填）",
                value=settings.get("NGROK_AUTHTOKEN", ""),
                password=True, password_toggle_button=True,
            ).classes("w-full")
            with ui.row().classes("items-center gap-2"):
                ui_refs["port_input"] = ui.number(
                    "服務埠", value=int(settings.get("PORT", "8080") or "8080"), format="%d",
                ).classes("w-32")
                ui.button("啟動 Ngrok", icon="play_arrow", on_click=start_ngrok).props("color=positive")
                ui.button("停止 Ngrok", icon="stop", on_click=stop_ngrok).props("color=negative outline")
            ui.label("把下面網址貼到 LINE Developers 的 Webhook URL：").classes("text-sm text-grey-7 mt-2")
            ui_refs["webhook_input"] = ui.input(
                "Webhook URL", value="（尚未啟動 Ngrok）",
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
    """關閉時清掉 ngrok。"""
    if state["ngrok_tunnel"] and PYNGROK_AVAILABLE:
        try:
            ngrok.kill()
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

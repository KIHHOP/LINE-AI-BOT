"""
WebUI 伺服器組裝：
- 共用設定狀態
- LINE Webhook 端點（/callback），把阻塞的模型呼叫丟到執行緒避免卡住事件迴圈
- 登入驗證（session middleware + 未登入導向 /login）
- 啟動入口 run()，預設只綁本機

安全性：
- WEBUI_HOST 預設 127.0.0.1，只有本機能存取。
- 所有頁面（除登入頁與 LINE 的 /callback）都需登入。
- 登入密碼來自 WEBUI_PASSWORD；未設定時啟動會自動產生並印在終端機。
"""

import json
import secrets

from fastapi import Request, Response
from fastapi.responses import RedirectResponse
from nicegui import app as nicegui_app, ui
from starlette.middleware.base import BaseHTTPMiddleware

from .. import config, line_api, ollama, tunnel
from ..logbuffer import log
import anyio

# ---------------------------------------------------------------------------
# 共用狀態
# ---------------------------------------------------------------------------
settings: dict = config.load_settings()

# 不需登入即可存取的路徑（LINE Webhook 必須公開；登入頁本身亦然）
PUBLIC_PATHS = {"/login", "/callback"}
# NiceGUI 與靜態資源前綴
PUBLIC_PREFIXES = ("/_nicegui", "/static")


def get_setting(key: str) -> str:
    return settings.get(key, "")


# ---------------------------------------------------------------------------
# 登入驗證
# ---------------------------------------------------------------------------
class AuthMiddleware(BaseHTTPMiddleware):
    """未登入者導向 /login；放行公開路徑與靜態資源。

    使用 NiceGUI 的 app.storage.user 保存登入狀態（需設定 storage_secret）。
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        try:
            authed = nicegui_app.storage.user.get("authenticated", False)
        except Exception:
            authed = False
        if authed:
            return await call_next(request)
        return RedirectResponse("/login")


def current_password() -> str:
    return settings.get("WEBUI_PASSWORD", "")


def verify_password(candidate: str) -> bool:
    pw = current_password()
    if not pw:
        return False
    return secrets.compare_digest(candidate, pw)


# ---------------------------------------------------------------------------
# LINE Webhook 端點
# ---------------------------------------------------------------------------
@nicegui_app.get("/callback")
async def callback_health():
    return Response(content="LINE webhook endpoint is alive.", status_code=200)


@nicegui_app.post("/callback")
async def line_callback(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    secret = settings.get("LINE_CHANNEL_SECRET", "")
    if not line_api.verify_signature(secret, body, signature):
        log("Webhook 簽章驗證失敗（檢查 Channel Secret）", "WARNING")
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
            log(f"收到訊息：{user_text}")

            # 模型推論是同步且可能很久，丟到 worker thread 避免卡住事件迴圈
            reply_text = await anyio.to_thread.run_sync(ollama.ask, user_text, settings)

            ok = await anyio.to_thread.run_sync(
                line_api.reply, access_token, reply_token, reply_text
            )
            if not ok and user_id:
                log("reply 失敗，改用 push 補送", "WARNING")
                await anyio.to_thread.run_sync(
                    line_api.push, access_token, user_id, reply_text
                )

    return Response(content="OK", status_code=200)


# ---------------------------------------------------------------------------
# 關閉時清理
# ---------------------------------------------------------------------------
def _on_shutdown():
    try:
        tunnel.stop()
    except Exception:
        pass


nicegui_app.on_shutdown(_on_shutdown)


# ---------------------------------------------------------------------------
# 啟動
# ---------------------------------------------------------------------------
def run():
    """組裝中介層、註冊頁面並啟動服務。"""
    # 登入密碼：未設定則自動產生並印在終端機（不寫回 .env，重啟會換新的）
    if not settings.get("WEBUI_PASSWORD"):
        generated = secrets.token_urlsafe(9)
        settings["WEBUI_PASSWORD"] = generated
        log("未設定 WEBUI_PASSWORD，已產生臨時登入密碼（僅本次有效）：", "WARNING")
        log(f"    登入密碼：{generated}", "WARNING")
        log("建議到 WebUI 設定固定密碼，或寫入 .env 的 WEBUI_PASSWORD。", "WARNING")

    # 登入驗證中介層；登入狀態由 NiceGUI app.storage.user 保存
    # （storage_secret 已在 ui.run 設定，NiceGUI 會自行掛上 session cookie 機制）
    nicegui_app.add_middleware(AuthMiddleware)

    # 註冊頁面（延後 import 避免循環相依）
    from . import pages  # noqa: F401

    host = settings.get("WEBUI_HOST", "127.0.0.1") or "127.0.0.1"
    port = int(settings.get("WEBUI_PORT", "8080") or "8080")

    log("WebUI 啟動中…")
    if host in ("127.0.0.1", "localhost"):
        log(f"WebUI 僅綁定本機：http://127.0.0.1:{port}")
    else:
        log(f"WebUI 綁定 {host}:{port}（可被同網段存取，請確認已設定強密碼）", "WARNING")

    ui.run(
        host=host,
        port=port,
        title="LINE AI Bot 控制台",
        reload=False,
        show=False,
        storage_secret=settings["WEBUI_SECRET_KEY"],
    )

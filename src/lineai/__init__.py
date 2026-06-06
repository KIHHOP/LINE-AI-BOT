"""
LINE AI Bot — 把 LINE 訊息轉接到本地 Ollama 模型，並以 Cloudflare Tunnel 對外。

套件結構：
- config      設定讀寫與預設值
- logbuffer   即時日誌緩衝（供 WebUI 顯示）
- ollama      Ollama 中轉（列模型 / 聊天）
- line_api    LINE 簽章驗證、回覆、推播
- tunnel      cloudflared 偵測、啟停、登入 / 建立 / 綁定網域
- webui       NiceGUI 控制台（含 LINE Webhook 端點與安裝精靈）
"""

__version__ = "0.1.0"

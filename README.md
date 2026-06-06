# LINE AI 聊天機器人（LINE × Ngrok × Ollama）附 WebUI 控制台

一個用 Python 實作的 LINE 聊天機器人。使用者在 LINE 傳文字訊息後，伺服器會呼叫**本地端的 Ollama 模型**產生回覆，再傳回 LINE。對外連線透過 **Ngrok** 把本機服務穿透到公網。

所有設定與啟動操作都集中在一個 **WebUI 控制台**完成，不需要手動敲終端機指令。

## 架構流程

```
使用者 (LINE App)
      │  傳送文字
      ▼
LINE Platform（雲端）
      │  Webhook (HTTPS POST /callback)
      ▼
Ngrok（公網 → 本機穿透）
      │
      ▼
本地服務 (webui.py, port 8080)
  ├─ WebUI 控制台（設定 / 啟停 / 日誌 / 測試）
  └─ /callback  LINE Webhook 端點
      │  POST /api/chat
      ▼
本地 Ollama (port 11434) → 產生回覆
      │
      ▼  回傳文字
本地服務 → LINE Platform → 使用者
```

## 檔案說明

| 檔案 | 說明 |
|------|------|
| `webui.py` | **主程式**：WebUI 控制台 + LINE Webhook 端點（NiceGUI） |
| `core.py` | 核心邏輯：設定讀寫、Ollama 中轉、LINE 簽章驗證與回覆 |
| `app.py` | 舊版純 CLI（Flask + line-bot-sdk），保留作參考，非必要 |
| `.env` | 設定檔（含金鑰，已被 .gitignore 排除） |
| `requirements.txt` | WebUI 版所需套件 |

## 技術選型

- **WebUI：NiceGUI**（MIT 授權，可商用）。底層為 FastAPI + uvicorn，能把控制台 UI 與 LINE Webhook 端點掛在同一服務、同一個埠，狀態管理直觀。
- **穿透：pyngrok**（MIT 授權），在 UI 內一鍵啟停 Ngrok。
- LINE 簽章驗證與回覆改用 `requests` + `hmac` 自行實作，不綁定特定 SDK 版本，UI 改金鑰即時生效。

> 注意：Ngrok 服務本身免費版有連線數與固定網域限制，正式商用建議採用 Ngrok 付費方案或自架反向代理。

---

## 事前準備

1. **Python 3.9+**
2. **Ollama**：到 <https://ollama.com> 下載安裝，並下載至少一個模型
   ```powershell
   ollama pull qwen35:latest
   ```
3. **Ngrok**：到 <https://ngrok.com> 註冊，取得 authtoken（待會貼進 WebUI）
4. **LINE 官方帳號 + Messaging API Channel**：在 <https://developers.line.biz> 建立

## 安裝

```powershell
# 在專案目錄下
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

---

## 啟動 WebUI

```powershell
.\venv\Scripts\python.exe webui.py
```

看到 `NiceGUI ready to go on http://localhost:8080` 後，瀏覽器開啟 <http://localhost:8080>。

### 在 WebUI 中操作（由上而下）

1. **服務狀態**：頂端顯示 Ollama 連線、Ngrok、LINE 金鑰是否就緒。
2. **① LINE 設定**：貼上 Channel Access Token 與 Channel Secret。
3. **② Ollama 模型設定**：確認 API 位址，按「重新載入模型」會自動帶出本機已下載的模型，選一個；可調整系統提示詞。
4. **③ Ngrok 穿透**：首次使用先貼上 Ngrok Authtoken，按「啟動 Ngrok」。啟動後「Webhook URL」欄會顯示對外網址（形如 `https://xxxx.ngrok-free.app/callback`）。
5. 按 **儲存全部設定**。
6. **④ 測試對話**：直接輸入訊息測試本地模型回覆，不需經過 LINE。
7. **即時日誌**：顯示 Webhook 收訊、模型呼叫、錯誤等即時訊息。

### 設定 LINE Webhook

1. 複製 WebUI 顯示的 Webhook URL。
2. 進入 LINE Developers Console > 你的 Channel > **Messaging API**。
3. 把網址貼到 **Webhook URL**（結尾為 `/callback`），開啟 **Use webhook**，按 **Verify**。
4. 到 LINE Official Account Manager 關閉「自動回覆訊息」與「歡迎訊息」。

完成後，用手機加該官方帳號為好友並傳訊息，即可收到本地模型產生的 AI 回覆。

---

## 常見問題

| 問題 | 解法 |
|------|------|
| 狀態列顯示「Ollama：無法連線」 | 確認 Ollama 已啟動（`ollama serve`），且 API 位址正確 |
| 模型下拉是空的 | 先 `ollama pull` 下載模型，再按「重新載入模型」 |
| 按「啟動 Ngrok」失敗 | 多半是 authtoken 未填或錯誤；到 ngrok dashboard 複製正確 token |
| LINE Verify 失敗 (400) | 檢查 Channel Secret 是否正確並已儲存 |
| LINE 收不到回覆 | 確認 Ollama、WebUI、Ngrok 都在執行，Webhook URL 結尾為 `/callback` |
| Ngrok 網址每次變動 | 免費版重啟會換網址，需重新更新 LINE Webhook URL；付費版可固定網域 |

## 注意事項

- 要正常運作時，需同時保持 **Ollama**、**WebUI（webui.py）**、**Ngrok（由 UI 啟動）** 三者執行。
- LINE 的 reply token 約 60 秒有效，若模型推論太久，程式會自動改用 push message 補送。
- `.env` 含密鑰，已列入 `.gitignore`，請勿提交到版本庫。

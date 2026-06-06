# LINE AI 聊天機器人（LINE × Cloudflare Tunnel × Ollama）附 WebUI 控制台

一個用 Python 實作的 LINE 聊天機器人。使用者在 LINE 傳文字訊息後，伺服器會呼叫**本地端的 Ollama 模型**產生回覆，再傳回 LINE。對外連線透過 **Cloudflare Tunnel** 把本機服務以**固定網域**穿透到公網並做反向代理。

所有設定與啟動操作都集中在一個 **WebUI 控制台**完成，不需要手動敲終端機指令。

## 架構流程

```
使用者 (LINE App)
      │  傳送文字
      ▼
LINE Platform（雲端）
      │  Webhook (HTTPS POST /callback)
      ▼
Cloudflare 邊緣（固定網域 + HTTPS 憑證）
      │  Cloudflare Tunnel（公網 → 本機反向代理）
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
| `webui.py` | **主程式**：WebUI 控制台 + LINE Webhook 端點（NiceGUI），並一鍵啟停 Cloudflare Tunnel |
| `core.py` | 核心邏輯：設定讀寫、Ollama 中轉、LINE 簽章驗證與回覆、cloudflared 子行程管理 |
| `app.py` | 舊版純 CLI（Flask + line-bot-sdk），保留作參考，非必要 |
| `.env` | 設定檔（含金鑰，已被 .gitignore 排除） |
| `requirements.txt` | WebUI 版所需套件 |

## 技術選型

- **WebUI：NiceGUI**（MIT 授權，可商用）。底層為 FastAPI + uvicorn，能把控制台 UI 與 LINE Webhook 端點掛在同一服務、同一個埠，狀態管理直觀。
- **穿透：Cloudflare Tunnel（cloudflared）**。免費、無連線數限制，對外為**固定網域**，由 Cloudflare 邊緣節點負責 HTTPS 憑證與反向代理。Webhook URL 設定一次即可，重啟不會變動。
- LINE 簽章驗證與回覆改用 `requests` + `hmac` 自行實作，不綁定特定 SDK 版本，UI 改金鑰即時生效。

> 與 Ngrok 相比，Cloudflare Tunnel 的最大好處是網域固定，不需要每次重啟都回 LINE 後台更新 Webhook URL。

---

## 事前準備

1. **Python 3.9+**
2. **Ollama**：到 <https://ollama.com> 下載安裝，並下載至少一個模型
   ```powershell
   ollama pull qwen3
   ```
3. **網域託管在 Cloudflare**：你的網域（例如 `linebotnanocat.com`）的 nameserver 需已指向 Cloudflare。
4. **cloudflared**：安裝 Cloudflare Tunnel 的執行檔
   ```powershell
   winget install --id Cloudflare.cloudflared
   ```
5. **LINE 官方帳號 + Messaging API Channel**：在 <https://developers.line.biz> 建立

## 安裝 Python 套件

```powershell
# 在專案目錄下
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

---

## Cloudflare Tunnel 一次性設定

以下指令只需執行一次，用來建立具名 tunnel 並把子網域指到它。以網域 `bot.linebotnanocat.com`、tunnel 名稱 `linebot` 為例：

```powershell
# 1. 登入（會開瀏覽器，選擇你的網域 zone 授權）
cloudflared tunnel login

# 2. 建立具名 tunnel（會在 C:\Users\<你>\.cloudflared\ 產生 <UUID>.json 憑證）
cloudflared tunnel create linebot

# 3. 把子網域指到此 tunnel（自動建立 DNS CNAME 記錄）
cloudflared tunnel route dns linebot bot.linebotnanocat.com
```

接著建立 tunnel 的設定檔，讓它知道要把流量轉到本機哪個埠。在 `C:\Users\<你>\.cloudflared\config.yml` 寫入：

```yaml
tunnel: linebot
credentials-file: C:\Users\<你>\.cloudflared\<UUID>.json

ingress:
  - hostname: bot.linebotnanocat.com
    service: http://localhost:8080
  - service: http_status:404
```

> `<UUID>` 換成第 2 步實際產生的檔名。設定完成後，WebUI 的「啟動 Tunnel」按鈕會執行 `cloudflared tunnel run linebot`，把 `bot.linebotnanocat.com` 的流量反向代理到本機 port 8080。

---

## 啟動 WebUI

```powershell
.\venv\Scripts\python.exe webui.py
```

看到 `NiceGUI ready to go on http://localhost:8080` 後，瀏覽器開啟 <http://localhost:8080>。

### 在 WebUI 中操作（由上而下）

1. **服務狀態**：頂端顯示 Ollama 連線、Cloudflare Tunnel、LINE 金鑰是否就緒。
2. **① LINE 設定**：貼上 Channel Access Token 與 Channel Secret。
3. **② Ollama 模型設定**：確認 API 位址，按「重新載入模型」會自動帶出本機已下載的模型，選一個；可調整系統提示詞。
4. **③ Cloudflare Tunnel 穿透**：確認對外網域、Tunnel 名稱與 cloudflared 路徑，按「啟動 Tunnel」。「Webhook URL」欄會顯示固定網址（形如 `https://bot.linebotnanocat.com/callback`）。
5. 按 **儲存全部設定**。
6. **④ 測試對話**：直接輸入訊息測試本地模型回覆，不需經過 LINE。
7. **即時日誌**：顯示 Webhook 收訊、模型呼叫、cloudflared 輸出、錯誤等即時訊息。

### 設定 LINE Webhook

1. 複製 WebUI 顯示的 Webhook URL（固定不變，只需設定一次）。
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
| 按「啟動 Tunnel」顯示找不到 cloudflared | 確認已安裝 cloudflared，或在設定填入正確執行檔路徑 |
| Tunnel 啟動了但對外連不到 | 檢查 `config.yml` 的 `service` 埠是否與 WebUI 的 PORT 一致，及 DNS route 是否已建立 |
| LINE Verify 失敗 (400) | 檢查 Channel Secret 是否正確並已儲存 |
| LINE 收不到回覆 | 確認 Ollama、WebUI、Cloudflare Tunnel 三者都在執行，Webhook URL 結尾為 `/callback` |

## 注意事項

- 要正常運作時，需同時保持 **Ollama**、**WebUI（webui.py）**、**Cloudflare Tunnel（由 UI 啟動）** 三者執行。
- LINE 的 reply token 約 60 秒有效，若模型推論太久，程式會自動改用 push message 補送。
- `.env` 含密鑰，已列入 `.gitignore`，請勿提交到版本庫。
- cloudflared 的憑證檔（`.cloudflared\*.json`）等同 tunnel 的金鑰，請勿外流或提交到版本庫。

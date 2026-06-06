# LINE AI Bot

把 LINE 的訊息接到**本地端 Ollama 模型**產生回覆的開源聊天機器人，附 **WebUI 控制台**與 **Cloudflare Tunnel 安裝精靈**。所有設定、穿透、測試都在網頁完成，不需手動敲指令。

對外連線透過 Cloudflare Tunnel 以**固定網域**反向代理到本機，Webhook URL 設定一次即可、重啟不變。

> 授權：MIT，可自由用於商用。詳見 [LICENSE](LICENSE)。

## 特色

- **本地模型**：對話走本機 Ollama，資料不離開你的機器。
- **零指令設定**：WebUI 內建 Cloudflare Tunnel 安裝精靈，引導完成登入 / 建立 tunnel / 綁定網域。
- **固定對外網域**：免費、無連線數限制，Webhook URL 不再每次重啟變動。
- **內建登入驗證**：控制台預設只綁本機並需密碼登入，避免金鑰外洩。
- **模組化架構**：核心邏輯與 UI 分離，方便測試、擴充與打包。

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
本地服務 (WebUI, port 8080)
  ├─ WebUI 控制台（設定 / 安裝精靈 / 啟停 / 日誌 / 測試，需登入）
  └─ /callback  LINE Webhook 端點（公開）
      │  POST /api/chat
      ▼
本地 Ollama (port 11434) → 產生回覆
      │
      ▼  回傳文字
本地服務 → LINE Platform → 使用者
```

## 專案結構

```
lineai-bot/
├── run.py                      # 啟動入口（python run.py）
├── pyproject.toml              # 套件中繼資料與相依
├── requirements.txt            # 執行所需套件
├── .env.example                # 設定範本
├── LICENSE                     # MIT
└── src/lineai/
    ├── config.py               # 設定鍵定義、讀寫、網址組合
    ├── logbuffer.py            # 即時日誌緩衝（供 WebUI 顯示）
    ├── ollama.py               # Ollama 中轉（列模型 / 檢查 / 聊天）
    ├── line_api.py             # LINE 簽章驗證 / 回覆 / 推播
    ├── tunnel.py               # cloudflared 偵測、啟停、login/create/route
    └── webui/
        ├── server.py           # NiceGUI 組裝、登入驗證、/callback 路由
        └── pages.py            # UI 版面（含 Cloudflare 安裝精靈）
```

## 技術選型

- **WebUI：NiceGUI**（MIT）。底層 FastAPI + uvicorn，把控制台與 LINE Webhook 掛在同一服務、同一埠。
- **穿透：Cloudflare Tunnel（cloudflared）**。固定網域、免費、Cloudflare 邊緣負責 HTTPS 與反向代理。
- **LINE 串接**：以 `requests` + `hmac` 自行實作簽章驗證與回覆，不綁特定 SDK 版本，UI 改金鑰即時生效。

---

## 事前準備

1. **Python 3.9+**
2. **Ollama**：到 <https://ollama.com> 下載安裝，並下載至少一個模型
   ```powershell
   ollama pull qwen3
   ```
3. **網域 + Cloudflare**：擁有一個網域，且其 nameserver 已指向 Cloudflare（在 Cloudflare 後台「Add a site」並依指示改 nameserver）。
4. **cloudflared**：安裝 Cloudflare Tunnel 執行檔
   ```powershell
   winget install --id Cloudflare.cloudflared
   ```
   > 提示：第 3、4 步的說明也內建在 WebUI 的「① 安裝精靈」卡片中。
5. **LINE 官方帳號 + Messaging API Channel**：在 <https://developers.line.biz> 建立。

## 安裝

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 啟動

```powershell
.\venv\Scripts\python.exe run.py
```

預設只綁本機，瀏覽器開 <http://localhost:8080>。第一次啟動若未設定密碼，終端機會印出一組臨時登入密碼。

---

## 在 WebUI 中操作（由上而下）

1. **服務狀態**：頂端顯示 Ollama、Tunnel、LINE 金鑰是否就緒。
2. **① Cloudflare Tunnel 安裝精靈**：
   - 確認 cloudflared 路徑（在 PATH 上時填 `cloudflared` 即可）。
   - 填 Tunnel 名稱與對外網域。
   - **步驟1 登入**：開啟瀏覽器選網域授權，完成後回頁面按「重新檢查」。
   - **步驟2 建立 Tunnel**、**步驟3 綁定網域**：各按一次即可，三顆燈全綠代表就緒。
3. **② LINE 設定**：貼上 Channel Access Token 與 Channel Secret。
4. **③ Ollama 模型設定**：按「重新載入模型」帶出本機模型，選一個；可調系統提示詞。
5. **④ 啟動服務**：三燈全綠後「啟動 Tunnel」可按，下方顯示固定的 Webhook URL。
6. **⑤ 控制台密碼與儲存**：設定固定登入密碼，按「儲存全部設定」。
7. **⑥ 測試對話**：不經 LINE 直接測試本地模型。
8. **即時日誌**：顯示 Webhook 收訊、模型呼叫、cloudflared 輸出。

### 設定 LINE Webhook

1. 複製 WebUI 顯示的 Webhook URL（固定不變，只需設定一次）。
2. LINE Developers Console > 你的 Channel > **Messaging API**。
3. 貼到 **Webhook URL**（結尾 `/callback`），開啟 **Use webhook**，按 **Verify**。
4. 到 LINE Official Account Manager 關閉「自動回覆訊息」與「歡迎訊息」。

完成後，加該官方帳號為好友並傳訊息，即可收到本地模型的 AI 回覆。

---

## 設定項目

設定存於專案根目錄 `.env`（首次啟動自動建立），也可在 WebUI 修改。

| 鍵 | 說明 |
|----|------|
| `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_CHANNEL_SECRET` | LINE Messaging API 金鑰 |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` / `OLLAMA_SYSTEM_PROMPT` | Ollama 位址、模型、系統提示詞 |
| `PUBLIC_DOMAIN` | 對外固定網域，例如 `bot.example.com` |
| `CF_TUNNEL_NAME` | cloudflared 具名 tunnel 名稱 |
| `CLOUDFLARED_PATH` | cloudflared 執行檔路徑（在 PATH 上填 `cloudflared`） |
| `WEBUI_HOST` | WebUI 綁定位址，預設 `127.0.0.1`（只綁本機） |
| `WEBUI_PORT` | 服務埠，預設 `8080` |
| `WEBUI_PASSWORD` | 控制台登入密碼；留空則啟動自動產生臨時密碼 |
| `WEBUI_SECRET_KEY` | session 加密金鑰；留空則自動產生並寫回 `.env` |

---

## 安全性（商用前必讀）

- **只綁本機**：`WEBUI_HOST` 預設 `127.0.0.1`，控制台只有本機能存取。若改 `0.0.0.0` 讓區網存取，**務必先設定強密碼**。
- **登入驗證**：除 `/login` 與 LINE 的 `/callback` 外，所有頁面都需登入。`/callback` 本身以 LINE 簽章驗證把關。
- **密碼**：正式使用請在 `.env` 設定固定 `WEBUI_PASSWORD`，勿依賴啟動時的臨時密碼。
- **機密檔案**：`.env` 與 cloudflared 憑證（`~/.cloudflared/*.json`、`cert.pem`）等同金鑰，已列入 `.gitignore`，切勿提交或外流。
- **規模化建議**：Cloudflare Tunnel 免費版適合單機；高流量或多節點商用建議搭配 Cloudflare 付費方案、把 WebUI 與 Webhook 端點分離部署，並在 `/callback` 前加上速率限制。

---

## 常見問題

| 問題 | 解法 |
|------|------|
| 「Ollama：無法連線」 | 確認 Ollama 已啟動（`ollama serve`）且 API 位址正確 |
| 模型下拉是空的 | 先 `ollama pull` 下載模型，再按「重新載入模型」 |
| 安裝精靈偵測不到 cloudflared | 確認已安裝，或在路徑欄填入完整 `cloudflared.exe` 路徑後按「重新檢查」 |
| 啟動 Tunnel 說找不到 tunnel | 先在安裝精靈完成步驟1～3（登入 / 建立 / 綁定） |
| LINE Verify 失敗 (400) | 檢查 Channel Secret 是否正確並已儲存 |
| LINE 收不到回覆 | 確認 Ollama、WebUI、Tunnel 三者都在執行，Webhook URL 結尾為 `/callback` |
| 忘記控制台密碼 | 編輯 `.env` 的 `WEBUI_PASSWORD` 後重啟 |

## 注意事項

- 正常運作需同時保持 **Ollama**、**WebUI**、**Cloudflare Tunnel（由 UI 啟動）** 三者執行。
- LINE 的 reply token 約 60 秒有效，模型推論太久時程式會自動改用 push message 補送。

## 貢獻

歡迎 issue 與 PR。送 PR 前請確認 `python run.py` 能正常啟動，並盡量為新邏輯補上測試。

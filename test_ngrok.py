"""暫時性 ngrok 連線測試腳本（測試後可刪除）。"""
import time
import traceback

import core
from pyngrok import ngrok, conf

settings = core.load_settings()
token = settings.get("NGROK_AUTHTOKEN", "").strip()
print("authtoken 是否已設定:", bool(token))

if token:
    conf.get_default().auth_token = token

# 清掉殘留
try:
    for t in ngrok.get_tunnels():
        ngrok.disconnect(t.public_url)
except Exception as e:
    print("清除既有 tunnel 時:", e)
try:
    ngrok.kill()
except Exception as e:
    print("kill 時:", e)

time.sleep(1)

try:
    tunnel = ngrok.connect(8080, "http")
    print("建立成功:", tunnel.public_url)
    ngrok.disconnect(tunnel.public_url)
    ngrok.kill()
    print("已關閉測試 tunnel")
except Exception:
    print("建立失敗，完整錯誤如下：")
    traceback.print_exc()

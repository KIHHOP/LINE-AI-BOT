"""
LINE AI Bot 啟動入口。

用法：
    python run.py

會啟動 WebUI 控制台（預設只綁本機 127.0.0.1:8080），
LINE Webhook 端點 /callback 掛在同一服務。
"""

import os
import sys

# 讓 src/ 佈局可被直接以 python run.py 執行
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from lineai.webui import server  # noqa: E402

if __name__ in {"__main__", "__mp_main__"}:
    server.run()

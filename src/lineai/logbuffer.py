"""
即時日誌緩衝：同時輸出到標準 logging 與一個環形緩衝區，供 WebUI 即時顯示。
"""

import time
import logging
from collections import deque
from threading import Lock

_log_buffer: "deque[str]" = deque(maxlen=500)
_log_lock = Lock()

logger = logging.getLogger("lineai")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_h)


def log(message: str, level: str = "INFO") -> None:
    """寫入日誌（同時進緩衝區與標準 logging）。"""
    ts = time.strftime("%H:%M:%S")
    line = f"{ts} [{level}] {message}"
    with _log_lock:
        _log_buffer.append(line)
    getattr(logger, level.lower(), logger.info)(message)


def get_logs() -> list:
    with _log_lock:
        return list(_log_buffer)


def clear_logs() -> None:
    with _log_lock:
        _log_buffer.clear()

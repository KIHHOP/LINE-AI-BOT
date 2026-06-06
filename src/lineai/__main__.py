"""python -m lineai 入口。"""

from .webui import server

if __name__ in {"__main__", "__mp_main__"}:
    server.run()

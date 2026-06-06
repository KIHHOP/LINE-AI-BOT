"""
Cloudflare Tunnel（cloudflared）管理。

涵蓋：
- 執行檔偵測（設定值 / PATH / winget・Program Files 常見位置）
- 環境狀態檢查（是否安裝、是否登入、tunnel 是否存在）
- 一次性設定指令：login / create / route dns（供 WebUI 安裝精靈呼叫）
- tunnel 執行子行程的啟停與輸出導入日誌

設計原則：所有「會跑子行程」的函式都不阻塞呼叫端的事件迴圈太久，
互動式指令（login）以非阻塞子行程執行、輸出串進日誌緩衝。
"""

import os
import shutil
import subprocess
import threading

from .logbuffer import log

# tunnel run 的長駐子行程（單一全域）
_run_proc: "subprocess.Popen | None" = None
_run_lock = threading.Lock()

# login 的互動式子行程（單一全域）
_login_proc: "subprocess.Popen | None" = None
_login_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 執行檔偵測
# ---------------------------------------------------------------------------
def _candidate_paths() -> list:
    """Windows 上 cloudflared 可能的安裝位置（winget / 官方安裝包）。"""
    candidates = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        wg = os.path.join(local, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(wg):
            try:
                for name in os.listdir(wg):
                    if name.lower().startswith("cloudflare.cloudflared"):
                        candidates.append(os.path.join(wg, name, "cloudflared.exe"))
            except Exception:
                pass
        candidates.append(os.path.join(local, "Microsoft", "WinGet", "Links", "cloudflared.exe"))
    for root in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        if root:
            candidates.append(os.path.join(root, "cloudflared", "cloudflared.exe"))
            candidates.append(os.path.join(root, "Cloudflare", "Cloudflared", "cloudflared.exe"))
    return candidates


def resolve(settings: dict) -> str:
    """解析出可用的 cloudflared 執行檔路徑；找不到回傳空字串。"""
    configured = (settings.get("CLOUDFLARED_PATH") or "").strip()
    if configured:
        if os.path.sep in configured or configured.lower().endswith(".exe"):
            if os.path.isfile(configured):
                return configured
        else:
            found = shutil.which(configured)
            if found:
                return found

    found = shutil.which("cloudflared")
    if found:
        return found

    for cand in _candidate_paths():
        if os.path.isfile(cand):
            return cand

    return ""


def exe(settings: dict) -> str:
    """取得 cloudflared 路徑（解析失敗時退回設定值或預設名稱）。"""
    return resolve(settings) or (settings.get("CLOUDFLARED_PATH") or "cloudflared").strip() or "cloudflared"


def is_installed(settings: dict) -> bool:
    return bool(resolve(settings))


# ---------------------------------------------------------------------------
# 環境狀態
# ---------------------------------------------------------------------------
def cert_path() -> str:
    """cloudflared 登入後產生的 origin 憑證路徑。"""
    return os.path.join(os.path.expanduser("~"), ".cloudflared", "cert.pem")


def is_logged_in() -> bool:
    """是否已執行過 cloudflared tunnel login（cert.pem 是否存在）。"""
    return os.path.isfile(cert_path())


def _creationflags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _run_capture(settings: dict, args: list, timeout: int = 30) -> tuple[bool, str]:
    """同步執行一個 cloudflared 短指令，回傳 (成功, 輸出文字)。"""
    path = resolve(settings)
    if not path:
        return False, "找不到 cloudflared 執行檔"
    try:
        proc = subprocess.run(
            [path, *args],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            creationflags=_creationflags(),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out.strip()
    except subprocess.TimeoutExpired:
        return False, "指令逾時"
    except Exception as e:
        return False, f"執行失敗：{e}"


def list_tunnels(settings: dict) -> tuple[bool, str]:
    """列出帳號下的 tunnel（同時可驗證是否已登入）。"""
    return _run_capture(settings, ["tunnel", "list"])


def tunnel_exists(settings: dict, name: str) -> bool:
    """檢查指定名稱的 tunnel 是否存在。"""
    name = (name or "").strip()
    if not name:
        return False
    ok, out = list_tunnels(settings)
    if not ok:
        return False
    # tunnel list 的輸出每行含 tunnel 名稱；用單字邊界粗略比對
    for line in out.splitlines():
        cols = line.split()
        if name in cols:
            return True
    return False


def status(settings: dict) -> dict:
    """彙整安裝精靈需要的環境狀態。"""
    installed = is_installed(settings)
    logged_in = is_logged_in() if installed else False
    name = (settings.get("CF_TUNNEL_NAME") or "").strip()
    created = tunnel_exists(settings, name) if (installed and logged_in and name) else False
    return {
        "installed": installed,
        "logged_in": logged_in,
        "tunnel_created": created,
        "exe": resolve(settings),
    }


# ---------------------------------------------------------------------------
# 一次性設定指令
# ---------------------------------------------------------------------------
def login(settings: dict) -> tuple[bool, str]:
    """啟動互動式 cloudflared tunnel login（非阻塞）。

    cloudflared 會印出一段授權網址並等待使用者在瀏覽器完成授權，
    因此這裡用非阻塞子行程執行，輸出串進日誌；完成後 cert.pem 會出現。
    """
    global _login_proc
    with _login_lock:
        if _login_proc is not None and _login_proc.poll() is None:
            return False, "登入流程已在進行中，請完成瀏覽器授權"
        path = resolve(settings)
        if not path:
            return False, "找不到 cloudflared 執行檔"
        try:
            _login_proc = subprocess.Popen(
                [path, "tunnel", "login"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                creationflags=_creationflags(),
            )
        except Exception as e:
            _login_proc = None
            return False, f"啟動登入失敗：{e}"
        threading.Thread(target=_pump, args=(_login_proc, "login"), daemon=True).start()
    log("已啟動 cloudflared 登入，請在開啟的瀏覽器頁面選擇網域並授權")
    return True, "登入流程已啟動，請在瀏覽器完成授權"


def create(settings: dict, name: str) -> tuple[bool, str]:
    """建立具名 tunnel。"""
    name = (name or "").strip()
    if not name:
        return False, "請先輸入 Tunnel 名稱"
    if not is_logged_in():
        return False, "尚未登入，請先完成 Cloudflare 登入"
    if tunnel_exists(settings, name):
        return True, f"Tunnel「{name}」已存在，沿用即可"
    ok, out = _run_capture(settings, ["tunnel", "create", name], timeout=60)
    log(f"建立 tunnel：{out}", "INFO" if ok else "WARNING")
    if ok:
        return True, f"已建立 Tunnel「{name}」"
    return False, f"建立失敗：{out}"


def route_dns(settings: dict, name: str, domain: str) -> tuple[bool, str]:
    """把網域指到 tunnel（自動覆蓋既有 DNS 記錄）。"""
    name = (name or "").strip()
    domain = (domain or "").strip()
    if not name or not domain:
        return False, "請先填入 Tunnel 名稱與網域"
    ok, out = _run_capture(
        settings, ["tunnel", "route", "dns", "--overwrite-dns", name, domain], timeout=60
    )
    log(f"綁定網域：{out}", "INFO" if ok else "WARNING")
    if ok:
        return True, f"已將 {domain} 指向 Tunnel「{name}」"
    return False, f"綁定失敗：{out}"


# ---------------------------------------------------------------------------
# tunnel run 啟停
# ---------------------------------------------------------------------------
def _pump(proc: "subprocess.Popen", tag: str) -> None:
    """背景執行緒：把子行程輸出逐行讀進日誌緩衝。"""
    try:
        for raw in iter(proc.stdout.readline, ""):
            if not raw:
                break
            line = raw.rstrip("\n")
            if line:
                log(f"[cloudflared:{tag}] {line}")
    except Exception:
        pass


def is_running() -> bool:
    with _run_lock:
        return _run_proc is not None and _run_proc.poll() is None


def start(settings: dict) -> tuple[bool, str]:
    """啟動 cloudflared tunnel run <name>，以 --url 指定本機服務埠。"""
    global _run_proc
    with _run_lock:
        if _run_proc is not None and _run_proc.poll() is None:
            return False, "Cloudflare Tunnel 已在執行"

        path = resolve(settings)
        if not path:
            return False, (
                "找不到 cloudflared 執行檔。請先在安裝精靈完成安裝，"
                "或在設定填入完整路徑。"
            )

        name = (settings.get("CF_TUNNEL_NAME") or "").strip()
        if not name:
            return False, "尚未設定 Tunnel 名稱"
        if not is_logged_in():
            return False, "尚未登入 Cloudflare，請先完成安裝精靈"
        if not tunnel_exists(settings, name):
            return False, f"找不到 Tunnel「{name}」，請先在安裝精靈建立"

        port = int(settings.get("WEBUI_PORT", "8080") or "8080")
        cmd = [path, "tunnel", "--url", f"http://localhost:{port}", "run", name]
        try:
            _run_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                creationflags=_creationflags(),
            )
        except Exception as e:
            _run_proc = None
            return False, f"啟動 cloudflared 失敗：{e}"
        threading.Thread(target=_pump, args=(_run_proc, "run"), daemon=True).start()

    log(f"使用 cloudflared：{path}")
    log("Cloudflare Tunnel 已啟動")
    return True, "Cloudflare Tunnel 已啟動"


def stop() -> tuple[bool, str]:
    """停止 cloudflared run 子行程。"""
    global _run_proc
    with _run_lock:
        if _run_proc is None or _run_proc.poll() is not None:
            _run_proc = None
            return False, "Cloudflare Tunnel 尚未啟動"
        proc = _run_proc
        _run_proc = None

    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception as e:
        log(f"停止 cloudflared 時發生例外：{e}", "WARNING")
        return False, f"停止時發生例外：{e}"

    log("Cloudflare Tunnel 已停止")
    return True, "Cloudflare Tunnel 已停止"

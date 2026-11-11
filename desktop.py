"""Windows desktop entry point; --smoke-test performs local read-only checks."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from runtime_paths import DATA_ROOT, RESOURCE_ROOT, initialize


APP_TITLE = "穿搭舞蹈工作室"
LOGGER = logging.getLogger("fashion_dance.desktop")


class SingleInstanceLock:
    """Acquire only when requested; importing this module creates no mutex."""

    def __init__(self, data_root: Path) -> None:
        normalized = os.path.normcase(str(data_root.expanduser().resolve())).casefold()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
        self.name = "Local\\FashionDanceStudio_" + digest
        self.handle = None
        self.kernel = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        if self.handle is not None:
            return True
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        self.kernel.CreateMutexW.restype = ctypes.c_void_p
        self.kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        self.kernel.ReleaseMutex.restype = ctypes.c_bool
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        handle = self.kernel.CreateMutexW(None, True, self.name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: this process is not the owner.
            self.kernel.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is not None and self.kernel is not None:
            self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def prepare_stdio() -> None:
    """Windowed PyInstaller builds have None streams; restore pipes when present."""
    for name, handle_id in (("stdout", -11), ("stderr", -12)):
        if getattr(sys, name) is not None:
            continue
        stream = None
        if os.name == "nt":
            try:
                import msvcrt
                kernel = ctypes.windll.kernel32
                kernel.GetStdHandle.restype = ctypes.c_void_p
                handle = kernel.GetStdHandle(handle_id)
                if handle and handle != ctypes.c_void_p(-1).value:
                    descriptor = msvcrt.open_osfhandle(int(handle), os.O_WRONLY)
                    stream = os.fdopen(descriptor, "w", encoding="utf-8", buffering=1, closefd=False)
            except (OSError, ValueError):
                pass
        setattr(sys, name, stream or open(os.devnull, "w", encoding="utf-8"))


def prepare_logging() -> None:
    logs = DATA_ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logs / "desktop.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    LOGGER.propagate = False
    logging.getLogger("werkzeug").setLevel(logging.ERROR)


def inform(message: str, error: bool = False) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x10 if error else 0x40)
    else:
        print(message, file=sys.stderr if error else sys.stdout)


class LocalServer:
    def __init__(self) -> None:
        import app as workflow
        from werkzeug.serving import make_server

        self.workflow = workflow
        self.server = make_server("127.0.0.1", 0, workflow.app, threaded=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, name="studio-local-http", daemon=True)
        self.lock = threading.Lock()
        self.stopped = False

    def start(self) -> None:
        self.thread.start()
        LOGGER.info("Local service started on loopback port %s", self.server.server_port)

    def start_recovery(self) -> None:
        """Only interactive launches reconnect saved tasks; smoke tests stay offline."""
        self.workflow.start_recovery_workers()

    def stop(self) -> None:
        with self.lock:
            if self.stopped:
                return
            self.stopped = True
        self.workflow.stop_recovery_workers()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        LOGGER.info("Local service stopped; saved task records were retained")


def smoke_test(server: LocalServer) -> int:
    checks = []
    for endpoint in ("/", "/api/settings", "/api/preferences", "/api/jobs", "/api/shortfilms"):
        try:
            with urllib.request.urlopen(server.url + endpoint, timeout=20) as response:
                body = response.read()
                check = {"endpoint": endpoint, "status": response.status, "ok": response.status == 200}
                if endpoint == "/api/jobs":
                    payload = json.loads(body)
                    jobs = payload.get("jobs") if isinstance(payload, dict) else None
                    valid_jobs = isinstance(jobs, list)
                    billing_jobs = sum(isinstance(job, dict) and isinstance(job.get("billing"), dict)
                                       for job in jobs) if valid_jobs else 0
                    check.update(jobs_count=len(jobs) if valid_jobs else 0, billing_jobs_count=billing_jobs)
                    check["ok"] = check["ok"] and valid_jobs and billing_jobs == len(jobs)
                if endpoint == '/api/shortfilms':
                    import video_superresolution
                    sr = video_superresolution.availability()
                    check['superresolution_models_bundled'] = all(m['available'] for m in sr['modes'].values())
                    check['ok'] = check['ok'] and check['superresolution_models_bundled']
                checks.append(check)
        except urllib.error.HTTPError as error:
            checks.append({"endpoint": endpoint, "status": error.code, "ok": False})
        except Exception as error:
            checks.append({"endpoint": endpoint, "error_type": type(error).__name__, "ok": False})
    report = {
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "ffmpeg_found": bool(shutil.which("ffmpeg")),
        "ffprobe_found": bool(shutil.which("ffprobe")),
        "cloud_requests_submitted": 0,
    }
    report["ok"] = report["ok"] and report["ffmpeg_found"] and report["ffprobe_found"]
    serialized = json.dumps(report, ensure_ascii=True)
    (DATA_ROOT / "logs" / "desktop-smoke.json").write_text(serialized + "\n", encoding="utf-8")
    print(serialized, flush=True)
    LOGGER.info("Local read-only smoke test finished: %s", "passed" if report["ok"] else "failed")
    return 0 if report["ok"] else 1


def browser_mode(server: LocalServer) -> None:
    """Keep a small controller alive while the system browser displays the app."""
    LOGGER.info("Using system browser fallback")
    webbrowser.open(server.url, new=1)
    try:
        import tkinter as tk
        from tkinter import ttk

        controller = tk.Tk()
        controller.title(APP_TITLE + " · 浏览器模式")
        controller.geometry("520x250")
        controller.minsize(480, 230)
        panel = ttk.Frame(controller, padding=24)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="工作室已在系统浏览器中打开", font=("Microsoft YaHei", 15)).pack(anchor="w")
        ttk.Label(panel, text="内嵌浏览器暂不可用，主要功能可继续在浏览器使用。\n云端任务编号保存后，关闭重开可继续查询并下载结果。", wraplength=455).pack(anchor="w", pady=(14, 10))
        ttk.Label(panel, text=server.url).pack(anchor="w")
        actions = ttk.Frame(panel)
        actions.pack(anchor="w", pady=(18, 0))
        ttk.Button(actions, text="打开工作室", command=lambda: webbrowser.open(server.url)).pack(side="left", padx=(0, 12))
        ttk.Button(actions, text="打开数据目录", command=lambda: os.startfile(str(DATA_ROOT))).pack(side="left", padx=(0, 12))
        ttk.Button(actions, text="退出软件", command=controller.destroy).pack(side="left")
        controller.mainloop()
    except Exception as error:
        LOGGER.warning("Browser controller unavailable: %s", type(error).__name__)
        inform("工作室已在系统浏览器打开。\n\n点击“确定”会退出软件。已经保存云端任务编号的视频，重开后会继续查询并下载；不会重新提交生成任务。")


def desktop_mode(server: LocalServer) -> None:
    try:
        import webview
        import ui_preferences

        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        background = "#111b24" if ui_preferences.load_preferences()["theme"] == "dark" else "#f5f3ef"
        window = webview.create_window(APP_TITLE, server.url, width=1280, height=900,
                                       min_size=(1000, 720), background_color=background, text_select=True)
        window.events.closed += server.stop
        icon = RESOURCE_ROOT / "desktop_assets" / "studio.ico"
        webview.start(gui="edgechromium", debug=False, private_mode=False,
                      storage_path=str(DATA_ROOT / "webview"), icon=str(icon) if icon.exists() else None)
    except (Exception, SystemExit) as error:
        # Exception values can contain request URLs or user data; log only the type.
        LOGGER.warning("Embedded browser unavailable: %s", type(error).__name__)
        browser_mode(server)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--smoke-test", action="store_true", help="Run read-only local endpoint checks, without GUI or cloud generation")
    parser.add_argument("--browser", action="store_true", help="Open the system browser instead of the embedded window")
    args = parser.parse_args(argv)
    prepare_stdio()
    instance_lock = SingleInstanceLock(DATA_ROOT)
    server = None
    try:
        if not args.smoke_test and not instance_lock.acquire():
            inform("工作室已打开，请使用现有窗口")
            return 0
        initialize()
        prepare_logging()
        server = LocalServer()
        server.start()
        if args.smoke_test:
            return smoke_test(server)
        server.start_recovery()
        if args.browser:
            browser_mode(server)
        else:
            desktop_mode(server)
        return 0
    except Exception as error:
        LOGGER.error("Application could not start: %s", type(error).__name__)
        if args.smoke_test:
            print(json.dumps({"ok": False, "error_type": type(error).__name__, "cloud_requests_submitted": 0}), flush=True)
        else:
            inform("软件启动失败。请查看数据目录 logs/desktop.log 中的错误类型，或联系维护人员。\n\n数据目录：" + str(DATA_ROOT), error=True)
        return 1
    finally:
        try:
            if server is not None:
                server.stop()
        finally:
            instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())

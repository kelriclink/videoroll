from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import socket
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.parse import urlsplit, urlunsplit

from videoroll.apps.render_worker.runtime import RenderWorkerRuntime, probe_capabilities
from videoroll.config import RenderWorkerSettings

APP_NAME = "VideoRoll Render Worker"
logger = logging.getLogger(__name__)


def _app_dir() -> Path:
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "VideoRoll" / "RenderWorker"


def _config_path() -> Path:
    return _app_dir() / "config.json"


def _credential_path() -> Path:
    return _app_dir() / "credential.json"


def _work_dir() -> Path:
    return _app_dir() / "work"


def _normalize_server_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    path = parts.path.rstrip("/")
    if not path:
        path = "/api"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _bundled_ffmpeg() -> str:
    candidates: list[Path] = []
    executable_dir = Path(sys.executable).resolve().parent
    candidates.append(executable_dir / "ffmpeg.exe")
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.append(Path(bundle_dir) / "ffmpeg.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("ffmpeg")
    if found:
        return found
    return str(candidates[0])


def _load_config() -> dict[str, object]:
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_config(data: dict[str, object]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class _QueueLogHandler(logging.Handler):
    def __init__(self, target: "queue.Queue[str]") -> None:
        super().__init__()
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.target.put_nowait(self.format(record))
        except Exception:
            pass


class WorkerController:
    def __init__(self, log_queue: "queue.Queue[str]") -> None:
        self.log_queue = log_queue
        self.runtime: RenderWorkerRuntime | None = None
        self.thread: threading.Thread | None = None
        self.last_error = ""

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, settings: RenderWorkerSettings) -> None:
        if self.running:
            return
        self.last_error = ""

        def runner() -> None:
            try:
                runtime = RenderWorkerRuntime(settings)
                self.runtime = runtime
                runtime.run_forever()
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Windows render worker stopped unexpectedly")
            finally:
                self.runtime = None

        self.thread = threading.Thread(target=runner, name="videoroll-render-worker", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        runtime = self.runtime
        if runtime is not None:
            runtime.stop()


class WindowsRenderWorkerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("900x680")
        self.root.minsize(760, 560)
        self.log_queue: "queue.Queue[str]" = queue.Queue(maxsize=4000)
        self.controller = WorkerController(self.log_queue)

        saved = _load_config()
        self.server_url = tk.StringVar(value=str(saved.get("server_url") or ""))
        self.enrollment_token = tk.StringVar(value="")
        self.node_name = tk.StringVar(value=str(saved.get("node_name") or socket.gethostname()))
        self.max_concurrency = tk.IntVar(value=int(saved.get("max_concurrency") or 1))
        self.status = tk.StringVar(value="已停止")
        self.ffmpeg_status = tk.StringVar(value=_bundled_ffmpeg())

        self._install_logging()
        self._build_ui()
        self._refresh_status()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _install_logging(self) -> None:
        app_dir = _app_dir()
        app_dir.mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        queue_handler = _QueueLogHandler(self.log_queue)
        queue_handler.setFormatter(formatter)
        file_handler = logging.FileHandler(app_dir / "render-worker.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(queue_handler)
        root_logger.addHandler(file_handler)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(outer, text="VideoRoll Windows 渲染节点", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        ttk.Label(
            outer,
            text="节点自动扫描 NVIDIA GPU，并自行选择空闲且支持目标编码器的显卡。",
        ).pack(anchor=tk.W, pady=(4, 14))

        form = ttk.LabelFrame(outer, text="连接与节点配置", padding=12)
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="服务器").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=5)
        ttk.Entry(form, textvariable=self.server_url).grid(row=0, column=1, sticky=tk.EW, pady=5)
        ttk.Label(form, text="例如 https://video.example.com/api").grid(row=0, column=2, sticky=tk.W, padx=(8, 0))

        ttk.Label(form, text="一次性配对 Token").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=5)
        ttk.Entry(form, textvariable=self.enrollment_token, show="*").grid(row=1, column=1, sticky=tk.EW, pady=5)
        ttk.Label(form, text="首次配对才需要 vre_*").grid(row=1, column=2, sticky=tk.W, padx=(8, 0))

        ttk.Label(form, text="节点名称").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=5)
        ttk.Entry(form, textvariable=self.node_name).grid(row=2, column=1, sticky=tk.EW, pady=5)

        ttk.Label(form, text="最大并发").grid(row=3, column=0, sticky=tk.W, padx=(0, 8), pady=5)
        ttk.Spinbox(form, from_=1, to=32, textvariable=self.max_concurrency, width=8).grid(
            row=3, column=1, sticky=tk.W, pady=5
        )
        ttk.Label(form, text="节点总并发上限；GPU 由节点内部自动分配").grid(
            row=3, column=2, sticky=tk.W, padx=(8, 0)
        )

        ttk.Label(form, text="FFmpeg").grid(row=4, column=0, sticky=tk.W, padx=(0, 8), pady=5)
        ttk.Label(form, textvariable=self.ffmpeg_status).grid(row=4, column=1, columnspan=2, sticky=tk.W, pady=5)

        actions = ttk.Frame(outer)
        actions.pack(fill=tk.X, pady=12)
        self.detect_button = ttk.Button(actions, text="检测硬件", command=self._detect_hardware)
        self.detect_button.pack(side=tk.LEFT)
        self.start_button = ttk.Button(actions, text="启动节点", command=self._start)
        self.start_button.pack(side=tk.LEFT, padx=(8, 0))
        self.stop_button = ttk.Button(actions, text="停止接单", command=self._stop)
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="重新配对", command=self._reset_pairing).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(actions, text="状态：").pack(side=tk.LEFT, padx=(24, 4))
        ttk.Label(actions, textvariable=self.status, font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)

        device_box = ttk.LabelFrame(outer, text="检测到的渲染设备", padding=8)
        device_box.pack(fill=tk.X, pady=(0, 12))
        self.devices = tk.Listbox(device_box, height=6, font=("Consolas", 10))
        self.devices.pack(fill=tk.X)

        log_box = ttk.LabelFrame(outer, text="运行日志", padding=8)
        log_box.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(log_box, height=16, wrap=tk.NONE, state=tk.DISABLED, font=("Consolas", 9))
        yscroll = ttk.Scrollbar(log_box, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=yscroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _settings(self) -> RenderWorkerSettings:
        server = _normalize_server_url(self.server_url.get())
        if not server:
            raise ValueError("请填写 VideoRoll 服务器地址")
        ffmpeg_path = _bundled_ffmpeg()
        if not Path(ffmpeg_path).is_file() and shutil.which(ffmpeg_path) is None:
            raise FileNotFoundError("找不到 ffmpeg.exe；请使用完整 Windows 客户端压缩包")
        node_name = self.node_name.get().strip() or socket.gethostname()
        return RenderWorkerSettings(
            RENDER_WORKER_SERVER_URL=server,
            RENDER_WORKER_ENROLLMENT_TOKEN=self.enrollment_token.get().strip(),
            RENDER_WORKER_CREDENTIAL_FILE=str(_credential_path()),
            RENDER_WORKER_KEY=f"windows-{socket.gethostname().lower()}",
            RENDER_WORKER_NAME=node_name,
            RENDER_WORKER_BACKEND="auto",
            RENDER_WORKER_GPU_DEVICE="",
            RENDER_WORKER_MAX_CONCURRENCY=max(1, min(32, int(self.max_concurrency.get()))),
            RENDER_WORKER_WORK_DIR=str(_work_dir()),
            FFMPEG_PATH=ffmpeg_path,
        )

    def _persist_config(self) -> None:
        _save_config(
            {
                "server_url": _normalize_server_url(self.server_url.get()),
                "node_name": self.node_name.get().strip() or socket.gethostname(),
                "max_concurrency": max(1, min(32, int(self.max_concurrency.get()))),
            }
        )

    def _detect_hardware(self) -> None:
        self.detect_button.configure(state=tk.DISABLED)
        self.status.set("检测硬件…")

        def task() -> None:
            try:
                settings = self._settings()
                caps, _resources, devices = probe_capabilities(settings)
                lines = []
                for device in devices:
                    encoders = ", ".join(device.encoders) or "无硬件编码器"
                    location = (
                        f" index={device.index}"
                        if device.index is not None
                        else (f" {device.path}" if device.path else "")
                    )
                    lines.append(f"{device.name} | {device.backend}{location} | {encoders}")
                if not lines:
                    lines = [f"未发现渲染设备；FFmpeg encoders={caps.get('encoders', [])}"]
                self.root.after(0, lambda: self._set_devices(lines))
            except Exception as exc:
                logger.exception("hardware detection failed")
                self.root.after(0, lambda: messagebox.showerror(APP_NAME, str(exc)))
            finally:
                self.root.after(0, lambda: self.detect_button.configure(state=tk.NORMAL))

        threading.Thread(target=task, name="hardware-probe", daemon=True).start()

    def _set_devices(self, lines: list[str]) -> None:
        self.devices.delete(0, tk.END)
        for line in lines:
            self.devices.insert(tk.END, line)
        self.status.set("运行中" if self.controller.running else "已停止")

    def _start(self) -> None:
        if self.controller.running:
            return
        try:
            settings = self._settings()
            if not _credential_path().is_file() and not settings.enrollment_token.strip():
                raise ValueError("此电脑尚未配对，请先输入渲染管理页面生成的一次性 vre_* Token")
            self._persist_config()
            _work_dir().mkdir(parents=True, exist_ok=True)
            self.controller.start(settings)
            self.status.set("启动中…")
            logger.info("starting Windows render worker: server=%s name=%s", settings.server_url, settings.name)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _stop(self) -> None:
        if not self.controller.running:
            return
        self.controller.stop()
        self.status.set("停止接单中…")
        logger.info("worker stop requested; active executions may finish before exit")

    def _reset_pairing(self) -> None:
        if self.controller.running:
            messagebox.showwarning(APP_NAME, "请先停止节点，再重新配对。")
            return
        if not messagebox.askyesno(APP_NAME, "删除本机 Worker 凭据并重新配对？"):
            return
        try:
            _credential_path().unlink(missing_ok=True)
            self.enrollment_token.set("")
            logger.info("local worker credential removed")
            messagebox.showinfo(APP_NAME, "本机凭据已删除。请在服务器生成新的 vre_* Token 后重新启动。")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _refresh_status(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, line + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)

        if self.controller.running:
            runtime = self.controller.runtime
            if runtime is not None:
                with runtime._active_lock:
                    active = len(runtime._active)
                self.status.set(f"运行中 · {active} 个任务")
                self.enrollment_token.set("")
        elif self.controller.thread is not None:
            self.status.set("异常停止" if self.controller.last_error else "已停止")

        self.start_button.configure(state=tk.DISABLED if self.controller.running else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if self.controller.running else tk.DISABLED)
        self.root.after(750, self._refresh_status)

    def _on_close(self) -> None:
        if self.controller.running and not messagebox.askyesno(
            APP_NAME,
            "节点仍在运行。关闭客户端会中断仍在执行的本地渲染进程，确定退出吗？",
        ):
            return
        self.controller.stop()
        self.root.destroy()


def main() -> None:
    if os.name != "nt":
        raise SystemExit("VideoRoll Windows Render Worker must run on Windows")
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.15)
    except tk.TclError:
        pass
    WindowsRenderWorkerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

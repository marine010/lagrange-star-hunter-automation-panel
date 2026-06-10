from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .windowing import WindowInfo, enable_dpi_awareness, list_visible_windows

enable_dpi_awareness()

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


DEFAULT_CONFIG = Path("configs/star_hunter_1920.json")


def _safe_file_stem(text: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text.strip())
    safe = safe.strip("_")
    return safe[:64] or "sample"


class LagrangeDataGui(tk.Tk):
    def __init__(self, config_path: Path | None = None):
        super().__init__()
        self.title("拉格朗日数据采集")
        self.geometry("760x560")
        self.minsize(700, 500)

        self.config_path_var = tk.StringVar(value=str((config_path or DEFAULT_CONFIG).resolve()))
        self.window_var = tk.StringVar(value="")
        self.mode_var = tk.StringVar(value="hand")
        self.interval_ms_var = tk.StringVar(value="1000")
        self.duration_var = tk.StringVar(value="120")
        self.frames_var = tk.StringVar(value="")
        self.battle_time_var = tk.StringVar(value="140")
        self.output_name_var = tk.StringVar(value="")
        self.normalize_zoom_var = tk.BooleanVar(value=False)
        self.zoom_seconds_var = tk.StringVar(value="0")
        self.zoom_scroll_var = tk.StringVar(value="-5")
        self.status_var = tk.StringVar(value="就绪")
        self.output_dir_var = tk.StringVar(value="")

        self._windows: list[WindowInfo] = []
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None

        self._build_ui()
        self.refresh_windows_clicked()
        self.after(100, self._poll_queue)

    def _build_ui(self) -> None:
        self.configure(background="#111827")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        self._build_styles()

        header = ttk.Frame(self, padding=(12, 10), style="Top.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="数据采集工具", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=1, sticky="e")

        config_bar = ttk.Frame(self, padding=(12, 8), style="App.TFrame")
        config_bar.grid(row=1, column=0, sticky="ew")
        config_bar.columnconfigure(1, weight=1)
        ttk.Label(config_bar, text="配置", style="Label.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(config_bar, textvariable=self.config_path_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(config_bar, text="选择", command=self.browse_config_clicked).grid(row=0, column=2)

        controls = ttk.Frame(self, padding=(12, 8), style="Panel.TFrame")
        controls.grid(row=2, column=0, sticky="ew", padx=12)
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(3, weight=1)

        ttk.Label(controls, text="窗口", style="PanelLabel.TLabel").grid(row=0, column=0, sticky="w")
        self.window_combo = ttk.Combobox(
            controls,
            textvariable=self.window_var,
            state="readonly",
            postcommand=self.refresh_windows_clicked,
        )
        self.window_combo.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(8, 8))
        ttk.Button(controls, text="刷新", command=self.refresh_windows_clicked).grid(row=0, column=4, sticky="ew")

        ttk.Label(controls, text="类型", style="PanelLabel.TLabel").grid(row=1, column=0, sticky="w", pady=(10, 0))
        mode_frame = ttk.Frame(controls, style="Panel.TFrame")
        mode_frame.grid(row=1, column=1, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Radiobutton(
            mode_frame,
            text="手牌/费用/计时",
            variable=self.mode_var,
            value="hand",
            command=self._mode_changed,
        ).pack(side="left")
        ttk.Radiobutton(
            mode_frame,
            text="战斗/技能/066",
            variable=self.mode_var,
            value="battle",
            command=self._mode_changed,
        ).pack(side="left", padx=(14, 0))

        ttk.Label(controls, text="间隔ms", style="PanelLabel.TLabel").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(controls, textvariable=self.interval_ms_var, width=10).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(10, 0))
        ttk.Label(controls, text="时长s", style="PanelLabel.TLabel").grid(row=2, column=2, sticky="e", pady=(10, 0))
        ttk.Entry(controls, textvariable=self.duration_var, width=10).grid(row=2, column=3, sticky="w", padx=(8, 0), pady=(10, 0))
        ttk.Label(controls, text="帧数", style="PanelLabel.TLabel").grid(row=2, column=4, sticky="w", pady=(10, 0))
        ttk.Entry(controls, textvariable=self.frames_var, width=8).grid(row=2, column=5, sticky="w", pady=(10, 0))

        ttk.Label(controls, text="战斗时间", style="PanelLabel.TLabel").grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(controls, textvariable=self.battle_time_var, width=10).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(10, 0))
        ttk.Checkbutton(
            controls,
            text="开局缩放校准",
            variable=self.normalize_zoom_var,
        ).grid(row=3, column=2, sticky="e", pady=(10, 0))
        ttk.Entry(controls, textvariable=self.zoom_seconds_var, width=8).grid(row=3, column=3, sticky="w", padx=(8, 0), pady=(10, 0))
        ttk.Label(controls, text="滚轮", style="PanelLabel.TLabel").grid(row=3, column=4, sticky="w", pady=(10, 0))
        ttk.Entry(controls, textvariable=self.zoom_scroll_var, width=8).grid(row=3, column=5, sticky="w", pady=(10, 0))

        ttk.Label(controls, text="目录名", style="PanelLabel.TLabel").grid(row=4, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(controls, textvariable=self.output_name_var).grid(row=4, column=1, columnspan=5, sticky="ew", padx=(8, 0), pady=(10, 0))

        actions = ttk.Frame(self, padding=(12, 8), style="App.TFrame")
        actions.grid(row=4, column=0, sticky="ew")
        actions.columnconfigure(3, weight=1)
        self.start_button = ttk.Button(actions, text="开始采集", command=self.start_clicked, style="Accent.TButton")
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop_clicked, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=(0, 8))
        ttk.Button(actions, text="打开目录", command=self.open_output_clicked).grid(row=0, column=2, padx=(0, 8))
        ttk.Label(actions, textvariable=self.output_dir_var, style="Path.TLabel").grid(row=0, column=3, sticky="ew")

        log_frame = ttk.Frame(self, padding=(12, 0, 12, 12), style="App.TFrame")
        log_frame.grid(row=3, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            height=12,
            background="#020617",
            foreground="#d1d5db",
            insertbackground="#d1d5db",
            relief="flat",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("App.TFrame", background="#111827")
        style.configure("Top.TFrame", background="#e5edf7")
        style.configure("Panel.TFrame", background="#f8fafc")
        style.configure("Title.TLabel", background="#e5edf7", foreground="#111827", font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Status.TLabel", background="#e5edf7", foreground="#475569", font=("Microsoft YaHei UI", 9))
        style.configure("Label.TLabel", background="#111827", foreground="#e5e7eb", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("PanelLabel.TLabel", background="#f8fafc", foreground="#334155", font=("Microsoft YaHei UI", 9))
        style.configure("Path.TLabel", background="#111827", foreground="#9ca3af", font=("Microsoft YaHei UI", 8))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 9, "bold"))

    def browse_config_clicked(self) -> None:
        path = filedialog.askopenfilename(
            title="选择配置",
            filetypes=[("JSON config", "*.json"), ("All files", "*.*")],
            initialdir=str(Path("configs").resolve()),
        )
        if path:
            self.config_path_var.set(path)

    def refresh_windows_clicked(self) -> None:
        try:
            self._windows = list_visible_windows()
        except Exception as exc:
            messagebox.showerror("刷新窗口失败", str(exc))
            return
        values = [f"{item.title}  |  hwnd={item.hwnd}" for item in self._windows]
        self.window_combo.configure(values=values)
        if values and (not self.window_var.get() or self.window_var.get() not in values):
            preferred_index = next(
                (index for index, item in enumerate(self._windows) if "星际猎人" in item.title),
                0,
            )
            self.window_combo.current(preferred_index)

    def _mode_changed(self) -> None:
        if self.mode_var.get() == "hand" and self.interval_ms_var.get().strip() in {"750", ""}:
            self.interval_ms_var.set("1000")
        elif self.mode_var.get() == "battle" and self.interval_ms_var.get().strip() in {"1000", ""}:
            self.interval_ms_var.set("750")

    def _selected_window(self) -> WindowInfo | None:
        index = self.window_combo.current()
        if 0 <= index < len(self._windows):
            return self._windows[index]
        return None

    def _build_output_dir(self) -> Path:
        mode = self.mode_var.get()
        prefix = "hand_live_data_gui" if mode == "hand" else "battle_live_data_gui"
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        custom = _safe_file_stem(self.output_name_var.get())
        name = custom if self.output_name_var.get().strip() else f"{prefix}_{stamp}"
        return (Path("training_samples") / name).resolve()

    def _build_command(self, window: WindowInfo, output_dir: Path) -> list[str]:
        mode = self.mode_var.get()
        interval_ms = max(1, int(float(self.interval_ms_var.get().strip() or "1000")))
        duration = max(0.0, float(self.duration_var.get().strip() or "0"))
        frames_text = self.frames_var.get().strip()
        command = [
            sys.executable,
            "-m",
            "lagrange_bot.bot",
            "collect-training" if mode == "hand" else "collect-battle",
            "--config",
            self.config_path_var.get(),
            "--hwnd",
            str(window.hwnd),
            "--output",
            str(output_dir),
            "--interval-ms",
            str(interval_ms),
            "--duration",
            str(duration),
        ]
        if frames_text:
            command.extend(["--frames", str(max(1, int(float(frames_text))))])
        if mode == "battle":
            command.extend(["--time", str(float(self.battle_time_var.get().strip() or "140"))])
            if self.normalize_zoom_var.get():
                command.extend(
                    [
                        "--normalize-zoom-seconds",
                        str(max(0.0, float(self.zoom_seconds_var.get().strip() or "0"))),
                        "--normalize-zoom-scroll",
                        str(int(float(self.zoom_scroll_var.get().strip() or "-5"))),
                    ]
                )
        return command

    def start_clicked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            messagebox.showinfo("采集中", "当前采集还没有停止。")
            return
        window = self._selected_window()
        if window is None:
            messagebox.showerror("缺少窗口", "请先选择要采集的游戏窗口。")
            return
        try:
            output_dir = self._build_output_dir()
            command = self._build_command(window, output_dir)
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        output_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir_var.set(str(output_dir))
        self._append_log(f"$ {' '.join(command)}\n")
        try:
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            self._process = subprocess.Popen(
                command,
                cwd=str(Path.cwd()),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
            self._process = None
            return

        self.status_var.set(f"采集中 pid={self._process.pid}")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._reader_thread = threading.Thread(target=self._read_process_output, daemon=True)
        self._reader_thread.start()

    def stop_clicked(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        self.status_var.set("正在停止...")
        process.terminate()
        self.after(1500, self._kill_if_still_running)

    def open_output_clicked(self) -> None:
        path_text = self.output_dir_var.get().strip()
        if not path_text:
            messagebox.showinfo("没有目录", "还没有采集输出目录。")
            return
        path = Path(path_text)
        if not path.exists():
            messagebox.showinfo("目录不存在", str(path))
            return
        os.startfile(str(path))  # type: ignore[attr-defined]

    def _kill_if_still_running(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    def _read_process_output(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self._queue.put(("line", line.rstrip("\n")))
        returncode = process.wait()
        self._queue.put(("exit", returncode))

    def _poll_queue(self) -> None:
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "line":
                self._handle_process_line(str(payload))
            elif kind == "exit":
                self._process_exited(int(payload))
        self.after(100, self._poll_queue)

    def _handle_process_line(self, line: str) -> None:
        self._append_log(line + "\n")
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        name = event.get("event")
        if name in {"training_collection_start", "battle_collection_start"}:
            self.output_dir_var.set(str(event.get("dir") or ""))
            self.status_var.set("采集中")
        elif name in {"training_frame_saved", "battle_frame_saved"}:
            frame = event.get("frame_index")
            extra = ""
            if "targets" in event:
                extra = f" | 066={event.get('targets')}"
            self.status_var.set(f"已采集 {frame} 帧{extra}")
        elif name in {"training_collection_stop", "battle_collection_stop"}:
            self.status_var.set(f"采集完成，共 {event.get('frames')} 帧")

    def _process_exited(self, returncode: int) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("采集完成" if returncode == 0 else f"采集结束 code={returncode}")
        self._append_log(f"\n[process exited with code {returncode}]\n")
        self._process = None

    def _append_log(self, text: str) -> None:
        self.log_text.insert("end", text)
        self.log_text.see("end")

    def destroy(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
        super().destroy()


def launch_data_gui(config: str | Path | None = None) -> None:
    config_path = Path(config).resolve() if config else None
    app = LagrangeDataGui(config_path=config_path)
    app.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the Lagrange data collection GUI.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Config JSON path.")
    args = parser.parse_args(argv)
    launch_data_gui(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

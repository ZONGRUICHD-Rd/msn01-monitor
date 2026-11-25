"""Cross-platform Tkinter Dark-Mode Host GUI (上位机) for MSN01 Monitor."""

from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Optional

from PIL import Image, ImageTk

from . import __app_name__, __version__
from .config import AppConfig, load_config, save_config
from .protocol import MSN01Screen, PortCandidate, list_serial_ports
from .renderer import THEME_IDS, THEMES, DashboardRenderer
from .telemetry import GpuSample, SystemSnapshot, TelemetryCollector


# Modern dark slate UI palette
UI_BG = "#0F172A"
UI_CARD = "#1E293B"
UI_CARD_ALT = "#162033"
UI_BORDER = "#334155"
UI_TEXT = "#F8FAFC"
UI_MUTED = "#94A3B8"
UI_ACCENT = "#06B6D4"
UI_ACCENT_HOVER = "#22D3EE"
UI_GREEN = "#10B981"
UI_AMBER = "#F59E0B"
UI_RED = "#EF4444"

TOUCH_THRESHOLD = 3600
TOUCH_COOLDOWN_S = 0.7


@dataclass
class _UiUpdate:
    snapshot: SystemSnapshot
    preview_img: Image.Image
    connected: bool
    port_text: str
    screen_size: tuple[int, int]
    status_text: str
    status_level: str  # "ok", "warn", "err", "idle"
    frame_ms: float
    frame_bytes: int
    touch_adc: Optional[int]
    theme_id: str


class MonitorWorker(threading.Thread):
    """Background worker handling telemetry sampling, rendering, serial push, and touch polling."""

    def __init__(self, config: AppConfig, ui_queue: "queue.Queue[_UiUpdate]") -> None:
        super().__init__(daemon=True)
        self._lock = threading.RLock()
        self._cfg = config
        self._ui_queue = ui_queue
        self._stop_event = threading.Event()
        self._want_connected = config.auto_connect
        self._force_reconnect = False

        self._screen = MSN01Screen()
        self._telemetry = TelemetryCollector()
        self._renderer = DashboardRenderer()

        self._status_text = "准备就绪"
        self._status_level = "idle"
        self._last_touch_switch = 0.0
        self._was_touched = False
        self._next_reconnect_at = 0.0
        self._last_frame_ms = 0.0
        self._last_frame_bytes = 0
        self._last_touch_adc: Optional[int] = None

    def update_config(self, **kwargs) -> None:
        with self._lock:
            port_changed = "port" in kwargs and kwargs["port"] != self._cfg.port
            for k, v in kwargs.items():
                if hasattr(self._cfg, k):
                    setattr(self._cfg, k, v)
            if port_changed and self._want_connected:
                self._force_reconnect = True

    def get_config_copy(self) -> AppConfig:
        with self._lock:
            return AppConfig(**self._cfg.__dict__)

    def request_connect(self) -> None:
        with self._lock:
            self._want_connected = True
            self._force_reconnect = True
            self._next_reconnect_at = 0.0

    def request_disconnect(self) -> None:
        with self._lock:
            self._want_connected = False
            self._force_reconnect = False
        self._screen.close()

    def stop(self) -> None:
        self._stop_event.set()
        self._screen.close()
        self._telemetry.close()

    def _cycle_theme_locked(self) -> str:
        cur = self._cfg.theme
        idx = THEME_IDS.index(cur) if cur in THEME_IDS else 0
        nxt = THEME_IDS[(idx + 1) % len(THEME_IDS)]
        self._cfg.theme = nxt
        save_config(self._cfg)
        return nxt

    def _emit_ui(self, snap: SystemSnapshot, preview: Image.Image, cfg: AppConfig) -> None:
        upd = _UiUpdate(
            snapshot=snap,
            preview_img=preview,
            connected=self._screen.is_connected,
            port_text=self._screen.port or "--",
            screen_size=self._screen.size,
            status_text=self._status_text,
            status_level=self._status_level,
            frame_ms=self._last_frame_ms,
            frame_bytes=self._last_frame_bytes,
            touch_adc=self._last_touch_adc,
            theme_id=cfg.theme,
        )
        # Keep only the latest update so UI never lags
        while not self._ui_queue.empty():
            try:
                self._ui_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._ui_queue.put_nowait(upd)
        except queue.Full:
            pass

    def run(self) -> None:
        while not self._stop_event.is_set():
            loop_start = time.perf_counter()

            with self._lock:
                cfg = AppConfig(**self._cfg.__dict__)
                want_conn = self._want_connected
                force_reconn = self._force_reconnect
                self._force_reconnect = False

            # Handle connect / disconnect transitions
            if not want_conn:
                if self._screen.is_connected:
                    self._screen.close()
                self._status_text = "已断开连接（预览仍实时运行）"
                self._status_level = "idle"
                self._last_touch_adc = None
            else:
                if force_reconn and self._screen.is_connected:
                    self._screen.close()

                if not self._screen.is_connected and time.perf_counter() >= self._next_reconnect_at:
                    try:
                        self._status_text = f"正在连接 ({cfg.port})..."
                        self._status_level = "warn"
                        w, h = self._screen.connect(cfg.port)
                        self._status_text = f"已连接 {self._screen.port} ({w}×{h})"
                        self._status_level = "ok"
                    except Exception as exc:
                        self._status_text = f"连接失败: {exc}"
                        self._status_level = "err"
                        if cfg.auto_reconnect:
                            self._next_reconnect_at = time.perf_counter() + 2.5
                        else:
                            with self._lock:
                                self._want_connected = False

            # Poll capacitive touch sensor if connected & enabled
            if self._screen.is_connected and cfg.touch_switch_theme:
                adc = self._screen.read_touch_adc()
                self._last_touch_adc = adc
                if adc is not None:
                    touched = adc < TOUCH_THRESHOLD
                    now = time.perf_counter()
                    if touched and not self._was_touched and (now - self._last_touch_switch) >= TOUCH_COOLDOWN_S:
                        with self._lock:
                            new_theme = self._cycle_theme_locked()
                            cfg.theme = new_theme
                        self._last_touch_switch = now
                        self._status_text = f"触摸键切换主题 -> {new_theme}"
                        self._status_level = "ok"
                    self._was_touched = touched

            # Collect telemetry & render frame
            snap = self._telemetry.sample(gpu_id=cfg.gpu_id)
            frame = self._renderer.render(
                snap,
                theme=cfg.theme,
                size=self._screen.size if self._screen.is_connected else (160, 80),
                record_history=True,
            )

            # Prepare preview image reflecting rotate_180 & brightness
            preview = frame.copy()
            if cfg.rotate_180:
                preview = preview.transpose(Image.Transpose.ROTATE_180)
            if cfg.brightness < 100:
                scale = max(0.1, cfg.brightness / 100.0)
                preview = preview.point(lambda p: int(p * scale))

            # Push frame to hardware if connected
            if self._screen.is_connected:
                t0 = time.perf_counter()
                try:
                    nbytes = self._screen.push_frame(
                        frame,
                        brightness=cfg.brightness,
                        rotate_180=cfg.rotate_180,
                    )
                    self._last_frame_ms = (time.perf_counter() - t0) * 1000.0
                    self._last_frame_bytes = nbytes
                    self._status_text = (
                        f"已连接 {self._screen.port} ({self._screen.width}×{self._screen.height})"
                    )
                    self._status_level = "ok"
                except Exception as exc:
                    self._screen.close()
                    self._status_text = f"通信中断: {exc}"
                    self._status_level = "err"
                    if cfg.auto_reconnect:
                        self._next_reconnect_at = time.perf_counter() + 2.0
                    else:
                        with self._lock:
                            self._want_connected = False

            self._emit_ui(snap, preview, cfg)

            interval = 1.0 / max(0.5, cfg.refresh_hz)
            elapsed = time.perf_counter() - loop_start
            sleep_s = max(0.02, interval - elapsed)
            self._stop_event.wait(sleep_s)


class MSN01MonitorApp:
    """Main Tkinter desktop host window (Windows & Linux)."""

    def __init__(self, initial_port: Optional[str] = None) -> None:
        self._enable_windows_dpi()
        self.cfg = load_config()
        if initial_port:
            self.cfg.port = initial_port

        self.root = tk.Tk()
        self.root.title(f"{__app_name__} v{__version__}")
        self.root.configure(bg=UI_BG)
        self.root.resizable(False, False)

        self._ui_queue: "queue.Queue[_UiUpdate]" = queue.Queue(maxsize=2)
        self._preview_photo: Optional[ImageTk.PhotoImage] = None
        self._ports_cache: list[PortCandidate] = []
        self._gpu_choices: list[tuple[str, str]] = [("auto", "自动选择 (Auto)")]
        self._syncing_vars = False

        self._init_styles()
        self._build_ui()
        self._refresh_ports()

        self.worker = MonitorWorker(self.cfg, self._ui_queue)
        self.worker.start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._poll_ui_queue)

    @staticmethod
    def _enable_windows_dpi() -> None:
        if sys.platform.startswith("win"):
            try:
                import ctypes

                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass

    def _ui_font(self, size: int, bold: bool = False, mono: bool = False) -> tuple:
        if sys.platform.startswith("win"):
            family = "Consolas" if mono else "Microsoft YaHei UI"
        else:
            family = "DejaVu Sans Mono" if mono else "DejaVu Sans"
        return (family, size, "bold" if bold else "normal")

    def _init_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("Dark.TFrame", background=UI_BG)
        style.configure("Card.TFrame", background=UI_CARD)
        style.configure(
            "Dark.TCombobox",
            fieldbackground=UI_CARD_ALT,
            background=UI_CARD_ALT,
            foreground=UI_TEXT,
            arrowcolor=UI_ACCENT,
            bordercolor=UI_BORDER,
            lightcolor=UI_BORDER,
            darkcolor=UI_BORDER,
            selectbackground=UI_ACCENT,
            selectforeground=UI_BG,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", UI_CARD_ALT)],
            foreground=[("readonly", UI_TEXT)],
            background=[("active", UI_BORDER)],
        )
        style.configure(
            "Dark.Horizontal.TScale",
            background=UI_CARD,
            troughcolor=UI_BG,
            sliderrelief="flat",
        )
        style.configure(
            "Dark.TCheckbutton",
            background=UI_CARD,
            foreground=UI_TEXT,
            font=self._ui_font(9),
        )
        style.map(
            "Dark.TCheckbutton",
            background=[("active", UI_CARD)],
            foreground=[("active", UI_TEXT)],
        )

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg=UI_BG, padx=16, pady=14)
        outer.pack(fill="both", expand=True)

        # ---- Top Header ----
        header = tk.Frame(outer, bg=UI_BG)
        header.pack(fill="x", pady=(0, 12))

        title_box = tk.Frame(header, bg=UI_BG)
        title_box.pack(side="left")
        tk.Label(
            title_box,
            text="MSN01 硬件监控上位机",
            bg=UI_BG,
            fg=UI_TEXT,
            font=self._ui_font(14, bold=True),
        ).pack(anchor="w")
        os_tag = "Windows" if sys.platform.startswith("win") else ("Linux" if sys.platform.startswith("linux") else sys.platform)
        tk.Label(
            title_box,
            text=f"CH32x035 USB 迷你副屏 · 跨平台监控 ({os_tag})",
            bg=UI_BG,
            fg=UI_MUTED,
            font=self._ui_font(9),
        ).pack(anchor="w")

        self.status_badge = tk.Label(
            header,
            text="● 初始化中...",
            bg=UI_CARD,
            fg=UI_AMBER,
            font=self._ui_font(9, bold=True),
            padx=12,
            pady=6,
            highlightthickness=1,
            highlightbackground=UI_BORDER,
        )
        self.status_badge.pack(side="right", anchor="center")

        # ---- Main Two-Column Body ----
        body = tk.Frame(outer, bg=UI_BG)
        body.pack(fill="both", expand=True)

        left_col = tk.Frame(body, bg=UI_BG)
        left_col.pack(side="left", fill="y", padx=(0, 14))

        right_col = tk.Frame(body, bg=UI_BG)
        right_col.pack(side="left", fill="both", expand=True)

        # ---- Left Column: Live Screen Preview Bezel ----
        preview_card = tk.Frame(
            left_col,
            bg=UI_CARD,
            padx=14,
            pady=12,
            highlightthickness=1,
            highlightbackground=UI_BORDER,
        )
        preview_card.pack(fill="x")

        pv_hdr = tk.Frame(preview_card, bg=UI_CARD)
        pv_hdr.pack(fill="x", pady=(0, 8))
        tk.Label(
            pv_hdr,
            text="实时屏幕预览 (3× 放大)",
            bg=UI_CARD,
            fg=UI_TEXT,
            font=self._ui_font(10, bold=True),
        ).pack(side="left")
        self.perf_label = tk.Label(
            pv_hdr,
            text="160×80 · -- ms",
            bg=UI_CARD,
            fg=UI_MUTED,
            font=self._ui_font(9, mono=True),
        )
        self.perf_label.pack(side="right")

        bezel = tk.Frame(
            preview_card,
            bg="#020617",
            padx=10,
            pady=10,
            highlightthickness=2,
            highlightbackground="#475569",
        )
        bezel.pack()
        self.preview_label = tk.Label(bezel, bg="#070B14", width=480, height=240, bd=0)
        self.preview_label.pack()

        # ---- Left Column: 3 Real-Time Metric Readout Cards ----
        cards_row = tk.Frame(left_col, bg=UI_BG)
        cards_row.pack(fill="x", pady=(12, 0))

        self.cpu_val_lbl, self.cpu_sub_lbl = self._make_stat_card(cards_row, "CPU", "#00E5FF", padx=(0, 8))
        self.gpu_val_lbl, self.gpu_sub_lbl = self._make_stat_card(cards_row, "GPU", "#22F59B", padx=(0, 8))
        self.ram_val_lbl, self.ram_sub_lbl = self._make_stat_card(cards_row, "RAM", "#FFB020", padx=(0, 0))

        # ---- Right Column: Connection Settings ----
        conn_card = tk.Frame(
            right_col,
            bg=UI_CARD,
            padx=14,
            pady=12,
            highlightthickness=1,
            highlightbackground=UI_BORDER,
            width=310,
        )
        conn_card.pack(fill="x")

        tk.Label(
            conn_card,
            text="设备连接",
            bg=UI_CARD,
            fg=UI_TEXT,
            font=self._ui_font(10, bold=True),
        ).pack(anchor="w", pady=(0, 8))

        port_row = tk.Frame(conn_card, bg=UI_CARD)
        port_row.pack(fill="x", pady=(0, 8))
        self.port_var = tk.StringVar(value=self.cfg.port)
        self.port_combo = ttk.Combobox(
            port_row,
            textvariable=self.port_var,
            state="readonly",
            style="Dark.TCombobox",
            font=self._ui_font(9),
            width=24,
        )
        self.port_combo.pack(side="left", fill="x", expand=True)
        self.port_combo.bind("<<ComboboxSelected>>", self._on_port_selected)

        tk.Button(
            port_row,
            text="刷新",
            command=self._refresh_ports,
            bg=UI_CARD_ALT,
            fg=UI_TEXT,
            activebackground=UI_BORDER,
            activeforeground=UI_TEXT,
            relief="flat",
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
            font=self._ui_font(9),
        ).pack(side="left", padx=(6, 0))

        self.conn_btn = tk.Button(
            conn_card,
            text="断开连接" if self.cfg.auto_connect else "连接并开始监控",
            command=self._toggle_connect,
            bg=UI_ACCENT,
            fg="#041018",
            activebackground=UI_ACCENT_HOVER,
            activeforeground="#041018",
            relief="flat",
            bd=0,
            pady=7,
            cursor="hand2",
            font=self._ui_font(10, bold=True),
        )
        self.conn_btn.pack(fill="x", pady=(0, 8))

        self.reconnect_var = tk.BooleanVar(value=self.cfg.auto_reconnect)
        ttk.Checkbutton(
            conn_card,
            text="断线后自动重连",
            variable=self.reconnect_var,
            command=self._on_settings_changed,
            style="Dark.TCheckbutton",
        ).pack(anchor="w")

        # ---- Right Column: Display & Telemetry Settings ----
        disp_card = tk.Frame(
            right_col,
            bg=UI_CARD,
            padx=14,
            pady=12,
            highlightthickness=1,
            highlightbackground=UI_BORDER,
        )
        disp_card.pack(fill="both", expand=True, pady=(12, 0))

        tk.Label(
            disp_card,
            text="显示与监控配置",
            bg=UI_CARD,
            fg=UI_TEXT,
            font=self._ui_font(10, bold=True),
        ).pack(anchor="w", pady=(0, 8))

        # GPU selector
        tk.Label(disp_card, text="监控显卡 (GPU)", bg=UI_CARD, fg=UI_MUTED, font=self._ui_font(9)).pack(anchor="w")
        self.gpu_var = tk.StringVar(value="自动选择 (Auto)")
        self.gpu_combo = ttk.Combobox(
            disp_card,
            textvariable=self.gpu_var,
            state="readonly",
            style="Dark.TCombobox",
            font=self._ui_font(9),
            values=["自动选择 (Auto)"],
        )
        self.gpu_combo.pack(fill="x", pady=(2, 8))
        self.gpu_combo.bind("<<ComboboxSelected>>", self._on_gpu_selected)

        # Theme selector
        tk.Label(disp_card, text="仪表盘主题", bg=UI_CARD, fg=UI_MUTED, font=self._ui_font(9)).pack(anchor="w")
        self._theme_label_to_id = {t.name: t.id for t in THEMES}
        self._theme_id_to_label = {t.id: t.name for t in THEMES}
        self.theme_var = tk.StringVar(value=self._theme_id_to_label.get(self.cfg.theme, THEMES[0].name))
        self.theme_combo = ttk.Combobox(
            disp_card,
            textvariable=self.theme_var,
            state="readonly",
            style="Dark.TCombobox",
            font=self._ui_font(9),
            values=[t.name for t in THEMES],
        )
        self.theme_combo.pack(fill="x", pady=(2, 10))
        self.theme_combo.bind("<<ComboboxSelected>>", self._on_theme_selected)

        # Refresh rate slider
        fps_hdr = tk.Frame(disp_card, bg=UI_CARD)
        fps_hdr.pack(fill="x")
        tk.Label(fps_hdr, text="刷新频率", bg=UI_CARD, fg=UI_MUTED, font=self._ui_font(9)).pack(side="left")
        self.fps_lbl = tk.Label(
            fps_hdr, text=f"{self.cfg.refresh_hz:.1f} Hz", bg=UI_CARD, fg=UI_ACCENT, font=self._ui_font(9, bold=True, mono=True)
        )
        self.fps_lbl.pack(side="right")
        self.fps_var = tk.DoubleVar(value=self.cfg.refresh_hz)
        ttk.Scale(
            disp_card,
            from_=0.5,
            to=10.0,
            variable=self.fps_var,
            command=self._on_fps_slide,
            style="Dark.Horizontal.TScale",
        ).pack(fill="x", pady=(2, 8))

        # Brightness slider
        br_hdr = tk.Frame(disp_card, bg=UI_CARD)
        br_hdr.pack(fill="x")
        tk.Label(br_hdr, text="软件亮度", bg=UI_CARD, fg=UI_MUTED, font=self._ui_font(9)).pack(side="left")
        self.bright_lbl = tk.Label(
            br_hdr, text=f"{self.cfg.brightness}%", bg=UI_CARD, fg=UI_ACCENT, font=self._ui_font(9, bold=True, mono=True)
        )
        self.bright_lbl.pack(side="right")
        self.bright_var = tk.IntVar(value=self.cfg.brightness)
        ttk.Scale(
            disp_card,
            from_=10,
            to=100,
            variable=self.bright_var,
            command=self._on_bright_slide,
            style="Dark.Horizontal.TScale",
        ).pack(fill="x", pady=(2, 10))

        # Toggles
        self.rotate_var = tk.BooleanVar(value=self.cfg.rotate_180)
        ttk.Checkbutton(
            disp_card,
            text="画面旋转 180°",
            variable=self.rotate_var,
            command=self._on_settings_changed,
            style="Dark.TCheckbutton",
        ).pack(anchor="w", pady=(0, 4))

        self.touch_var = tk.BooleanVar(value=self.cfg.touch_switch_theme)
        ttk.Checkbutton(
            disp_card,
            text="轻触机身按键切换主题",
            variable=self.touch_var,
            command=self._on_settings_changed,
            style="Dark.TCheckbutton",
        ).pack(anchor="w", pady=(0, 8))

        self.touch_lbl = tk.Label(
            disp_card,
            text="触摸传感器 ADC: --",
            bg=UI_CARD,
            fg=UI_MUTED,
            font=self._ui_font(8, mono=True),
        )
        self.touch_lbl.pack(anchor="w")

        # ---- Footer Status Bar ----
        footer = tk.Frame(outer, bg=UI_BG)
        footer.pack(fill="x", pady=(10, 0))
        self.footer_lbl = tk.Label(
            footer,
            text="正在启动监控线程...",
            bg=UI_BG,
            fg=UI_MUTED,
            font=self._ui_font(9),
        )
        self.footer_lbl.pack(side="left")

    def _make_stat_card(
        self, parent: tk.Frame, title: str, accent: str, padx: tuple[int, int]
    ) -> tuple[tk.Label, tk.Label]:
        card = tk.Frame(
            parent,
            bg=UI_CARD,
            padx=12,
            pady=10,
            highlightthickness=1,
            highlightbackground=UI_BORDER,
            width=165,
            height=92,
        )
        card.pack(side="left", fill="both", expand=True, padx=padx)
        card.pack_propagate(False)

        top = tk.Frame(card, bg=UI_CARD)
        top.pack(fill="x")
        tk.Frame(top, bg=accent, width=4, height=14).pack(side="left", padx=(0, 6))
        tk.Label(top, text=title, bg=UI_CARD, fg=accent, font=self._ui_font(10, bold=True)).pack(side="left")

        val_lbl = tk.Label(card, text="--%", bg=UI_CARD, fg=UI_TEXT, font=self._ui_font(18, bold=True, mono=True))
        val_lbl.pack(anchor="w", pady=(4, 2))

        sub_lbl = tk.Label(card, text="--", bg=UI_CARD, fg=UI_MUTED, font=self._ui_font(8))
        sub_lbl.pack(anchor="w")
        return val_lbl, sub_lbl

    # ---- Event Handlers ----
    def _refresh_ports(self) -> None:
        self._ports_cache = list_serial_ports()
        labels = ["自动检测 (Auto)"] + [p.label for p in self._ports_cache]
        self._port_label_to_dev = {"自动检测 (Auto)": "auto"}
        self._port_dev_to_label = {"auto": "自动检测 (Auto)"}
        for p in self._ports_cache:
            self._port_label_to_dev[p.label] = p.device
            self._port_dev_to_label[p.device] = p.label

        self.port_combo["values"] = labels
        target_label = self._port_dev_to_label.get(self.cfg.port, "自动检测 (Auto)")
        self.port_var.set(target_label)

    def _on_port_selected(self, _event=None) -> None:
        dev = self._port_label_to_dev.get(self.port_var.get(), "auto")
        self.cfg.port = dev
        save_config(self.cfg)
        self.worker.update_config(port=dev)

    def _toggle_connect(self) -> None:
        if self.conn_btn["text"].startswith("断开"):
            self.cfg.auto_connect = False
            save_config(self.cfg)
            self.worker.request_disconnect()
            self.conn_btn.configure(text="连接并开始监控", bg=UI_ACCENT, fg="#041018")
        else:
            self.cfg.auto_connect = True
            save_config(self.cfg)
            self.worker.request_connect()
            self.conn_btn.configure(text="断开连接", bg=UI_RED, fg=UI_TEXT)

    def _on_gpu_selected(self, _event=None) -> None:
        sel_label = self.gpu_var.get()
        gid = "auto"
        for k, lbl in self._gpu_choices:
            if lbl == sel_label:
                gid = k
                break
        self.cfg.gpu_id = gid
        save_config(self.cfg)
        self.worker.update_config(gpu_id=gid)

    def _on_theme_selected(self, _event=None) -> None:
        if self._syncing_vars:
            return
        tid = self._theme_label_to_id.get(self.theme_var.get(), "cyber_bars")
        self.cfg.theme = tid
        save_config(self.cfg)
        self.worker.update_config(theme=tid)

    def _on_fps_slide(self, val: str) -> None:
        hz = round(float(val) * 2) / 2.0  # 0.5 Hz increments
        self.fps_lbl.configure(text=f"{hz:.1f} Hz")
        self.cfg.refresh_hz = hz
        self.worker.update_config(refresh_hz=hz)
        save_config(self.cfg)

    def _on_bright_slide(self, val: str) -> None:
        br = int(round(float(val)))
        self.bright_lbl.configure(text=f"{br}%")
        self.cfg.brightness = br
        self.worker.update_config(brightness=br)
        save_config(self.cfg)

    def _on_settings_changed(self) -> None:
        self.cfg.rotate_180 = bool(self.rotate_var.get())
        self.cfg.touch_switch_theme = bool(self.touch_var.get())
        self.cfg.auto_reconnect = bool(self.reconnect_var.get())
        save_config(self.cfg)
        self.worker.update_config(
            rotate_180=self.cfg.rotate_180,
            touch_switch_theme=self.cfg.touch_switch_theme,
            auto_reconnect=self.cfg.auto_reconnect,
        )

    def _sync_gpu_dropdown(self, gpus: list[GpuSample]) -> None:
        new_choices = [("auto", "自动选择 (Auto)")]
        for g in gpus:
            new_choices.append((g.id, f"{g.short_name} [{g.source}]"))
        if new_choices != self._gpu_choices:
            self._gpu_choices = new_choices
            self.gpu_combo["values"] = [lbl for _, lbl in new_choices]
            cur_lbl = "自动选择 (Auto)"
            for k, lbl in new_choices:
                if k == self.cfg.gpu_id:
                    cur_lbl = lbl
                    break
            self.gpu_var.set(cur_lbl)

    def _poll_ui_queue(self) -> None:
        try:
            upd = self._ui_queue.get_nowait()
        except queue.Empty:
            upd = None

        if upd is not None:
            snap = upd.snapshot
            self._sync_gpu_dropdown(snap.gpus)

            # Sync theme dropdown if physical touch button cycled it
            expected_theme_lbl = self._theme_id_to_label.get(upd.theme_id)
            if expected_theme_lbl and self.theme_var.get() != expected_theme_lbl:
                self._syncing_vars = True
                self.theme_var.set(expected_theme_lbl)
                self.cfg.theme = upd.theme_id
                self._syncing_vars = False

            # Update 3x live preview image (pixel-crisp NEAREST upscale)
            preview_scaled = upd.preview_img.resize((480, 240), Image.Resampling.NEAREST)
            self._preview_photo = ImageTk.PhotoImage(preview_scaled)
            self.preview_label.configure(image=self._preview_photo)

            # Update CPU / GPU / RAM cards
            self.cpu_val_lbl.configure(text=f"{snap.cpu_percent:.0f}%")
            cpu_parts = []
            if snap.cpu_freq_mhz:
                cpu_parts.append(f"{snap.cpu_freq_mhz / 1000:.2f} GHz")
            if snap.cpu_temp_c is not None:
                cpu_parts.append(f"{snap.cpu_temp_c:.0f}°C")
            cpu_parts.append(f"{snap.cpu_cores_physical}C/{snap.cpu_cores_logical}T")
            self.cpu_sub_lbl.configure(text=" · ".join(cpu_parts))

            if snap.selected_gpu is not None:
                g = snap.selected_gpu
                self.gpu_val_lbl.configure(text=f"{g.util_percent:.0f}%")
                g_parts = [g.short_name]
                if g.temp_c is not None:
                    g_parts.append(f"{g.temp_c:.0f}°C")
                elif g.vram_text:
                    g_parts.append(f"VRAM {g.vram_text}")
                self.gpu_sub_lbl.configure(text=" · ".join(g_parts)[:24])
            else:
                self.gpu_val_lbl.configure(text="N/A")
                self.gpu_sub_lbl.configure(text="未检测到 GPU 指标")

            self.ram_val_lbl.configure(text=f"{snap.ram_percent:.0f}%")
            self.ram_sub_lbl.configure(text=f"{snap.ram_used_gb:.1f} / {snap.ram_total_gb:.1f} GB")

            # Connection badge & button sync
            badge_colors = {"ok": UI_GREEN, "warn": UI_AMBER, "err": UI_RED, "idle": UI_MUTED}
            self.status_badge.configure(
                text=f"● {upd.status_text}",
                fg=badge_colors.get(upd.status_level, UI_MUTED),
            )
            if upd.connected:
                self.conn_btn.configure(text="断开连接", bg=UI_RED, fg=UI_TEXT)
                self.perf_label.configure(
                    text=f"{upd.screen_size[0]}×{upd.screen_size[1]} · {upd.frame_ms:.1f}ms · {upd.frame_bytes / 1024:.1f}KB"
                )
            else:
                if self.cfg.auto_connect and upd.status_level in ("warn", "err"):
                    self.conn_btn.configure(text="停止重试 / 断开", bg=UI_RED, fg=UI_TEXT)
                else:
                    self.conn_btn.configure(text="连接并开始监控", bg=UI_ACCENT, fg="#041018")
                self.perf_label.configure(text=f"{upd.screen_size[0]}×{upd.screen_size[1]} · 离线预览")

            adc_text = f"触摸传感器 ADC: {upd.touch_adc}" if upd.touch_adc is not None else "触摸传感器 ADC: --"
            self.touch_lbl.configure(text=adc_text)
            self.footer_lbl.configure(
                text=f"端口: {upd.port_text}  |  主题: {upd.theme_id}  |  刷新: {self.cfg.refresh_hz:.1f} Hz"
            )

        self.root.after(80, self._poll_ui_queue)

    def _on_close(self) -> None:
        save_config(self.cfg)
        self.worker.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def launch_gui(initial_port: Optional[str] = None) -> None:
    app = MSN01MonitorApp(initial_port=initial_port)
    app.run()

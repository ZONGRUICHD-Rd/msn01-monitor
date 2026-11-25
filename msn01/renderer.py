"""Crisp 160x80 (and 320x172 auto-scaled) CPU/GPU/RAM dashboard renderer."""

from __future__ import annotations

import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont

from .telemetry import SystemSnapshot


@dataclass(frozen=True)
class ThemeInfo:
    id: str
    name: str
    description: str


THEMES: list[ThemeInfo] = [
    ThemeInfo("cyber_bars", "赛博进度条 (Cyber Bars)", "三行高对比度分段进度条 + 频率/显存/内存详情"),
    ThemeInfo("spark_graph", "实时折线图 (Telemetry Graph)", "左侧实时数值 + 右侧三通道动态波形曲线"),
    ThemeInfo("triple_gauge", "三连仪表盘 (Triple Gauges)", "CPU / GPU / RAM 三联环形仪表盘"),
    ThemeInfo("clock_hud", "时钟看板 (Clock HUD)", "顶部实时时钟 + 底部三联大数字卡片"),
]

THEME_IDS: list[str] = [t.id for t in THEMES]

# High-contrast IPS color palette
BG_COLOR = (7, 11, 20)
CARD_BG = (14, 21, 37)
CARD_BORDER = (26, 40, 68)
TRACK_BG = (24, 36, 58)
TEXT_BRIGHT = (244, 248, 255)
TEXT_MUTED = (142, 163, 194)
TEXT_DIM = (92, 112, 144)

COLOR_CPU = (0, 229, 255)    # Electric cyan
COLOR_GPU = (34, 245, 155)   # Neon emerald
COLOR_RAM = (255, 176, 32)   # Warm amber
COLOR_WARN = (255, 138, 0)   # Orange >= 75%
COLOR_CRIT = (255, 59, 92)   # Coral red >= 90%


def _usage_color(pct: float, base: tuple[int, int, int]) -> tuple[int, int, int]:
    if pct >= 90.0:
        return COLOR_CRIT
    if pct >= 75.0:
        return COLOR_WARN
    return base


def _find_font_path(bold: bool = True, mono: bool = False) -> Optional[str]:
    if sys.platform.startswith("win"):
        fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        if mono:
            names = ["consolab.ttf", "consola.ttf", "lucon.ttf"] if bold else ["consola.ttf", "consolab.ttf"]
        else:
            names = ["segoeuib.ttf", "arialbd.ttf", "seguisb.ttf", "consolab.ttf"] if bold else ["segoeui.ttf", "arial.ttf", "consola.ttf"]
        for n in names:
            p = os.path.join(fonts_dir, n)
            if os.path.exists(p):
                return p
    else:
        if mono:
            candidates = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
                "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
                "/usr/share/fonts/truetype/ubuntu/UbuntuMono-B.ttf",
            ]
        else:
            candidates = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            ]
        for p in candidates:
            if os.path.exists(p):
                return p
    return None


class _FontCache:
    def __init__(self) -> None:
        self._sans_bold = _find_font_path(bold=True, mono=False)
        self._sans_reg = _find_font_path(bold=False, mono=False)
        self._mono_bold = _find_font_path(bold=True, mono=True)
        self._cache: dict[tuple[str, int], ImageFont.ImageFont] = {}

    def get(self, kind: str, size: int) -> ImageFont.ImageFont:
        key = (kind, size)
        if key in self._cache:
            return self._cache[key]

        path = {
            "bold": self._sans_bold,
            "reg": self._sans_reg or self._sans_bold,
            "mono": self._mono_bold or self._sans_bold,
        }.get(kind)

        font: ImageFont.ImageFont
        if path:
            try:
                font = ImageFont.truetype(path, size=size)
            except Exception:
                font = self._fallback(size)
        else:
            font = self._fallback(size)

        self._cache[key] = font
        return font

    @staticmethod
    def _fallback(size: int) -> ImageFont.ImageFont:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


class DashboardRenderer:
    """Renders real-time CPU, GPU, RAM dashboards for MSN01 displays."""

    def __init__(self, history_len: int = 46) -> None:
        self._fonts = _FontCache()
        self.cpu_hist: deque[float] = deque([0.0] * history_len, maxlen=history_len)
        self.gpu_hist: deque[float] = deque([0.0] * history_len, maxlen=history_len)
        self.ram_hist: deque[float] = deque([0.0] * history_len, maxlen=history_len)
        self._renderers: dict[str, Callable[[SystemSnapshot], Image.Image]] = {
            "cyber_bars": self._render_cyber_bars,
            "spark_graph": self._render_spark_graph,
            "triple_gauge": self._render_triple_gauge,
            "clock_hud": self._render_clock_hud,
        }

    def push_history(self, snap: SystemSnapshot) -> None:
        self.cpu_hist.append(snap.cpu_percent)
        self.gpu_hist.append(snap.selected_gpu.util_percent if snap.selected_gpu else 0.0)
        self.ram_hist.append(snap.ram_percent)

    def render(
        self,
        snap: SystemSnapshot,
        theme: str = "cyber_bars",
        size: tuple[int, int] = (160, 80),
        record_history: bool = True,
    ) -> Image.Image:
        if record_history:
            self.push_history(snap)
        fn = self._renderers.get(theme, self._render_cyber_bars)
        img = fn(snap)
        if img.size != size:
            img = img.resize(size, Image.Resampling.LANCZOS)
        return img

    @staticmethod
    def _cpu_subtext(snap: SystemSnapshot) -> str:
        if snap.cpu_temp_c is not None:
            return f"{snap.cpu_temp_c:.0f}C"
        if snap.cpu_freq_mhz is not None:
            return f"{snap.cpu_freq_mhz / 1000:.1f}GHz"
        return f"{snap.cpu_cores_logical}T"

    @staticmethod
    def _gpu_subtext(snap: SystemSnapshot) -> str:
        g = snap.selected_gpu
        if g is None:
            return "N/A"
        parts: list[str] = []
        if g.temp_c is not None:
            parts.append(f"{g.temp_c:.0f}C")
        if g.vram_text:
            parts.append(g.vram_text)
        if parts:
            return " ".join(parts)[:10]
        return g.vendor

    @staticmethod
    def _ram_subtext(snap: SystemSnapshot) -> str:
        return f"{snap.ram_used_gb:.1f}/{snap.ram_total_gb:.0f}G"

    def _draw_text(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        text: str,
        font: ImageFont.ImageFont,
        fill: tuple[int, int, int],
        anchor: str = "la",
    ) -> None:
        try:
            draw.text(xy, text, font=font, fill=fill, anchor=anchor)
        except Exception:
            draw.text(xy, text, font=font, fill=fill)

    def _draw_segmented_bar(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        w: int,
        h: int,
        pct: float,
        color: tuple[int, int, int],
        segments: int = 24,
    ) -> None:
        pct = max(0.0, min(100.0, pct))
        gap = 1
        seg_w = max(2, (w - (segments - 1) * gap) // segments)
        total_w = segments * seg_w + (segments - 1) * gap
        start_x = x + (w - total_w) // 2
        filled = int(round((pct / 100.0) * segments))
        if pct > 0.5 and filled == 0:
            filled = 1

        for i in range(segments):
            sx = start_x + i * (seg_w + gap)
            c = color if i < filled else TRACK_BG
            draw.rectangle((sx, y, sx + seg_w - 1, y + h - 1), fill=c)

    # ------------------------------------------------------------------
    # Theme 1: Cyber Bars (Default 160x80)
    # ------------------------------------------------------------------
    def _render_cyber_bars(self, snap: SystemSnapshot) -> Image.Image:
        img = Image.new("RGB", (160, 80), BG_COLOR)
        draw = ImageDraw.Draw(img)

        f_tag = self._fonts.get("bold", 10)
        f_val = self._fonts.get("mono", 12)
        f_sub = self._fonts.get("reg", 9)

        gpu_pct = snap.selected_gpu.util_percent if snap.selected_gpu else 0.0
        rows = [
            ("CPU", snap.cpu_percent, self._cpu_subtext(snap), COLOR_CPU),
            ("GPU", gpu_pct, self._gpu_subtext(snap), COLOR_GPU),
            ("RAM", snap.ram_percent, self._ram_subtext(snap), COLOR_RAM),
        ]

        card_h = 24
        for idx, (label, pct, sub, base_col) in enumerate(rows):
            y0 = 2 + idx * 26
            y1 = y0 + card_h - 1
            col = _usage_color(pct, base_col)

            # Card surface + subtle border
            draw.rounded_rectangle((2, y0, 157, y1), radius=3, fill=CARD_BG, outline=CARD_BORDER, width=1)
            # Left neon accent bar
            draw.rectangle((3, y0 + 2, 5, y1 - 2), fill=col)

            # Tag label
            self._draw_text(draw, (9, y0 + 8), label, f_tag, base_col, anchor="lm")

            # Percentage value
            pct_str = f"{pct:3.0f}%" if (label != "GPU" or snap.selected_gpu is not None) else " --%"
            self._draw_text(draw, (78, y0 + 8), pct_str, f_val, TEXT_BRIGHT if pct < 90 else COLOR_CRIT, anchor="rm")

            # Secondary info right-aligned
            self._draw_text(draw, (153, y0 + 8), sub, f_sub, TEXT_MUTED, anchor="rm")

            # Segmented neon progress bar
            self._draw_segmented_bar(draw, 9, y0 + 16, 144, 5, pct, col, segments=24)

        return img

    # ------------------------------------------------------------------
    # Theme 2: Telemetry Sparkline Graph (160x80)
    # ------------------------------------------------------------------
    def _render_spark_graph(self, snap: SystemSnapshot) -> Image.Image:
        img = Image.new("RGB", (160, 80), BG_COLOR)
        draw = ImageDraw.Draw(img)

        f_tag = self._fonts.get("bold", 9)
        f_val = self._fonts.get("mono", 12)
        f_sub = self._fonts.get("reg", 8)

        gpu_pct = snap.selected_gpu.util_percent if snap.selected_gpu else 0.0
        cpu_short = f"{snap.cpu_temp_c:.0f}C" if snap.cpu_temp_c is not None else (
            f"{snap.cpu_freq_mhz / 1000:.1f}G" if snap.cpu_freq_mhz else f"{snap.cpu_cores_logical}T"
        )
        metrics = [
            ("CPU", snap.cpu_percent, cpu_short, COLOR_CPU),
            ("GPU", gpu_pct, self._gpu_subtext(snap)[:5], COLOR_GPU),
            ("RAM", snap.ram_percent, f"{snap.ram_used_gb:.0f}G", COLOR_RAM),
        ]

        # Left column: 3 compact readouts
        for idx, (label, pct, sub, col) in enumerate(metrics):
            y0 = 2 + idx * 26
            y1 = y0 + 23
            draw.rounded_rectangle((2, y0, 62, y1), radius=3, fill=CARD_BG, outline=CARD_BORDER, width=1)
            draw.rectangle((3, y0 + 3, 5, y1 - 3), fill=col)
            self._draw_text(draw, (8, y0 + 7), label, f_tag, col, anchor="lm")
            self._draw_text(draw, (8, y0 + 17), sub, f_sub, TEXT_DIM, anchor="lm")
            pct_s = f"{pct:2.0f}%" if (label != "GPU" or snap.selected_gpu is not None) else "--"
            val_font = self._fonts.get("mono", 10 if pct >= 99.5 else 12)
            self._draw_text(draw, (60, y0 + 12), pct_s, val_font, _usage_color(pct, TEXT_BRIGHT), anchor="rm")

        # Right panel: live oscilloscope waveform
        gx0, gy0, gx1, gy1 = 65, 2, 157, 77
        draw.rounded_rectangle((gx0, gy0, gx1, gy1), radius=3, fill=(10, 16, 29), outline=CARD_BORDER, width=1)

        plot_x0, plot_y0 = gx0 + 3, gy0 + 4
        plot_x1, plot_y1 = gx1 - 3, gy1 - 4
        plot_w = plot_x1 - plot_x0
        plot_h = plot_y1 - plot_y0

        # Horizontal reference grid lines (25%, 50%, 75%)
        for frac in (0.25, 0.50, 0.75):
            gy = int(plot_y1 - frac * plot_h)
            draw.line((plot_x0, gy, plot_x1, gy), fill=(22, 34, 56), width=1)
        # Vertical grid lines
        for frac in (0.33, 0.66):
            gx = int(plot_x0 + frac * plot_w)
            draw.line((gx, plot_y0, gx, plot_y1), fill=(18, 28, 46), width=1)

        # Draw waveforms (RAM back, GPU mid, CPU front)
        series = [
            (self.ram_hist, COLOR_RAM),
            (self.gpu_hist, COLOR_GPU),
            (self.cpu_hist, COLOR_CPU),
        ]
        for hist, col in series:
            n = len(hist)
            if n < 2:
                continue
            pts = []
            for i, val in enumerate(hist):
                px = plot_x0 + (i / (n - 1)) * plot_w
                py = plot_y1 - (max(0.0, min(100.0, val)) / 100.0) * plot_h
                pts.append((px, py))
            draw.line(pts, fill=col, width=1)
            lx, ly = pts[-1]
            draw.ellipse((lx - 1.5, ly - 1.5, lx + 1.5, ly + 1.5), fill=col)

        return img

    # ------------------------------------------------------------------
    # Theme 3: Triple Circular Gauges (160x80)
    # ------------------------------------------------------------------
    def _render_triple_gauge(self, snap: SystemSnapshot) -> Image.Image:
        # Render at 2x (320x160) for razor-smooth anti-aliased arcs, then downsample to 160x80!
        scale = 2
        W, H = 160 * scale, 80 * scale
        img = Image.new("RGB", (W, H), BG_COLOR)
        draw = ImageDraw.Draw(img)

        f_tag = self._fonts.get("bold", 10 * scale)
        f_val = self._fonts.get("mono", 12 * scale)
        f_sub = self._fonts.get("reg", 8 * scale)

        gpu_pct = snap.selected_gpu.util_percent if snap.selected_gpu else 0.0
        items = [
            ("CPU", snap.cpu_percent, self._cpu_subtext(snap)[:6], COLOR_CPU),
            ("GPU", gpu_pct, self._gpu_subtext(snap)[:6], COLOR_GPU),
            ("RAM", snap.ram_percent, f"{snap.ram_used_gb:.1f}G", COLOR_RAM),
        ]

        card_w = 50 * scale
        gap = 3 * scale
        start_x = 2 * scale

        for idx, (label, pct, sub, base_col) in enumerate(items):
            x0 = start_x + idx * (card_w + gap)
            x1 = x0 + card_w - 1
            y0, y1 = 2 * scale, 77 * scale
            cx = (x0 + x1) // 2
            col = _usage_color(pct, base_col)

            draw.rounded_rectangle((x0, y0, x1, y1), radius=4 * scale, fill=CARD_BG, outline=CARD_BORDER, width=scale)

            # Top label
            self._draw_text(draw, (cx, y0 + 8 * scale), label, f_tag, base_col, anchor="mm")

            # Arc gauge (270 degrees: 135 -> 405)
            r = 18 * scale
            cy = y0 + 38 * scale
            bbox = (cx - r, cy - r, cx + r, cy + r)
            arc_w = 4 * scale
            draw.arc(bbox, start=135, end=405, fill=TRACK_BG, width=arc_w)
            sweep = (max(0.0, min(100.0, pct)) / 100.0) * 270.0
            if sweep >= 1.0:
                draw.arc(bbox, start=135, end=135 + sweep, fill=col, width=arc_w)

            # Center value
            pct_s = f"{pct:.0f}" if (label != "GPU" or snap.selected_gpu is not None) else "--"
            self._draw_text(draw, (cx, cy), pct_s, f_val, TEXT_BRIGHT, anchor="mm")

            # Bottom sub-metric
            self._draw_text(draw, (cx, y1 - 8 * scale), sub, f_sub, TEXT_MUTED, anchor="mm")

        return img.resize((160, 80), Image.Resampling.LANCZOS)

    # ------------------------------------------------------------------
    # Theme 4: Clock + Minimal Metric Tiles (160x80)
    # ------------------------------------------------------------------
    def _render_clock_hud(self, snap: SystemSnapshot) -> Image.Image:
        img = Image.new("RGB", (160, 80), BG_COLOR)
        draw = ImageDraw.Draw(img)

        f_clock = self._fonts.get("mono", 15)
        f_date = self._fonts.get("reg", 9)
        f_tag = self._fonts.get("bold", 9)
        f_val = self._fonts.get("mono", 14)
        f_sub = self._fonts.get("reg", 8)

        now = time.localtime(snap.timestamp)
        clock_str = time.strftime("%H:%M:%S", now)
        date_str = time.strftime("%m-%d %a", now)

        # Top clock header
        draw.rounded_rectangle((2, 2, 157, 23), radius=3, fill=CARD_BG, outline=CARD_BORDER, width=1)
        self._draw_text(draw, (8, 12), clock_str, f_clock, TEXT_BRIGHT, anchor="lm")
        self._draw_text(draw, (153, 12), date_str, f_date, COLOR_CPU, anchor="rm")

        gpu_pct = snap.selected_gpu.util_percent if snap.selected_gpu else 0.0
        tiles = [
            ("CPU", snap.cpu_percent, self._cpu_subtext(snap)[:6], COLOR_CPU),
            ("GPU", gpu_pct, self._gpu_subtext(snap)[:6], COLOR_GPU),
            ("RAM", snap.ram_percent, f"{snap.ram_used_gb:.1f}G", COLOR_RAM),
        ]

        tile_w = 50
        gap = 3
        for idx, (label, pct, sub, base_col) in enumerate(tiles):
            x0 = 2 + idx * (tile_w + gap)
            x1 = x0 + tile_w - 1
            y0, y1 = 27, 77
            cx = (x0 + x1) // 2
            col = _usage_color(pct, base_col)

            draw.rounded_rectangle((x0, y0, x1, y1), radius=3, fill=CARD_BG, outline=CARD_BORDER, width=1)
            self._draw_text(draw, (cx, y0 + 7), label, f_tag, base_col, anchor="mm")

            pct_s = f"{pct:.0f}%" if (label != "GPU" or snap.selected_gpu is not None) else "--"
            self._draw_text(draw, (cx, y0 + 23), pct_s, f_val, TEXT_BRIGHT if pct < 90 else COLOR_CRIT, anchor="mm")
            self._draw_text(draw, (cx, y0 + 36), sub, f_sub, TEXT_MUTED, anchor="mm")

            # Bottom accent fill bar
            bar_x0, bar_x1 = x0 + 4, x1 - 4
            bar_y0, bar_y1 = y1 - 6, y1 - 3
            draw.rectangle((bar_x0, bar_y0, bar_x1, bar_y1), fill=TRACK_BG)
            fill_w = int((bar_x1 - bar_x0) * max(0.0, min(100.0, pct)) / 100.0)
            if fill_w > 0:
                draw.rectangle((bar_x0, bar_y0, bar_x0 + fill_w, bar_y1), fill=col)

        return img

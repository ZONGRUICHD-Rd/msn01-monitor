#!/usr/bin/env python3
"""MSN01 USB Mini Screen CPU/GPU/RAM Monitor — Cross-Platform Entry Point (Windows & Linux).

Usage:
    python main.py                        # Launch desktop GUI (上位机)
    python main.py --cli                  # Run headless CLI mode (great for Linux servers)
    python main.py --cli --port COM4 --theme cyber_bars --fps 2
    python main.py --list-ports           # List detected serial ports
    python main.py --list-gpus            # List detected GPUs and live readings
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

from msn01 import __app_name__, __version__
from msn01.config import load_config
from msn01.protocol import MSN01Screen, list_serial_ports
from msn01.renderer import THEME_IDS, THEMES, DashboardRenderer
from msn01.telemetry import TelemetryCollector


def cmd_list_ports() -> int:
    ports = list_serial_ports()
    if not ports:
        print("未检测到任何串口设备。")
        return 0
    print(f"检测到 {len(ports)} 个串口设备：")
    for p in ports:
        print(f"  - {p.label}  SN={p.serial_number or '-'}")
    return 0


def cmd_list_gpus() -> int:
    collector = TelemetryCollector()
    time.sleep(0.4)
    snap = collector.sample()
    collector.close()
    print(f"CPU: {snap.cpu_percent:.1f}%  RAM: {snap.ram_used_gb:.1f}/{snap.ram_total_gb:.1f} GB ({snap.ram_percent:.1f}%)")
    if not snap.gpus:
        print("未检测到可用的 GPU 指标后端。")
        return 0
    print(f"检测到 {len(snap.gpus)} 个 GPU 指标源：")
    for g in snap.gpus:
        vram = f" VRAM={g.vram_text}" if g.vram_text else ""
        temp = f" Temp={g.temp_c:.0f}C" if g.temp_c is not None else ""
        print(f"  - id={g.id}  name={g.name}  util={g.util_percent:.1f}%{vram}{temp}  [{g.source}]")
    return 0


def run_cli(args: argparse.Namespace) -> int:
    cfg = load_config()
    port = args.port or cfg.port
    theme = args.theme or cfg.theme
    fps = args.fps if args.fps is not None else cfg.refresh_hz
    brightness = args.brightness if args.brightness is not None else cfg.brightness
    rotate_180 = args.rotate_180 or cfg.rotate_180
    gpu_id = args.gpu or cfg.gpu_id
    touch_switch = not args.no_touch

    screen = MSN01Screen()
    telemetry = TelemetryCollector()
    renderer = DashboardRenderer()

    running = True

    def _handle_sig(_sig, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _handle_sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sig)

    print(f"[{__app_name__} v{__version__}] CLI 模式启动 (theme={theme}, fps={fps}, brightness={brightness}%)")
    if not args.once:
        print("按 Ctrl+C 停止监控。")

    was_touched = False
    last_touch_switch = 0.0
    interval = 1.0 / max(0.5, fps)

    try:
        while running:
            t0 = time.perf_counter()

            if not screen.is_connected:
                try:
                    w, h = screen.connect(port)
                    print(f"[Connected] 端口={screen.port} 分辨率={w}x{h} flash_id={hex(screen.flash_id) if screen.flash_id else 'N/A'}")
                except Exception as exc:
                    print(f"[Wait] {exc}", flush=True)
                    if args.once:
                        return 1
                    time.sleep(2.0)
                    continue

            if touch_switch and not args.once:
                adc = screen.read_touch_adc()
                if adc is not None:
                    touched = adc < 3600
                    now = time.perf_counter()
                    if touched and not was_touched and (now - last_touch_switch) >= 0.7:
                        idx = THEME_IDS.index(theme) if theme in THEME_IDS else 0
                        theme = THEME_IDS[(idx + 1) % len(THEME_IDS)]
                        last_touch_switch = now
                        print(f"[Touch] ADC={adc} -> 切换主题: {theme}", flush=True)
                    was_touched = touched

            snap = telemetry.sample(gpu_id=gpu_id)
            img = renderer.render(snap, theme=theme, size=screen.size, record_history=True)
            try:
                t_push = time.perf_counter()
                nbytes = screen.push_frame(img, brightness=brightness, rotate_180=rotate_180)
                push_ms = (time.perf_counter() - t_push) * 1000.0
                if args.once or args.verbose:
                    gpu_s = f"{snap.selected_gpu.util_percent:.0f}%" if snap.selected_gpu else "N/A"
                    print(
                        f"[Frame] CPU={snap.cpu_percent:.0f}% GPU={gpu_s} RAM={snap.ram_percent:.0f}% "
                        f"| {nbytes}B in {push_ms:.1f}ms",
                        flush=True,
                    )
            except Exception as exc:
                print(f"[Disconnect] 推送失败: {exc}，准备重连...", flush=True)
                screen.close()
                if args.once:
                    return 1
                time.sleep(1.5)
                continue

            if args.once:
                return 0

            elapsed = time.perf_counter() - t0
            sleep_s = max(0.02, interval - elapsed)
            time.sleep(sleep_s)
    finally:
        screen.close()
        telemetry.close()
        print("\n[Stopped] 已安全断开串口。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=f"{__app_name__} v{__version__} (Windows & Linux)")
    p.add_argument("--cli", "--headless", action="store_true", dest="cli", help="无界面命令行/守护进程模式")
    p.add_argument("--port", type=str, default=None, help="串口路径 (如 COM4, /dev/ttyACM0, 或 auto)")
    p.add_argument("--theme", type=str, choices=THEME_IDS, default=None, help="显示主题")
    p.add_argument("--fps", type=float, default=None, help="刷新频率 Hz (0.5 - 10.0)")
    p.add_argument("--brightness", type=int, default=None, help="软件亮度 (10 - 100)")
    p.add_argument("--rotate-180", action="store_true", help="画面旋转 180 度")
    p.add_argument("--gpu", type=str, default=None, help="指定 GPU ID (用 --list-gpus 查看) 或 auto")
    p.add_argument("--no-touch", action="store_true", help="禁用机身触摸键切换主题")
    p.add_argument("--once", action="store_true", help="CLI 模式仅渲染推送一帧后退出")
    p.add_argument("-v", "--verbose", action="store_true", help="CLI 模式打印每帧状态")
    p.add_argument("--list-ports", action="store_true", help="列出可用串口并退出")
    p.add_argument("--list-gpus", action="store_true", help="列出检测到的 GPU 并退出")
    p.add_argument("--list-themes", action="store_true", help="列出内置主题并退出")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.list_ports:
        return cmd_list_ports()
    if args.list_gpus:
        return cmd_list_gpus()
    if args.list_themes:
        for t in THEMES:
            print(f"  {t.id:<14} {t.name} - {t.description}")
        return 0

    if args.cli or args.once:
        return run_cli(args)

    # Check headless Linux environment before trying to open Tkinter
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("未检测到图形桌面环境 (DISPLAY/WAYLAND_DISPLAY)，自动切换为 --cli 无界面模式。")
        return run_cli(args)

    try:
        from msn01.gui import launch_gui
    except ImportError as exc:
        print(f"无法启动 GUI (可能缺少 python3-tk): {exc}")
        print("提示：Linux 可运行 `sudo apt install python3-tk`，或使用 `python3 main.py --cli` 无界面运行。")
        return 1

    launch_gui(initial_port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

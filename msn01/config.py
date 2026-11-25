"""Cross-platform JSON configuration storage for MSN01 Monitor."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class AppConfig:
    port: str = "auto"
    gpu_id: str = "auto"
    theme: str = "cyber_bars"
    refresh_hz: float = 2.0
    brightness: int = 100
    rotate_180: bool = False
    touch_switch_theme: bool = True
    auto_connect: bool = True
    auto_reconnect: bool = True


def get_config_dir() -> Path:
    """Return platform-appropriate config directory (Windows/Linux/macOS)."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "msn01-monitor"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "msn01-monitor"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base_path = Path(xdg) if xdg else (Path.home() / ".config")
    return base_path / "msn01-monitor"


def get_config_path() -> Path:
    return get_config_dir() / "config.json"


def load_config() -> AppConfig:
    path = get_config_path()
    if not path.exists():
        return AppConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        cfg = AppConfig()
        for key in asdict(cfg).keys():
            if key in raw:
                setattr(cfg, key, raw[key])
        # Clamp sensible ranges
        cfg.refresh_hz = max(0.5, min(15.0, float(cfg.refresh_hz)))
        cfg.brightness = max(10, min(100, int(cfg.brightness)))
        return cfg
    except Exception:
        return AppConfig()


def save_config(cfg: AppConfig) -> Path:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
    return path

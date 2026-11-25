"""MSN01 / MSU2 USB Mini Screen Serial Protocol Driver (Windows & Linux).

Verified protocol on WCH CH32x035 (VID:1A86 PID:FE0C):
- Heartbeat from device (every ~100ms while idle): b'\\x00MSN01'
- Host handshake: b'\\x00MSNCN' -> device echoes b'\\x00MSNCN'
- Flash/model query: [0x00, 0x30, 0x20, 0x02, 0x00, 0x00]
    -> 0x8513 (34067): 160x80 (0.96")
    -> 0x8516 (34070): 320x172 (1.47")
- Frame render:
    1. Set origin (x, y): [0x02, 0x00, xH, xL, yH, yL]
    2. Set size (w, h):   [0x02, 0x01, wH, wL, hH, hL]
    3. Init write + ACK:  [0x02, 0x03, 0x07, 0x00, 0x00, 0x00] -> ACK [0x02, 0x03, ...]
    4. For each 256-byte RGB565-BE chunk (64 x 4-byte double-pixel slots):
       - Fill buffer with dominant 4-byte word: [0x02, 0x04, b0, b1, b2, b3]
       - Patch non-matching slots:              [0x04, idx, b0, b1, b2, b3]
       - Commit chunk to LCD:                   [0x02, 0x03, 0x08, lenH, lenL, 0x00]
- Capacitive touch sensor (ADC ch 9):
    [0x08, 0x09, 0x00, 0x00, 0x00, 0x00] -> [0x08, 0x09, 0x00, 0x00, hi, lo]
    Idle ~4000, touched < 3600.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import serial
import serial.tools.list_ports
from PIL import Image

try:
    import numpy as np
except ImportError:
    np = None  # Pure-Python fallback available


MSN01_VID = 0x1A86
MSN01_PID = 0xFE0C
DEFAULT_BAUD = 921600

CMD_HANDSHAKE = b"\x00MSNCN"
CMD_FLASH_INFO = bytes([0x00, 0x30, 0x20, 0x02, 0x00, 0x00])
CMD_ADC_TOUCH = bytes([0x08, 0x09, 0x00, 0x00, 0x00, 0x00])

FLASH_RESOLUTIONS: dict[int, tuple[int, int]] = {
    0x8513: (160, 80),   # 34067 - 0.96" IPS
    0x8516: (320, 172),  # 34070 - 1.47" IPS
}


@dataclass(frozen=True)
class PortCandidate:
    device: str
    description: str
    vid: Optional[int]
    pid: Optional[int]
    serial_number: Optional[str]

    @property
    def is_msn01_vid(self) -> bool:
        return self.vid == MSN01_VID and self.pid == MSN01_PID

    @property
    def label(self) -> str:
        tag = " [MSN01]" if self.is_msn01_vid else ""
        if self.vid is not None and self.pid is not None:
            return f"{self.device}{tag} ({self.vid:04X}:{self.pid:04X})"
        desc = self.description or "Serial Port"
        return f"{self.device} ({desc})"


def list_serial_ports() -> list[PortCandidate]:
    """Return serial ports sorted with MSN01 (1A86:FE0C) and USB ports first."""
    candidates: list[PortCandidate] = []
    for p in serial.tools.list_ports.comports():
        # Filter out Linux legacy /dev/ttyS* unless USB VID/PID exists
        if sys.platform.startswith("linux") and p.device.startswith("/dev/ttyS") and p.vid is None:
            continue
        candidates.append(
            PortCandidate(
                device=p.device,
                description=p.description or "",
                vid=p.vid,
                pid=p.pid,
                serial_number=p.serial_number,
            )
        )

    def sort_key(c: PortCandidate) -> tuple[int, int, str]:
        msn_rank = 0 if c.is_msn01_vid else 1
        usb_rank = 0 if c.vid is not None else 1
        return (msn_rank, usb_rank, c.device)

    candidates.sort(key=sort_key)
    return candidates


def probe_msn01_port(port: str, baudrate: int = DEFAULT_BAUD) -> bool:
    """Quickly verify whether a serial port responds to the MSN01 protocol."""
    try:
        with serial.Serial(port, baudrate=baudrate, timeout=0.25, write_timeout=0.5) as ser:
            ser.reset_input_buffer()
            time.sleep(0.15)
            data = ser.read(ser.in_waiting)
            if b"\x00MSN01" in data:
                return True
            # Already connected previously? Try handshake directly.
            ser.write(CMD_HANDSHAKE)
            time.sleep(0.12)
            resp = ser.read(ser.in_waiting or 16)
            return b"\x00MSN" in resp
    except Exception:
        return False


def auto_detect_port() -> Optional[str]:
    """Find the best MSN01 screen port on Windows or Linux."""
    ports = list_serial_ports()
    # Fast path: prefer VID:PID 1A86:FE0C
    for p in ports:
        if p.is_msn01_vid and probe_msn01_port(p.device):
            return p.device
    # Fallback: probe other USB serial ports
    for p in ports:
        if not p.is_msn01_vid and p.vid is not None and probe_msn01_port(p.device):
            return p.device
    # Final fallback: if a 1A86:FE0C exists even if busy/silent, return it
    for p in ports:
        if p.is_msn01_vid:
            return p.device
    return None


def image_to_rgb565_be(
    img: Image.Image,
    target_size: tuple[int, int],
    brightness: int = 100,
    rotate_180: bool = False,
) -> bytes:
    """Convert a PIL Image to big-endian RGB565 bytes matching target_size."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    if img.size != target_size:
        img = img.resize(target_size, Image.Resampling.LANCZOS)
    if rotate_180:
        img = img.transpose(Image.Transpose.ROTATE_180)

    brightness = max(5, min(100, int(brightness)))

    if np is not None:
        arr = np.asarray(img, dtype=np.uint16)
        if brightness < 100:
            arr = (arr * brightness) // 100
        rgb565 = (
            ((arr[:, :, 0] & 0xF8) << 8)
            | ((arr[:, :, 1] & 0xFC) << 3)
            | (arr[:, :, 2] >> 3)
        )
        return rgb565.astype(">u2").tobytes()

    # Pure-Python fallback (no numpy dependency required)
    raw = img.tobytes()
    out = bytearray((len(raw) // 3) * 2)
    j = 0
    if brightness < 100:
        scale = brightness
        for i in range(0, len(raw), 3):
            r = (raw[i] * scale) // 100
            g = (raw[i + 1] * scale) // 100
            b = (raw[i + 2] * scale) // 100
            out[j] = (r & 0xF8) | (g >> 5)
            out[j + 1] = ((g & 0x1C) << 3) | (b >> 3)
            j += 2
    else:
        for i in range(0, len(raw), 3):
            r = raw[i]
            g = raw[i + 1]
            b = raw[i + 2]
            out[j] = (r & 0xF8) | (g >> 5)
            out[j + 1] = ((g & 0x1C) << 3) | (b >> 3)
            j += 2
    return bytes(out)


def encode_compressed_chunks(rgb565_be: bytes) -> bytearray:
    """Encode RGB565 big-endian frame into MSN01 fill+diff compressed LCD packets."""
    out = bytearray()
    total = len(rgb565_be)

    for off in range(0, total, 256):
        chunk = rgb565_be[off : off + 256]
        valid = len(chunk)
        if valid < 256:
            chunk = chunk + (b"\xff" * (256 - valid))

        # Split 256 bytes into 64 4-byte (2-pixel) words
        words = [chunk[i : i + 4] for i in range(0, 256, 4)]

        # Choose dominant 4-byte word to pre-fill MCU buffer
        counts: dict[bytes, int] = {}
        best_word = words[0]
        best_count = 0
        for wd in words:
            c = counts.get(wd, 0) + 1
            counts[wd] = c
            if c > best_count:
                best_count = c
                best_word = wd

        # Fill command: [0x02, 0x04, b0, b1, b2, b3]
        out.extend(b"\x02\x04")
        out.extend(best_word)

        # Patch differing slots: [0x04, idx, b0, b1, b2, b3]
        for idx, wd in enumerate(words):
            if wd != best_word:
                out.append(0x04)
                out.append(idx)
                out.extend(wd)

        # Commit chunk to LCD: [0x02, 0x03, 0x08, validH, validL, 0x00]
        out.append(0x02)
        out.append(0x03)
        out.append(0x08)
        out.append((valid >> 8) & 0xFF)
        out.append(valid & 0xFF)
        out.append(0x00)

    return out


class MSN01Screen:
    """Thread-safe controller for an MSN01 / CH32x035 USB mini screen."""

    def __init__(self, baudrate: int = DEFAULT_BAUD) -> None:
        self.baudrate = baudrate
        self.port: Optional[str] = None
        self.width: int = 160
        self.height: int = 80
        self.flash_id: Optional[int] = None
        self._ser: Optional[serial.Serial] = None
        self._lock = threading.RLock()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._ser is not None and self._ser.is_open

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    def connect(self, port: Optional[str] = None) -> tuple[int, int]:
        """Connect to specified port (or auto-detect), handshake, and detect resolution."""
        with self._lock:
            self.close()
            target = port if (port and port.lower() != "auto") else auto_detect_port()
            if not target:
                raise ConnectionError("未检测到 MSN01 小屏幕，请检查 USB 连接或串口权限。")

            ser = serial.Serial(
                port=target,
                baudrate=self.baudrate,
                timeout=0.5,
                write_timeout=1.5,
            )
            self._ser = ser
            self.port = target

            try:
                self._handshake_locked()
                self._query_model_locked()
            except Exception:
                self.close()
                raise

            return (self.width, self.height)

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    def _handshake_locked(self) -> None:
        assert self._ser is not None
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

        for _ in range(3):
            self._ser.write(CMD_HANDSHAKE)
            self._ser.flush()
            deadline = time.perf_counter() + 0.25
            buf = bytearray()
            while time.perf_counter() < deadline:
                n = self._ser.in_waiting
                if n > 0:
                    buf.extend(self._ser.read(n))
                    if b"\x00MSN" in buf:
                        return
                time.sleep(0.005)
        raise ConnectionError(f"串口 {self.port} 未响应 MSN01 握手协议。")

    def _query_model_locked(self) -> None:
        assert self._ser is not None
        self._ser.reset_input_buffer()
        self._ser.write(CMD_FLASH_INFO)
        self._ser.flush()

        deadline = time.perf_counter() + 0.35
        buf = bytearray()
        while time.perf_counter() < deadline:
            n = self._ser.in_waiting
            if n > 0:
                buf.extend(self._ser.read(n))
                if len(buf) >= 6:
                    break
            time.sleep(0.005)

        if len(buf) >= 6:
            # Find response header 0x00 0x30 if preceded by residual heartbeat
            idx = buf.find(b"\x00\x30")
            if idx != -1 and len(buf) >= idx + 6:
                fid = (buf[idx + 4] << 8) | buf[idx + 5]
            else:
                fid = (buf[4] << 8) | buf[5]
            self.flash_id = fid
            self.width, self.height = FLASH_RESOLUTIONS.get(fid, (160, 80))
        else:
            self.flash_id = None
            self.width, self.height = (160, 80)

    def _init_lcd_write_locked(self, x: int, y: int, w: int, h: int) -> bool:
        assert self._ser is not None
        self._ser.reset_input_buffer()
        cmd_xy = bytes([0x02, 0x00, (x >> 8) & 0xFF, x & 0xFF, (y >> 8) & 0xFF, y & 0xFF])
        cmd_wh = bytes([0x02, 0x01, (w >> 8) & 0xFF, w & 0xFF, (h >> 8) & 0xFF, h & 0xFF])
        cmd_init = bytes([0x02, 0x03, 0x07, 0x00, 0x00, 0x00])
        self._ser.write(cmd_xy + cmd_wh + cmd_init)
        self._ser.flush()

        deadline = time.perf_counter() + 0.6
        buf = bytearray()
        while time.perf_counter() < deadline:
            n = self._ser.in_waiting
            if n > 0:
                buf.extend(self._ser.read(n))
                if b"\x02\x03" in buf:
                    return True
                if len(buf) >= 12:
                    return False
            time.sleep(0.001)
        return False

    def push_frame(
        self,
        img: Image.Image,
        brightness: int = 100,
        rotate_180: bool = False,
    ) -> int:
        """Render a PIL image onto the MSN01 screen. Returns bytes written."""
        rgb565 = image_to_rgb565_be(
            img,
            target_size=(self.width, self.height),
            brightness=brightness,
            rotate_180=rotate_180,
        )
        payload = encode_compressed_chunks(rgb565)

        with self._lock:
            if not self.is_connected or self._ser is None:
                raise ConnectionError("MSN01 尚未连接。")

            ok = self._init_lcd_write_locked(0, 0, self.width, self.height)
            if not ok:
                # Wake up once and retry
                self._handshake_locked()
                ok = self._init_lcd_write_locked(0, 0, self.width, self.height)
                if not ok:
                    raise IOError("LCD 写入初始化无响应 (ACK 超时)。")

            self._ser.write(payload)
            self._ser.flush()
            return len(payload)

    def read_touch_adc(self) -> Optional[int]:
        """Read capacitive touch sensor ADC channel 9 (idle ~4000, touched < 3600)."""
        with self._lock:
            if not self.is_connected or self._ser is None:
                return None
            try:
                self._ser.reset_input_buffer()
                self._ser.write(CMD_ADC_TOUCH)
                self._ser.flush()
                deadline = time.perf_counter() + 0.05
                buf = bytearray()
                while time.perf_counter() < deadline:
                    n = self._ser.in_waiting
                    if n > 0:
                        buf.extend(self._ser.read(n))
                        idx = buf.find(b"\x08\x09")
                        if idx != -1 and len(buf) >= idx + 6:
                            return (buf[idx + 4] << 8) | buf[idx + 5]
                    time.sleep(0.002)
            except Exception:
                return None
        return None

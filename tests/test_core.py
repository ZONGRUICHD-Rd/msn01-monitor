"""Automated unit tests for MSN01 protocol encoder, telemetry, renderer, and config."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from msn01 import config, protocol
from msn01.renderer import THEME_IDS, DashboardRenderer
from msn01.telemetry import GpuSample, SystemSnapshot, TelemetryCollector


class TestProtocolEncoding(unittest.TestCase):
    def test_rgb565_numpy_matches_pure_python(self) -> None:
        img = Image.new("RGB", (16, 8))
        px = img.load()
        for y in range(8):
            for x in range(16):
                px[x, y] = ((x * 17) % 256, (y * 31) % 256, ((x + y) * 11) % 256)

        for brightness in (100, 60):
            for rotate in (False, True):
                np_bytes = protocol.image_to_rgb565_be(img, (16, 8), brightness=brightness, rotate_180=rotate)
                with mock.patch.object(protocol, "np", None):
                    py_bytes = protocol.image_to_rgb565_be(img, (16, 8), brightness=brightness, rotate_180=rotate)
                self.assertEqual(len(np_bytes), 16 * 8 * 2)
                self.assertEqual(np_bytes, py_bytes)

    def test_compressed_chunks_structure_and_ratio(self) -> None:
        # Solid color frame should compress drastically: 100 chunks * (6 fill + 6 commit) = 1200 bytes
        img = Image.new("RGB", (160, 80), (7, 11, 20))
        rgb565 = protocol.image_to_rgb565_be(img, (160, 80))
        self.assertEqual(len(rgb565), 160 * 80 * 2)

        payload = protocol.encode_compressed_chunks(rgb565)
        self.assertEqual(len(payload), 100 * 12)
        self.assertEqual(payload[:2], bytearray(b"\x02\x04"))
        self.assertEqual(payload[6:12], bytearray(b"\x02\x03\x08\x01\x00\x00"))


class TestRenderer(unittest.TestCase):
    def _sample_snapshot(self) -> SystemSnapshot:
        gpu = GpuSample(
            id="test:0",
            name="AMD Radeon(TM) Graphics",
            vendor="AMD",
            util_percent=42.5,
            vram_used_mb=512.0,
            vram_total_mb=2048.0,
            temp_c=56.0,
            source="test",
        )
        return SystemSnapshot(
            cpu_percent=37.2,
            cpu_freq_mhz=4300.0,
            cpu_temp_c=61.0,
            cpu_cores_physical=8,
            cpu_cores_logical=16,
            ram_percent=65.4,
            ram_used_gb=20.4,
            ram_total_gb=31.3,
            gpus=[gpu],
            selected_gpu=gpu,
        )

    def test_all_themes_render_both_resolutions(self) -> None:
        renderer = DashboardRenderer()
        snap = self._sample_snapshot()
        for tid in THEME_IDS:
            img_small = renderer.render(snap, theme=tid, size=(160, 80))
            self.assertEqual(img_small.size, (160, 80))
            self.assertEqual(img_small.mode, "RGB")

            img_large = renderer.render(snap, theme=tid, size=(320, 172), record_history=False)
            self.assertEqual(img_large.size, (320, 172))
            self.assertEqual(img_large.mode, "RGB")

    def test_render_without_gpu(self) -> None:
        renderer = DashboardRenderer()
        snap = self._sample_snapshot()
        snap.gpus = []
        snap.selected_gpu = None
        for tid in THEME_IDS:
            img = renderer.render(snap, theme=tid, size=(160, 80))
            self.assertEqual(img.size, (160, 80))


class TestTelemetryAndConfig(unittest.TestCase):
    def test_live_telemetry_sample(self) -> None:
        col = TelemetryCollector()
        try:
            snap = col.sample()
            self.assertGreaterEqual(snap.cpu_percent, 0.0)
            self.assertLessEqual(snap.cpu_percent, 100.0)
            self.assertGreater(snap.ram_total_gb, 0.0)
        finally:
            col.close()

    def test_config_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            with mock.patch.object(config, "get_config_path", return_value=cfg_path):
                c1 = config.AppConfig(port="COM4", theme="spark_graph", refresh_hz=4.0, brightness=80, rotate_180=True)
                config.save_config(c1)
                c2 = config.load_config()
                self.assertEqual(c1, c2)


if __name__ == "__main__":
    unittest.main()

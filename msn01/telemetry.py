"""Cross-platform CPU, GPU, and RAM telemetry collector (Windows & Linux).

Zero-admin GPU monitoring support:
- Windows:
    1. NVML (nvml.dll via ctypes) for NVIDIA GPUs (util, VRAM, temp) when permitted
    2. DXGI + PDH English Counters (dxgi.dll + pdh.dll via ctypes) for ALL GPUs
       (AMD, Intel, NVIDIA) with zero admin rights and zero subprocess overhead
    3. nvidia-smi fallback if available and permitted
- Linux:
    1. NVML (libnvidia-ml.so.1 via ctypes) for NVIDIA GPUs
    2. nvidia-smi fallback
    3. /sys/class/drm sysfs for AMD (gpu_busy_percent, VRAM, hwmon temp) & Intel
"""

from __future__ import annotations

import ctypes
import glob
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import psutil


@dataclass
class GpuSample:
    id: str
    name: str
    vendor: str
    util_percent: float
    vram_used_mb: Optional[float] = None
    vram_total_mb: Optional[float] = None
    temp_c: Optional[float] = None
    source: str = ""

    @property
    def short_name(self) -> str:
        n = self.name
        for prefix in ("NVIDIA GeForce ", "NVIDIA ", "AMD Radeon(TM) ", "AMD Radeon ", "Intel(R) "):
            if n.startswith(prefix):
                n = n[len(prefix) :]
        return n[:18]

    @property
    def vram_text(self) -> str:
        if self.vram_used_mb is None:
            return ""
        if self.vram_total_mb and self.vram_total_mb >= 1024:
            return f"{self.vram_used_mb / 1024:.1f}/{self.vram_total_mb / 1024:.0f}G"
        if self.vram_used_mb >= 1024:
            return f"{self.vram_used_mb / 1024:.1f}G"
        return f"{self.vram_used_mb:.0f}M"


@dataclass
class SystemSnapshot:
    cpu_percent: float
    cpu_freq_mhz: Optional[float]
    cpu_temp_c: Optional[float]
    cpu_cores_physical: int
    cpu_cores_logical: int
    ram_percent: float
    ram_used_gb: float
    ram_total_gb: float
    gpus: list[GpuSample] = field(default_factory=list)
    selected_gpu: Optional[GpuSample] = None
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Backend 1: NVML (Windows nvml.dll + Linux libnvidia-ml.so.1)
# ---------------------------------------------------------------------------
class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMemory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class _NvmlBackend:
    def __init__(self) -> None:
        self._lib = None
        self._count = 0
        self._init()

    def _init(self) -> None:
        candidates = []
        if sys.platform.startswith("win"):
            candidates = [
                r"C:\Windows\System32\nvml.dll",
                r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll",
                "nvml.dll",
            ]
        else:
            candidates = ["libnvidia-ml.so.1", "libnvidia-ml.so"]

        for path in candidates:
            if os.path.isabs(path) and not os.path.exists(path):
                continue
            try:
                lib = ctypes.CDLL(path)
                rc = lib.nvmlInit_v2()
                if rc == 0:
                    cnt = ctypes.c_uint(0)
                    if lib.nvmlDeviceGetCount_v2(ctypes.byref(cnt)) == 0 and cnt.value > 0:
                        self._lib = lib
                        self._count = cnt.value
                        return
                    lib.nvmlShutdown()
            except Exception:
                continue

    @property
    def available(self) -> bool:
        return self._lib is not None and self._count > 0

    def sample(self) -> list[GpuSample]:
        if not self.available or self._lib is None:
            return []
        out: list[GpuSample] = []
        for idx in range(self._count):
            try:
                handle = ctypes.c_void_p()
                if self._lib.nvmlDeviceGetHandleByIndex_v2(idx, ctypes.byref(handle)) != 0:
                    continue
                name_buf = ctypes.create_string_buffer(128)
                self._lib.nvmlDeviceGetName(handle, name_buf, 128)
                name = name_buf.value.decode("utf-8", errors="replace") or f"NVIDIA GPU {idx}"

                util = _NvmlUtil()
                util_pct = 0.0
                if self._lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util)) == 0:
                    util_pct = float(util.gpu)

                temp = ctypes.c_uint(0)
                temp_c: Optional[float] = None
                if self._lib.nvmlDeviceGetTemperature(handle, 0, ctypes.byref(temp)) == 0:
                    temp_c = float(temp.value)

                mem = _NvmlMemory()
                vram_used: Optional[float] = None
                vram_total: Optional[float] = None
                if self._lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) == 0:
                    vram_used = mem.used / (1024 * 1024)
                    vram_total = mem.total / (1024 * 1024)

                out.append(
                    GpuSample(
                        id=f"nvml:{idx}",
                        name=name,
                        vendor="NVIDIA",
                        util_percent=max(0.0, min(100.0, util_pct)),
                        vram_used_mb=vram_used,
                        vram_total_mb=vram_total,
                        temp_c=temp_c,
                        source="NVML",
                    )
                )
            except Exception:
                continue
        return out

    def close(self) -> None:
        if self._lib is not None:
            try:
                self._lib.nvmlShutdown()
            except Exception:
                pass
            self._lib = None


# ---------------------------------------------------------------------------
# Backend 2: nvidia-smi CLI fallback (only if NVML ctypes isn't available)
# ---------------------------------------------------------------------------
class _NvidiaSmiBackend:
    def __init__(self) -> None:
        self._exe = shutil.which("nvidia-smi")
        self._enabled = False
        if self._exe:
            # Test once at startup so we never spawn failing commands repeatedly
            if self._query():
                self._enabled = True

    @property
    def available(self) -> bool:
        return self._enabled

    def _query(self) -> list[GpuSample]:
        if not self._exe:
            return []
        try:
            kwargs: dict = {
                "capture_output": True,
                "text": True,
                "timeout": 1.5,
            }
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            res = subprocess.run(
                [
                    self._exe,
                    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                **kwargs,
            )
            if res.returncode != 0:
                return []
            out: list[GpuSample] = []
            for line in res.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 6:
                    continue
                idx, name, util_s, mem_u, mem_t, temp_s = parts[:6]
                out.append(
                    GpuSample(
                        id=f"nvsmi:{idx}",
                        name=name,
                        vendor="NVIDIA",
                        util_percent=float(util_s) if util_s.replace(".", "", 1).isdigit() else 0.0,
                        vram_used_mb=float(mem_u) if mem_u.replace(".", "", 1).isdigit() else None,
                        vram_total_mb=float(mem_t) if mem_t.replace(".", "", 1).isdigit() else None,
                        temp_c=float(temp_s) if temp_s.replace(".", "", 1).isdigit() else None,
                        source="nvidia-smi",
                    )
                )
            return out
        except Exception:
            return []

    def sample(self) -> list[GpuSample]:
        return self._query() if self._enabled else []


# ---------------------------------------------------------------------------
# Backend 3: Windows DXGI + PDH English Counters (AMD / Intel / NVIDIA, no admin)
# ---------------------------------------------------------------------------
class _WindowsDxgiPdhBackend:
    _LUID_RE = re.compile(r"(luid_0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)")
    _ENG_RE = re.compile(r"engtype_([A-Za-z0-9_]+)")

    def __init__(self) -> None:
        self._available = False
        self._adapters: dict[str, tuple[str, str, float]] = {}  # luid -> (name, vendor, total_mb)
        self._pdh = None
        self._hq = None
        self._hc_eng = None
        self._hc_mem = None
        if sys.platform.startswith("win"):
            self._init_windows()

    @property
    def available(self) -> bool:
        return self._available

    def _init_windows(self) -> None:
        import ctypes.wintypes as wt

        self._wt = wt
        self._enumerate_dxgi_adapters()
        self._open_pdh()

    def _enumerate_dxgi_adapters(self) -> None:
        wt = self._wt

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]

        class DXGI_ADAPTER_DESC1(ctypes.Structure):
            _fields_ = [
                ("Description", wt.WCHAR * 128),
                ("VendorId", wt.UINT),
                ("DeviceId", wt.UINT),
                ("SubSysId", wt.UINT),
                ("Revision", wt.UINT),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", LUID),
                ("Flags", wt.UINT),
            ]

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        iid_factory1 = GUID(
            0x770AAE78,
            0xF26F,
            0x4DBA,
            (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87),
        )

        def get_vtbl(ptr: ctypes.c_void_p):
            return ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

        vendor_map = {0x10DE: "NVIDIA", 0x1002: "AMD", 0x8086: "Intel"}

        try:
            dxgi = ctypes.windll.dxgi
            factory = ctypes.c_void_p()
            if dxgi.CreateDXGIFactory1(ctypes.byref(iid_factory1), ctypes.byref(factory)) != 0:
                return
            f_vtbl = get_vtbl(factory)
            enum_adapters = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p)
            )(f_vtbl[12])
            release_type = ctypes.WINFUNCTYPE(wt.ULONG, ctypes.c_void_p)

            i = 0
            while True:
                adapter = ctypes.c_void_p()
                if enum_adapters(factory, i, ctypes.byref(adapter)) != 0:
                    break
                a_vtbl = get_vtbl(adapter)
                get_desc1 = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(DXGI_ADAPTER_DESC1)
                )(a_vtbl[10])
                desc = DXGI_ADAPTER_DESC1()
                if get_desc1(adapter, ctypes.byref(desc)) == 0:
                    # Skip Microsoft Basic Render Driver (DXGI_ADAPTER_FLAG_SOFTWARE = 2)
                    if (desc.Flags & 2) == 0:
                        luid = f"luid_0x{desc.AdapterLuid.HighPart & 0xFFFFFFFF:08x}_0x{desc.AdapterLuid.LowPart & 0xFFFFFFFF:08x}"
                        if luid not in self._adapters:
                            vendor = vendor_map.get(desc.VendorId, "GPU")
                            vram_mb = float(desc.DedicatedVideoMemory) / (1024 * 1024)
                            self._adapters[luid] = (desc.Description.strip(), vendor, vram_mb)
                release_type(a_vtbl[2])(adapter)
                i += 1
            release_type(f_vtbl[2])(factory)
        except Exception:
            pass

    def _open_pdh(self) -> None:
        try:
            pdh = ctypes.windll.pdh
            hq = ctypes.c_void_p()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(hq)) != 0:
                return
            hc_eng = ctypes.c_void_p()
            hc_mem = ctypes.c_void_p()
            rc1 = pdh.PdhAddEnglishCounterW(
                hq, r"\GPU Engine(*)\Utilization Percentage", 0, ctypes.byref(hc_eng)
            )
            rc2 = pdh.PdhAddEnglishCounterW(
                hq, r"\GPU Adapter Memory(*)\Dedicated Usage", 0, ctypes.byref(hc_mem)
            )
            if rc1 != 0 and rc2 != 0:
                pdh.PdhCloseQuery(hq)
                return
            pdh.PdhCollectQueryData(hq)
            self._pdh = pdh
            self._hq = hq
            self._hc_eng = hc_eng if rc1 == 0 else None
            self._hc_mem = hc_mem if rc2 == 0 else None
            self._available = True
        except Exception:
            self._available = False

    def _read_pdh_array(self, hc: ctypes.c_void_p) -> list[tuple[str, float]]:
        wt = self._wt

        class PDH_FMT_COUNTERVALUE(ctypes.Structure):
            _fields_ = [("CStatus", wt.DWORD), ("doubleValue", ctypes.c_double)]

        class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
            _fields_ = [("szName", wt.LPWSTR), ("FmtValue", PDH_FMT_COUNTERVALUE)]

        buf_size = wt.DWORD(0)
        item_cnt = wt.DWORD(0)
        # PDH_FMT_DOUBLE = 0x00000200
        self._pdh.PdhGetFormattedCounterArrayW(
            hc, 0x00000200, ctypes.byref(buf_size), ctypes.byref(item_cnt), None
        )
        if buf_size.value == 0:
            return []
        buf = ctypes.create_string_buffer(buf_size.value)
        rc = self._pdh.PdhGetFormattedCounterArrayW(
            hc, 0x00000200, ctypes.byref(buf_size), ctypes.byref(item_cnt), buf
        )
        if rc != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
        result: list[tuple[str, float]] = []
        for i in range(item_cnt.value):
            if items[i].FmtValue.CStatus in (0, 1):  # VALID / NEW_DATA
                result.append((items[i].szName or "", float(items[i].FmtValue.doubleValue)))
        return result

    def sample(self) -> list[GpuSample]:
        if not self._available or self._pdh is None or self._hq is None:
            return []
        try:
            if self._pdh.PdhCollectQueryData(self._hq) != 0:
                return []

            # Task Manager formula: for each LUID, sum per engine type, then take max across engine types
            eng_sums: dict[str, dict[str, float]] = {}
            if self._hc_eng is not None:
                for inst, val in self._read_pdh_array(self._hc_eng):
                    ml = self._LUID_RE.search(inst)
                    if not ml:
                        continue
                    luid = ml.group(1).lower()
                    me = self._ENG_RE.search(inst)
                    etype = me.group(1) if me else "other"
                    bucket = eng_sums.setdefault(luid, {})
                    bucket[etype] = bucket.get(etype, 0.0) + val

            ded_mem: dict[str, float] = {}
            if self._hc_mem is not None:
                for inst, val in self._read_pdh_array(self._hc_mem):
                    ml = self._LUID_RE.search(inst)
                    if not ml:
                        continue
                    luid = ml.group(1).lower()
                    ded_mem[luid] = max(ded_mem.get(luid, 0.0), val / (1024 * 1024))

            # Emit samples for all known DXGI hardware adapters (or LUIDs seen in PDH)
            luids = list(self._adapters.keys())
            for luid in eng_sums.keys():
                if luid not in self._adapters:
                    # Only include non-software LUIDs if DXGI missed them
                    continue

            out: list[GpuSample] = []
            for luid in luids:
                # Skip duplicate LUIDs with zero activity when same adapter appears multiple times
                if luid not in eng_sums and luid not in ded_mem:
                    continue
                name, vendor, total_mb = self._adapters[luid]
                per_eng = eng_sums.get(luid, {})
                util_pct = max(per_eng.values()) if per_eng else 0.0
                util_pct = max(0.0, min(100.0, util_pct))
                out.append(
                    GpuSample(
                        id=f"pdh:{luid}",
                        name=name,
                        vendor=vendor,
                        util_percent=util_pct,
                        vram_used_mb=ded_mem.get(luid),
                        vram_total_mb=total_mb if total_mb > 0 else None,
                        temp_c=None,
                        source="DXGI/PDH",
                    )
                )
            return out
        except Exception:
            return []

    def close(self) -> None:
        if self._pdh is not None and self._hq is not None:
            try:
                self._pdh.PdhCloseQuery(self._hq)
            except Exception:
                pass
            self._hq = None


# ---------------------------------------------------------------------------
# Backend 4: Linux DRM sysfs (AMD + Intel, zero external tools)
# ---------------------------------------------------------------------------
class _LinuxDrmSysfsBackend:
    def __init__(self) -> None:
        self._cards: list[tuple[str, Path, str]] = []
        if sys.platform.startswith("linux"):
            self._scan()

    def _scan(self) -> None:
        vendor_names = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA"}
        for card_dir in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
            base = os.path.basename(card_dir)
            if "-" in base:  # Skip connector entries like card0-DP-1
                continue
            dev = Path(card_dir) / "device"
            vendor_file = dev / "vendor"
            if not vendor_file.exists():
                continue
            try:
                vid = vendor_file.read_text().strip().lower()
            except Exception:
                continue
            vendor = vendor_names.get(vid, "GPU")
            self._cards.append((base, dev, vendor))

    @property
    def available(self) -> bool:
        return bool(self._cards)

    @staticmethod
    def _read_float(path: Path) -> Optional[float]:
        try:
            return float(path.read_text().strip())
        except Exception:
            return None

    def _read_hwmon_temp(self, dev: Path) -> Optional[float]:
        for tfile in sorted((dev / "hwmon").glob("hwmon*/temp1_input")):
            val = self._read_float(tfile)
            if val is not None:
                return val / 1000.0 if val > 200 else val
        return None

    def sample(self, skip_nvidia: bool = False) -> list[GpuSample]:
        out: list[GpuSample] = []
        for card_name, dev, vendor in self._cards:
            if skip_nvidia and vendor == "NVIDIA":
                continue

            util: Optional[float] = None
            vram_used: Optional[float] = None
            vram_total: Optional[float] = None
            temp = self._read_hwmon_temp(dev)

            # AMD amdgpu sysfs
            busy = self._read_float(dev / "gpu_busy_percent")
            if busy is not None:
                util = busy
                u = self._read_float(dev / "mem_info_vram_used")
                t = self._read_float(dev / "mem_info_vram_total")
                if u is not None:
                    vram_used = u / (1024 * 1024)
                if t is not None:
                    vram_total = t / (1024 * 1024)
            else:
                # Intel i915/xe frequency-based busy estimate if no direct busy file
                gt_dir = dev.parent
                cur_f = self._read_float(gt_dir / "gt_act_freq_mhz") or self._read_float(
                    gt_dir / "gt_cur_freq_mhz"
                )
                max_f = self._read_float(gt_dir / "gt_max_freq_mhz") or self._read_float(
                    gt_dir / "gt_RP0_freq_mhz"
                )
                min_f = self._read_float(gt_dir / "gt_min_freq_mhz") or 0.0
                if cur_f is not None and max_f is not None and max_f > min_f:
                    util = max(0.0, min(100.0, ((cur_f - min_f) / (max_f - min_f)) * 100.0))

            if util is None and vram_used is None and temp is None:
                continue

            out.append(
                GpuSample(
                    id=f"drm:{card_name}",
                    name=f"{vendor} GPU ({card_name})",
                    vendor=vendor,
                    util_percent=max(0.0, min(100.0, util or 0.0)),
                    vram_used_mb=vram_used,
                    vram_total_mb=vram_total,
                    temp_c=temp,
                    source="sysfs",
                )
            )
        return out


# ---------------------------------------------------------------------------
# Unified Telemetry Collector
# ---------------------------------------------------------------------------
class TelemetryCollector:
    """Cross-platform CPU, GPU, and RAM telemetry collector."""

    def __init__(self) -> None:
        # Prime psutil CPU percentage counter
        psutil.cpu_percent(interval=None)
        self._cores_physical = psutil.cpu_count(logical=False) or psutil.cpu_count() or 1
        self._cores_logical = psutil.cpu_count(logical=True) or self._cores_physical

        self._nvml = _NvmlBackend()
        self._nvsmi = None if self._nvml.available else _NvidiaSmiBackend()
        self._win_pdh = _WindowsDxgiPdhBackend() if sys.platform.startswith("win") else None
        self._linux_drm = _LinuxDrmSysfsBackend() if sys.platform.startswith("linux") else None

    def _read_cpu_temp(self) -> Optional[float]:
        if not hasattr(psutil, "sensors_temperatures"):
            return None
        try:
            temps = psutil.sensors_temperatures()
            if not temps:
                return None
            preferred = ("k10temp", "zenpower", "coretemp", "cpu_thermal", "soc_thermal", "acpitz")
            for key in preferred:
                entries = temps.get(key)
                if entries:
                    vals = [e.current for e in entries if e.current and e.current > 0]
                    if vals:
                        return float(max(vals))
            for entries in temps.values():
                vals = [e.current for e in entries if e.current and e.current > 0]
                if vals:
                    return float(max(vals))
        except Exception:
            return None
        return None

    def _collect_gpus(self) -> list[GpuSample]:
        gpus: list[GpuSample] = []
        has_nvidia = False

        if self._nvml.available:
            nv = self._nvml.sample()
            gpus.extend(nv)
            has_nvidia = bool(nv)
        elif self._nvsmi is not None and self._nvsmi.available:
            nv = self._nvsmi.sample()
            gpus.extend(nv)
            has_nvidia = bool(nv)

        if self._win_pdh is not None and self._win_pdh.available:
            for g in self._win_pdh.sample():
                # Avoid duplicating NVIDIA cards already reported by NVML with richer metrics
                if has_nvidia and g.vendor == "NVIDIA":
                    continue
                gpus.append(g)

        if self._linux_drm is not None and self._linux_drm.available:
            gpus.extend(self._linux_drm.sample(skip_nvidia=has_nvidia))

        return gpus

    @staticmethod
    def select_gpu(gpus: list[GpuSample], gpu_id: str = "auto") -> Optional[GpuSample]:
        if not gpus:
            return None
        if gpu_id and gpu_id != "auto":
            for g in gpus:
                if g.id == gpu_id:
                    return g
        # Auto mode: prefer discrete/NVML GPU, or largest VRAM, or first active GPU
        return sorted(
            gpus,
            key=lambda g: (
                0 if g.source in ("NVML", "nvidia-smi") else 1,
                -(g.vram_total_mb or 0.0),
                -g.util_percent,
            ),
        )[0]

    def sample(self, gpu_id: str = "auto") -> SystemSnapshot:
        cpu_pct = float(psutil.cpu_percent(interval=None))
        freq_mhz: Optional[float] = None
        try:
            f = psutil.cpu_freq()
            if f and f.current:
                freq_mhz = float(f.current)
        except Exception:
            pass

        vm = psutil.virtual_memory()
        gpus = self._collect_gpus()
        selected = self.select_gpu(gpus, gpu_id)

        return SystemSnapshot(
            cpu_percent=max(0.0, min(100.0, cpu_pct)),
            cpu_freq_mhz=freq_mhz,
            cpu_temp_c=self._read_cpu_temp(),
            cpu_cores_physical=self._cores_physical,
            cpu_cores_logical=self._cores_logical,
            ram_percent=max(0.0, min(100.0, float(vm.percent))),
            ram_used_gb=vm.used / (1024**3),
            ram_total_gb=vm.total / (1024**3),
            gpus=gpus,
            selected_gpu=selected,
            timestamp=time.time(),
        )

    def close(self) -> None:
        self._nvml.close()
        if self._win_pdh is not None:
            self._win_pdh.close()

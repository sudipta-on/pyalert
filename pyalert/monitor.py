"""
pyalert.monitor
================

System telemetry collection for pyalert.

Design goals
------------
1.  Zero *hard* GPU dependency: NVIDIA GPU stats are read by loading the
    NVML shared library directly via ``ctypes`` (``nvml.dll`` on Windows,
    ``libnvidia-ml.so`` / ``libnvidia-ml.so.1`` on Linux). No ``pynvml`` or
    ``torch`` install is required.
2.  Total defensive fallback: if NVML can't be loaded (no NVIDIA driver, no
    GPU, non-Linux/Windows exotic platform, permissions issue, etc.) the
    monitor silently reports "no GPU data" and the rest of pyalert keeps
    working normally. A warning is emitted to stderr exactly once.
3.  ``psutil`` is the only hard runtime dependency and is used for
    per-core CPU utilization, aggregate RSS memory (including child
    processes), disk usage, and network counters.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a hard dependency
    sys.stderr.write(
        "[pyalert] WARNING: 'psutil' is not installed. CPU/RAM metrics will "
        "be unavailable. Install with `pip install psutil`.\n"
    )
    psutil = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #

@dataclass
class GPUInfo:
    """Snapshot of a single NVIDIA GPU's state."""

    index: int
    name: str
    utilization_pct: Optional[float] = None
    memory_used_mb: Optional[float] = None
    memory_total_mb: Optional[float] = None
    temperature_c: Optional[float] = None
    power_watts: Optional[float] = None

    @property
    def memory_pct(self) -> Optional[float]:
        if self.memory_used_mb is None or not self.memory_total_mb:
            return None
        return round((self.memory_used_mb / self.memory_total_mb) * 100, 1)


@dataclass
class SystemSnapshot:
    """A single point-in-time snapshot of host + process resource usage."""

    timestamp: float = field(default_factory=time.time)
    hostname: str = ""
    platform: str = ""
    python_version: str = ""
    pid: int = 0

    cpu_percent_total: Optional[float] = None
    cpu_percent_per_core: List[float] = field(default_factory=list)
    load_average: Optional[List[float]] = None

    ram_used_mb: Optional[float] = None
    ram_total_mb: Optional[float] = None
    ram_percent: Optional[float] = None

    process_rss_mb: Optional[float] = None  # aggregate RSS: main + children

    disk_used_gb: Optional[float] = None
    disk_total_gb: Optional[float] = None
    disk_percent: Optional[float] = None

    net_sent_mb: Optional[float] = None
    net_recv_mb: Optional[float] = None

    uptime_seconds: Optional[float] = None

    gpus: List[GPUInfo] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        """Flatten to a plain dict, safe for JSON serialization / templating."""
        return {
            "timestamp": self.timestamp,
            "hostname": self.hostname,
            "platform": self.platform,
            "python_version": self.python_version,
            "pid": self.pid,
            "cpu_percent_total": self.cpu_percent_total,
            "cpu_percent_per_core": self.cpu_percent_per_core,
            "load_average": self.load_average,
            "ram_used_mb": self.ram_used_mb,
            "ram_total_mb": self.ram_total_mb,
            "ram_percent": self.ram_percent,
            "process_rss_mb": self.process_rss_mb,
            "disk_used_gb": self.disk_used_gb,
            "disk_total_gb": self.disk_total_gb,
            "disk_percent": self.disk_percent,
            "net_sent_mb": self.net_sent_mb,
            "net_recv_mb": self.net_recv_mb,
            "uptime_seconds": self.uptime_seconds,
            "gpus": [gpu.__dict__ for gpu in self.gpus],
        }


# --------------------------------------------------------------------------- #
# NVML ctypes bindings
# --------------------------------------------------------------------------- #

class _NVMLBinding:
    """
    Minimal ctypes binding to the subset of the NVML C API pyalert needs.

    This intentionally avoids any third-party NVML wrapper. It loads the
    vendor-shipped shared library that ships with every NVIDIA driver
    install, so it works anywhere `nvidia-smi` works, with no extra
    Python packages.
    """

    # NVML return code for success
    NVML_SUCCESS = 0

    def __init__(self) -> None:
        self._lib: Optional[ctypes.CDLL] = None
        self._initialized = False
        self._load_library()
        if self._lib is not None:
            self._init_nvml()

    # -- library loading ----------------------------------------------- #

    def _candidate_names(self) -> List[str]:
        system = platform.system()
        if system == "Windows":
            return ["nvml.dll"]
        if system == "Darwin":
            # NVIDIA dropped macOS driver support years ago; no NVML there.
            return []
        # Linux and other POSIX systems
        return [
            "libnvidia-ml.so.1",
            "libnvidia-ml.so",
        ]

    def _load_library(self) -> None:
        for name in self._candidate_names():
            try:
                if platform.system() == "Windows":
                    # NVML on Windows typically lives under the driver
                    # store; also try the standard System32 path.
                    search_paths = [
                        name,
                        os.path.join(
                            os.environ.get("WINDIR", r"C:\Windows"),
                            "System32",
                            name,
                        ),
                        os.path.join(
                            os.environ.get(
                                "ProgramFiles",
                                r"C:\Program Files",
                            ),
                            "NVIDIA Corporation",
                            "NVSMI",
                            name,
                        ),
                    ]
                    for path in search_paths:
                        try:
                            self._lib = ctypes.CDLL(path)
                            return
                        except OSError:
                            continue
                else:
                    self._lib = ctypes.CDLL(name)
                    return
            except OSError:
                continue
        self._lib = None

    def _init_nvml(self) -> None:
        try:
            rc = self._lib.nvmlInit_v2()  # type: ignore[union-attr]
            self._initialized = rc == self.NVML_SUCCESS
        except (AttributeError, OSError):
            self._initialized = False

    @property
    def available(self) -> bool:
        return self._lib is not None and self._initialized

    def shutdown(self) -> None:
        if self._lib is not None and self._initialized:
            try:
                self._lib.nvmlShutdown()
            except OSError:
                pass
            self._initialized = False

    # -- data structures -------------------------------------------------- #

    class _MemoryInfo(ctypes.Structure):
        _fields_ = [
            ("total", ctypes.c_ulonglong),
            ("free", ctypes.c_ulonglong),
            ("used", ctypes.c_ulonglong),
        ]

    class _Utilization(ctypes.Structure):
        _fields_ = [
            ("gpu", ctypes.c_uint),
            ("memory", ctypes.c_uint),
        ]

    # -- device queries ----------------------------------------------- #

    def device_count(self) -> int:
        if not self.available:
            return 0
        count = ctypes.c_uint(0)
        try:
            rc = self._lib.nvmlDeviceGetCount_v2(ctypes.byref(count))  # type: ignore[union-attr]
            if rc != self.NVML_SUCCESS:
                return 0
        except (AttributeError, OSError):
            return 0
        return int(count.value)

    def device_info(self, index: int) -> Optional[GPUInfo]:
        if not self.available:
            return None
        try:
            handle = ctypes.c_void_p()
            rc = self._lib.nvmlDeviceGetHandleByIndex_v2(  # type: ignore[union-attr]
                ctypes.c_uint(index), ctypes.byref(handle)
            )
            if rc != self.NVML_SUCCESS:
                return None

            name_buf = ctypes.create_string_buffer(96)
            self._lib.nvmlDeviceGetName(handle, name_buf, ctypes.c_uint(96))  # type: ignore[union-attr]
            name = name_buf.value.decode("utf-8", errors="replace").strip() or f"GPU {index}"

            info = GPUInfo(index=index, name=name)

            mem = self._MemoryInfo()
            if self._lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) == self.NVML_SUCCESS:  # type: ignore[union-attr]
                info.memory_used_mb = round(mem.used / (1024 ** 2), 1)
                info.memory_total_mb = round(mem.total / (1024 ** 2), 1)

            util = self._Utilization()
            if self._lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util)) == self.NVML_SUCCESS:  # type: ignore[union-attr]
                info.utilization_pct = float(util.gpu)

            temp = ctypes.c_uint(0)
            # NVML_TEMPERATURE_GPU = 0
            if self._lib.nvmlDeviceGetTemperature(handle, ctypes.c_uint(0), ctypes.byref(temp)) == self.NVML_SUCCESS:  # type: ignore[union-attr]
                info.temperature_c = float(temp.value)

            power = ctypes.c_uint(0)
            if self._lib.nvmlDeviceGetPowerUsage(handle, ctypes.byref(power)) == self.NVML_SUCCESS:  # type: ignore[union-attr]
                info.power_watts = round(power.value / 1000.0, 1)

            return info
        except (AttributeError, OSError):
            return None


# --------------------------------------------------------------------------- #
# Public monitor
# --------------------------------------------------------------------------- #

class SystemMonitor:
    """
    High-level facade used by the rest of pyalert to grab a resource
    snapshot. Instantiate once and reuse; NVML is initialized lazily and
    only once per process.
    """

    def __init__(self, track_gpu: bool = True, track_disk: bool = True,
                 track_network: bool = True) -> None:
        self.track_gpu = track_gpu
        self.track_disk = track_disk
        self.track_network = track_network

        self._nvml: Optional[_NVMLBinding] = None
        self._nvml_warned = False
        self._boot_time = psutil.boot_time() if psutil else None

        if self.track_gpu:
            self._init_nvml()

    def _init_nvml(self) -> None:
        try:
            self._nvml = _NVMLBinding()
            if not self._nvml.available and not self._nvml_warned:
                sys.stderr.write(
                    "[pyalert] INFO: NVIDIA NVML library not found — GPU "
                    "metrics will be omitted (this is normal on machines "
                    "without an NVIDIA GPU/driver).\n"
                )
                self._nvml_warned = True
        except Exception as exc:  # noqa: BLE001 - must never crash host code
            sys.stderr.write(f"[pyalert] WARNING: GPU monitor init failed ({exc}). Continuing without GPU data.\n")
            self._nvml = None

    def _gpu_snapshot(self) -> List[GPUInfo]:
        if not self.track_gpu or self._nvml is None or not self._nvml.available:
            return []
        try:
            count = self._nvml.device_count()
            gpus = []
            for i in range(count):
                info = self._nvml.device_info(i)
                if info is not None:
                    gpus.append(info)
            return gpus
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[pyalert] WARNING: failed reading GPU stats ({exc}).\n")
            return []

    def _process_rss_mb(self) -> Optional[float]:
        """Aggregate RSS of the current process plus all live children (MB)."""
        if psutil is None:
            return None
        try:
            proc = psutil.Process(os.getpid())
            total = proc.memory_info().rss
            for child in proc.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return round(total / (1024 ** 2), 1)
        except Exception:  # noqa: BLE001
            return None

    def snapshot(self) -> SystemSnapshot:
        """Collect a full system snapshot. Never raises."""
        snap = SystemSnapshot(
            hostname=socket.gethostname(),
            platform=f"{platform.system()} {platform.release()}",
            python_version=platform.python_version(),
            pid=os.getpid(),
        )

        if psutil is not None:
            try:
                per_core = psutil.cpu_percent(interval=0.1, percpu=True)
                snap.cpu_percent_per_core = [round(c, 1) for c in per_core]
                snap.cpu_percent_total = (
                    round(sum(per_core) / len(per_core), 1) if per_core else None
                )
            except Exception:  # noqa: BLE001
                pass

            try:
                if hasattr(os, "getloadavg"):
                    snap.load_average = [round(x, 2) for x in os.getloadavg()]
            except (OSError, AttributeError):
                snap.load_average = None

            try:
                vm = psutil.virtual_memory()
                snap.ram_used_mb = round(vm.used / (1024 ** 2), 1)
                snap.ram_total_mb = round(vm.total / (1024 ** 2), 1)
                snap.ram_percent = vm.percent
            except Exception:  # noqa: BLE001
                pass

            snap.process_rss_mb = self._process_rss_mb()

            if self.track_disk:
                try:
                    du = psutil.disk_usage(os.getcwd())
                    snap.disk_used_gb = round(du.used / (1024 ** 3), 2)
                    snap.disk_total_gb = round(du.total / (1024 ** 3), 2)
                    snap.disk_percent = du.percent
                except Exception:  # noqa: BLE001
                    pass

            if self.track_network:
                try:
                    net = psutil.net_io_counters()
                    snap.net_sent_mb = round(net.bytes_sent / (1024 ** 2), 1)
                    snap.net_recv_mb = round(net.bytes_recv / (1024 ** 2), 1)
                except Exception:  # noqa: BLE001
                    pass

            if self._boot_time:
                snap.uptime_seconds = round(time.time() - self._boot_time, 0)

        snap.gpus = self._gpu_snapshot()
        return snap

    def close(self) -> None:
        """Release the NVML handle. Safe to call multiple times."""
        if self._nvml is not None:
            self._nvml.shutdown()

"""Know what this machine actually has - without assuming anything.

The user's laptop is a Lenovo LOQ with an RTX 3050 (6 GB) and 16 GB of RAM, but
LUMI must work on any Windows machine, and must never claim "you have a 6 GB
GPU" when it cannot see one.  Everything here is measured, and everything has a
graceful answer when a probe fails.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("lumi.ai.hardware")

NVIDIA_SMI_TIMEOUT_S = 4.0
#: Rough VRAM/quantisation table used for planning, not for promises.
GB = 1024 ** 3


@dataclass
class GpuInfo:
    name: str
    vram_total_gb: float = 0.0
    vram_free_gb: float = 0.0
    driver: str = ""
    source: str = ""

    @property
    def usable(self) -> bool:
        return self.vram_total_gb > 0

    @property
    def vram_gb(self) -> float:
        """Alias used by the tools and the UI (rounded to one decimal)."""
        return round(self.vram_total_gb, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "vram_total_gb": round(self.vram_total_gb, 1),
            "vram_free_gb": round(self.vram_free_gb, 1),
            "driver": self.driver,
            "source": self.source,
        }


@dataclass
class HardwareInfo:
    platform_name: str = ""
    cpu: str = ""
    physical_cores: int = 0
    logical_cores: int = 0
    ram_total_gb: float = 0.0
    ram_available_gb: float = 0.0
    gpus: list[GpuInfo] = field(default_factory=list)
    disk_free_gb: float = 0.0
    disk_total_gb: float = 0.0
    has_nvidia_smi: bool = False
    notes: list[str] = field(default_factory=list)

    # --------------------------------------------------------------- helpers
    @property
    def primary_gpu(self) -> Optional[GpuInfo]:
        usable = [gpu for gpu in self.gpus if gpu.usable]
        if not usable:
            return None
        return max(usable, key=lambda gpu: gpu.vram_total_gb)

    @property
    def vram_budget_gb(self) -> float:
        """How much VRAM LUMI may *plan* to use, leaving room for the desktop."""
        gpu = self.primary_gpu
        if gpu is None:
            return 0.0
        # A Windows desktop, a browser and a video call all share this memory.
        return round(max(0.0, gpu.vram_total_gb - 1.0), 1)

    @property
    def ram_budget_gb(self) -> float:
        """How much system RAM a model may plan to use."""
        return round(max(0.0, min(self.ram_available_gb, self.ram_total_gb * 0.6)), 1)

    def fit_for_model(self, size_bytes: float, *, overhead_gb: float = 1.2) -> dict[str, str]:
        """How well a model of ``size_bytes`` fits, in words a student can act on."""
        size_gb = float(size_bytes or 0) / GB
        level = fit_for_model(self, size_gb, overhead_gb=overhead_gb)
        message = {
            "good": f"Fits comfortably: about {size_gb:.1f} GB of weights against {self.vram_budget_gb:.1f} GB of usable GPU memory.",
            "partial": "Partly fits on the GPU - some layers will run on the CPU, so it will be slower.",
            "cpu": "No room in GPU memory: this will run on the CPU (usable, but slower).",
            "too_big": f"Too big for this machine (about {self.ram_budget_gb:.1f} GB of RAM budget available).",
        }[level]
        return {"level": level, "label": level.replace("_", " "), "message": message}

    def summary_line(self) -> str:
        bits = [f"CPU: {self.cpu or 'unknown'}"]
        if self.physical_cores:
            bits.append(f"{self.physical_cores} cores")
        bits.append(f"RAM: {self.ram_total_gb:.1f} GB ({self.ram_available_gb:.1f} GB free)")
        gpu = self.primary_gpu
        bits.append(f"GPU: {gpu.name} ({gpu.vram_total_gb:.1f} GB)" if gpu else "GPU: none detected")
        bits.append(f"Disk free: {self.disk_free_gb:.0f} GB")
        return " · ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform_name,
            "cpu": self.cpu,
            "physical_cores": self.physical_cores,
            "logical_cores": self.logical_cores,
            "ram_total_gb": round(self.ram_total_gb, 1),
            "ram_available_gb": round(self.ram_available_gb, 1),
            "ram_budget_gb": self.ram_budget_gb,
            "vram_budget_gb": self.vram_budget_gb,
            "gpus": [gpu.to_dict() for gpu in self.gpus],
            "disk_free_gb": round(self.disk_free_gb, 1),
            "disk_total_gb": round(self.disk_total_gb, 1),
            "has_nvidia_smi": self.has_nvidia_smi,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------
def _run(cmd: list[str], timeout: float = NVIDIA_SMI_TIMEOUT_S) -> str:
    """Run a probe quietly.  Never raises; returns "" when the tool is absent."""
    if not shutil.which(cmd[0]) and not Path(cmd[0]).exists():
        return ""
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("probe %s failed: %s", cmd[0], exc)
        return ""
    return (completed.stdout or "").strip()


def _nvidia_smi_paths() -> list[str]:
    names = ["nvidia-smi"]
    for base in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramW6432", r"C:\Program Files"),
        os.environ.get("SystemRoot", r"C:\Windows"),
    ):
        if base:
            names.append(str(Path(base) / "System32" / "nvidia-smi.exe"))
            names.append(str(Path(base) / "System32" / "nvidia-smi"))
    return names


def detect_nvidia_gpus() -> list[GpuInfo]:
    """Read the GPU list from ``nvidia-smi`` when it exists."""
    query = "name,memory.total,memory.free,driver_version"
    for candidate in _nvidia_smi_paths():
        output = _run([candidate, f"--query-gpu={query}", "--format=csv,noheader,nounits"])
        if not output:
            continue
        gpus: list[GpuInfo] = []
        for line in output.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 4:
                continue
            try:
                total = float(parts[1]) / 1024
                free = float(parts[2]) / 1024
            except ValueError:
                continue
            gpus.append(
                GpuInfo(
                    name=parts[0] or "NVIDIA GPU",
                    vram_total_gb=total,
                    vram_free_gb=free,
                    driver=parts[3],
                    source="nvidia-smi",
                )
            )
        if gpus:
            return gpus
    return []


def detect_windows_gpus() -> list[GpuInfo]:
    """Fall back to WMI, which reports *all* adapters (Intel iGPU included)."""
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Json -Compress"
    )
    output = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=8.0)
    if not output:
        return []
    import json

    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return []
    entries = payload if isinstance(payload, list) else [payload]
    gpus: list[GpuInfo] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("Name") or "GPU")
        raw = entry.get("AdapterRAM")
        try:
            vram = float(raw) / GB if raw else 0.0
        except (TypeError, ValueError):
            vram = 0.0
        # Win32_VideoController caps AdapterRAM at 4 GB on some drivers; mark it unknown.
        if vram and vram >= 3.9:
            vram = 0.0
        gpus.append(
            GpuInfo(
                name=name,
                vram_total_gb=vram,
                vram_free_gb=0.0,
                driver=str(entry.get("DriverVersion") or ""),
                source="wmi",
            )
        )
    return gpus


def detect_apple_gpu() -> list[GpuInfo]:
    """Apple silicon reports its GPU through system_profiler."""
    if sys.platform != "darwin":
        return []
    output = _run(["system_profiler", "SPDisplaysDataType"], timeout=8.0)
    if not output:
        return []
    name = ""
    vram = 0.0
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Chipset Model:"):
            name = stripped.split(":", 1)[1].strip()
        elif "VRAM" in stripped or "Unified Memory" in stripped:
            digits = "".join(char for char in stripped.split(":", 1)[-1] if char.isdigit())
            if digits:
                vram = float(digits) / 1024
    if name:
        return [GpuInfo(name=name, vram_total_gb=vram, vram_free_gb=vram, source="system_profiler")]
    return []


def _ram_gb() -> tuple[float, float]:
    """Total and available RAM in GB."""
    try:
        import psutil  # type: ignore

        memory = psutil.virtual_memory()
        return memory.total / GB, memory.available / GB
    except Exception:  # pragma: no cover - psutil missing or unsupported
        pass
    if os.name == "nt":  # pragma: no cover - Windows only
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return status.ullTotalPhys / GB, status.ullAvailPhys / GB
        except Exception:
            pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        available = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return pages * page_size / GB, available * page_size / GB
    except (ValueError, OSError, AttributeError):
        return 0.0, 0.0


def _cpu_name() -> str:
    machine = platform.machine() or "unknown"
    if os.name == "nt":  # pragma: no cover - Windows only
        name = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "(Get-CimInstance Win32_Processor).Name"],
            timeout=6.0,
        )
        if name:
            return name.splitlines()[0].strip()
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return f"{platform.processor() or machine}"


def _cores() -> tuple[int, int]:
    try:
        import psutil  # type: ignore

        physical = psutil.cpu_count(logical=False) or 0
        logical = psutil.cpu_count(logical=True) or 0
        return int(physical), int(logical)
    except Exception:
        logical = os.cpu_count() or 0
        return 0, int(logical)


def _disk() -> tuple[float, float]:
    try:
        target = Path.home().anchor or "/"
        usage = shutil.disk_usage(target)
        return usage.free / GB, usage.total / GB
    except OSError:
        return 0.0, 0.0


def _notes_for(info: HardwareInfo) -> list[str]:
    notes: list[str] = []
    gpu = info.primary_gpu
    if gpu is None:
        notes.append(
            "No NVIDIA GPU was detected. LUMI still works; replies come from the CPU and will be slower."
        )
        if info.gpus:
            notes.append(
                "A graphics adapter was found but its memory could not be read reliably, so LUMI will not "
                "promise GPU acceleration."
            )
    elif gpu.vram_total_gb < 6:
        notes.append(
            f"{gpu.name} has about {gpu.vram_total_gb:.0f} GB of VRAM, so 7B models at Q4 are the practical limit "
            "(3B models are comfortable)."
        )
    elif gpu.vram_total_gb < 4:
        notes.append("This GPU has little free VRAM; 1-3B models are the realistic choice.")
    if info.ram_total_gb and info.ram_total_gb < 12:
        notes.append("8 GB of RAM is tight for local models - stick to 1-3B parameter models.")
    if info.disk_free_gb and info.disk_free_gb < 12:
        notes.append("Free disk space is low; a 4 GB model download may not fit.")
    return notes


def detect(*, refresh: bool = True) -> HardwareInfo:
    """Measure this machine.  Never raises, always returns something usable."""
    del refresh  # kept for API symmetry; probes are cheap enough to always run
    physical, logical = _cores()
    total_ram, available_ram = _ram_gb()
    disk_free, disk_total = _disk()

    gpus = detect_nvidia_gpus()
    has_smi = bool(gpus) or bool(shutil.which("nvidia-smi"))
    if not gpus:
        gpus = detect_apple_gpu() or detect_windows_gpus()

    info = HardwareInfo(
        platform_name=f"{platform.system().lower()} ({os.name})",
        cpu=_cpu_name(),
        physical_cores=physical,
        logical_cores=logical,
        ram_total_gb=total_ram,
        ram_available_gb=available_ram,
        gpus=gpus,
        disk_free_gb=disk_free,
        disk_total_gb=disk_total,
        has_nvidia_smi=has_smi,
    )
    info.notes = _notes_for(info)
    return info


def fit_for_model(info: HardwareInfo, model_size_gb: float, *, overhead_gb: float = 1.2) -> str:
    """Classify how well a model of ``model_size_gb`` fits: good/partial/cpu/too_big."""
    needed = model_size_gb + overhead_gb
    gpu = info.primary_gpu
    if gpu is not None and needed <= info.vram_budget_gb:
        return "good"
    if gpu is not None and needed <= max(info.vram_budget_gb + 2.0, 0.0):
        return "partial"
    if needed <= info.ram_budget_gb:
        return "cpu"
    return "too_big"


def best_model_note(info: HardwareInfo) -> str:
    """One honest sentence about what this machine can run."""
    if info.primary_gpu is not None:
        return f"Runs well up to about {info.vram_budget_gb:.0f} GB of model weights on the GPU."
    if info.ram_total_gb >= 12:
        return "No usable GPU was found: 1-3B models will run on the CPU, slower but correctly."
    return "This machine is small for local models; 1-3B models are the realistic choice."


def total_ram_gb() -> float:
    return _ram_gb()[0]


def available_ram_gb() -> float:
    return _ram_gb()[1]


def current_load() -> dict[str, Any]:
    """Cheap load snapshot for the status bar."""
    load: dict[str, Any] = {}
    try:
        import psutil  # type: ignore

        load["cpu_percent"] = float(psutil.cpu_percent(interval=None))
        memory = psutil.virtual_memory()
        load["ram_percent"] = float(memory.percent)
        load["ram_available_gb"] = round(memory.available / GB, 1)
    except Exception:  # pragma: no cover - psutil missing
        pass
    return load

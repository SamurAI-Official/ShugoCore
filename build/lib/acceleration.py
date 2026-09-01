"""
ShugoCore hardware acceleration abstraction
===========================================

Maps workload classes (LLM inference, vision, audio, general compute) onto
the accelerators actually present on a device, in strict preference order,
with a deterministic CPU fallback that always exists.

Design invariants:

- **Hints, not dependencies.** Core modules receive resolved ladders as
  metadata; nothing in the decision path imports platform code.
- **Never blocks.** Detection is lazy, cached, and failure-tolerant: an
  enumerator crash yields "CPU only", never an exception in the loop.
- **Graceful degradation.** A failing accelerator is demoted (audited) and
  the ladder falls through; only when even the CPU path fails does the
  runtime escalate to the fallback controller.
- **Thermal awareness.** ``apply_thermal_level`` demotes power-hungry
  accelerators as a device heats up, so sustained on-device operation
  degrades instead of throttling into watchdog territory.

Platform enumerators are pluggable: Android (via the Kotlin bridge - NNAPI,
Hexagon, MediaTek APU, Adreno/Mali GPUs), Linux hosts (Jetson DLA, Intel
NPU, render nodes), and a CPU-only generic fallback.
"""

import glob
import logging
import os
import sys
import threading
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from telemetry import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer("shugocore.acceleration")


class AcceleratorKind(str, Enum):
    """Classes of compute device, in rough power/performance order."""

    NPU = "npu"      # NNAPI, Hexagon HTB, Jetson DLA, APU, Intel NPU
    DSP = "dsp"      # signal DSP (Hexagon cDSP - vision/audio offload)
    GPU = "gpu"      # Vulkan/OpenCL GPU (Adreno, Mali, desktop)
    CPU = "cpu"      # deterministic fallback; always available


class WorkloadClass(str, Enum):
    """Kinds of compute workload ShugoCore dispatches."""

    LLM = "llm"
    VISION = "vision"
    AUDIO = "audio"
    GENERAL = "general"


class AcceleratorDevice:
    """One detected compute device."""

    __slots__ = ("kind", "name", "source", "details")

    def __init__(self, kind: str, name: str, source: str = "unknown",
                 details: Optional[Dict[str, Any]] = None):
        try:
            self.kind = AcceleratorKind(str(kind).lower())
        except ValueError:
            self.kind = AcceleratorKind.CPU
        self.name = str(name)[:80]
        self.source = str(source)[:32]
        self.details = dict(details or {})

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind.value, "name": self.name,
                "source": self.source, "details": self.details}

    def __repr__(self) -> str:
        return f"AcceleratorDevice({self.kind.value}:{self.name} via {self.source})"


# Default preference ladders per workload class. CPU is always last and
# always available; resolve() guarantees a non-empty result.
DEFAULT_LADDERS: Dict[str, Tuple[AcceleratorKind, ...]] = {
    WorkloadClass.LLM.value: (AcceleratorKind.NPU, AcceleratorKind.GPU, AcceleratorKind.CPU),
    WorkloadClass.VISION.value: (AcceleratorKind.NPU, AcceleratorKind.DSP,
                                 AcceleratorKind.GPU, AcceleratorKind.CPU),
    WorkloadClass.AUDIO.value: (AcceleratorKind.DSP, AcceleratorKind.NPU,
                                AcceleratorKind.GPU, AcceleratorKind.CPU),
    WorkloadClass.GENERAL.value: (AcceleratorKind.NPU, AcceleratorKind.GPU, AcceleratorKind.CPU),
}

# Thermal level -> kinds demoted to CPU-only while at that level.
_THERMAL_DEMOTIONS: Dict[int, set] = {
    0: set(),                                       # nominal
    1: {AcceleratorKind.GPU},                       # light: GPU throttles first
    2: {AcceleratorKind.GPU, AcceleratorKind.NPU,
         AcceleratorKind.DSP},                        # elevated: CPU only
}


def detect_platform() -> str:
    """
    Heuristic platform detection. Returns one of:
    ``termux`` / ``android`` / ``linux`` / ``darwin`` / ``windows`` / ``unknown``.

    Termux is checked before generic Android markers (it also has
    ``/system/build.prop`` but is a Linux userland, not an app process).
    """
    prefix = os.environ.get("PREFIX", "")
    if "com.termux" in prefix:
        return "termux"
    if (os.environ.get("ANDROID_ART_ROOT")
            or os.environ.get("ANDROID_DATA") == "/data"
            or os.path.exists("/system/build.prop")):
        return "android"  # embedded app process (Chaquopy or similar)
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "windows"
    return "unknown"


# ---------------------------------------------------------------------------
# Platform enumerators (each returns devices; the policy appends CPU)
# ---------------------------------------------------------------------------
def enumerate_android(bridge: Any) -> List[AcceleratorDevice]:
    """
    Enumerate accelerators through the Kotlin bridge (Chaquopy). The bridge
    contract method ``enumerateAccelerators()`` returns JSON-safe dicts:
    ``[{"kind": "npu", "name": "Hexagon", "details": {...}}, ...]``.
    Unknown kinds coerce to CPU; malformed input never raises.
    """
    devices: List[AcceleratorDevice] = []
    try:
        raw = bridge.enumerateAccelerators()
    except Exception as exc:
        logger.warning("Android accelerator enumeration failed: %s", exc)
        return devices
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        devices.append(AcceleratorDevice(
            kind=str(entry.get("kind", "cpu")),
            name=str(entry.get("name", "android-accelerator")),
            source="android",
            details=entry.get("details") if isinstance(entry.get("details"), dict) else {},
        ))
    return devices


def _probe_exists(path: str) -> bool:
    try:
        return os.path.exists(path)
    except Exception:
        return False


def enumerate_linux(probe: Optional[Callable[[str], bool]] = None) -> List[AcceleratorDevice]:
    """
    Filesystem-probe enumeration for Linux hosts (also used on Termux,
    which presents a Linux userland). ``probe`` is injectable for tests.
    """
    probe = probe or _probe_exists
    devices: List[AcceleratorDevice] = []
    try:
        with open("/proc/device-tree/model", "r",
                  encoding="utf-8", errors="ignore") as handle:
            model = handle.read().strip("\x00\n ")
    except Exception:
        model = ""
    if "Jetson" in model:
        devices.append(AcceleratorDevice("npu", f"Jetson DLA ({model[:48]})", "linux",
                                         {"device_tree_model": model}))
    if probe("/dev/accel/accel0"):
        devices.append(AcceleratorDevice("npu", "Intel NPU (accel subsystem)", "linux"))
    if probe("/dev/kgsl-3d0"):
        devices.append(AcceleratorDevice("gpu", "Adreno GPU", "linux"))
    if probe("/dev/mali") or probe("/dev/mali0"):
        devices.append(AcceleratorDevice("gpu", "Mali GPU", "linux"))
    if glob.glob("/dev/fastrpc-*") or probe("/dev/fastrpc"):
        devices.append(AcceleratorDevice("dsp", "Hexagon DSP (fastrpc)", "linux"))
    if glob.glob("/dev/dri/renderD*"):
        devices.append(AcceleratorDevice("gpu", "DRM render node GPU", "linux"))
    return devices


def enumerate_generic() -> List[AcceleratorDevice]:
    """Conservative fallback for darwin/windows/unknown platforms."""
    return []


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
class AccelerationPolicy:
    """
    Resolves workload classes to ordered accelerator ladders and manages
    degradation (failed accelerators, thermal demotion). Thread-safe.
    """

    def __init__(self,
                 devices: Optional[List[AcceleratorDevice]] = None,
                 ladders: Optional[Dict[str, Tuple[AcceleratorKind, ...]]] = None,
                 audit: Optional[Any] = None):
        self._lock = threading.Lock()
        self._ladders = dict(DEFAULT_LADDERS)
        if ladders:
            self._ladders.update(ladders)
        self._devices: List[AcceleratorDevice] = []
        self._failed: Dict[str, float] = {}   # device.name -> monotonic time
        self._thermal_level = 0
        self._audit = audit
        self.set_devices(devices or [])

    # -- device management ----------------------------------------------------
    def set_devices(self, devices: List[AcceleratorDevice]) -> None:
        """Replace the detected device set; a CPU device is always ensured."""
        with self._lock:
            self._devices = list(devices)
            if not any(d.kind is AcceleratorKind.CPU for d in self._devices):
                cpu = AcceleratorDevice("cpu", f"CPU ({os.cpu_count() or '?'} cores)",
                                        "implicit")
                self._devices.append(cpu)

    def detect(self, bridge: Optional[Any] = None) -> List[AcceleratorDevice]:
        """Detect devices for the current platform (lazy; safe everywhere)."""
        platform = detect_platform()
        if platform == "android" and bridge is not None:
            found = enumerate_android(bridge)
        elif platform in ("linux", "termux"):
            found = enumerate_linux()
        else:
            found = enumerate_generic()
        self.set_devices(found)
        return self.devices()

    def devices(self) -> List[AcceleratorDevice]:
        with self._lock:
            return [d for d in self._devices
                    if d.name not in self._failed
                    and d.kind not in _THERMAL_DEMOTIONS.get(self._thermal_level, set())]

    # -- resolution -------------------------------------------------------------
    def resolve(self, workload: str) -> List[AcceleratorDevice]:
        """
        Ordered device ladder for ``workload`` (best first). Never empty:
        the CPU device survives every degradation path.
        """
        span = tracer.start_span("acceleration.resolve", {"workload": str(workload)})
        with span as active_span:
            available = {d.kind: d for d in self.devices()}
            try:
                workload_key = WorkloadClass(str(workload)).value
            except ValueError:
                workload_key = WorkloadClass.GENERAL.value
            ladder = self._ladders.get(workload_key,
                                       DEFAULT_LADDERS[WorkloadClass.GENERAL.value])
            resolved = [available[kind] for kind in ladder if kind in available]
            with self._lock:
                cpu = next((d for d in self._devices
                            if d.kind is AcceleratorKind.CPU), None)
            if not resolved and cpu is not None:
                resolved = [cpu]
            active_span.set_attribute("resolved_kinds",
                                      ",".join(d.kind.value for d in resolved))
            return resolved

    def preferred(self, workload: str) -> Optional[AcceleratorDevice]:
        ladder = self.resolve(workload)
        return ladder[0] if ladder else None

    # -- degradation --------------------------------------------------------------
    def report_failure(self, device: AcceleratorDevice, detail: str = "") -> None:
        """Demote a failing accelerator for the remainder of the session."""
        with self._lock:
            self._failed[device.name] = time.monotonic()
        logger.warning("Accelerator demoted: %s (%s)", device.name, detail)
        self._audit_event("accelerator_demoted", {
            "device": device.name, "kind": device.kind.value,
            "detail": str(detail)[:150]})

    def report_recovery(self, device_name: str) -> None:
        with self._lock:
            self._failed.pop(str(device_name), None)

    def apply_thermal_level(self, level: int) -> int:
        """
        0 = nominal, 1 = light (GPU demoted), 2 = elevated (CPU only).
        Returns the active level; demotion events are audited.
        """
        level = max(0, min(2, int(level)))
        with self._lock:
            previous = self._thermal_level
            self._thermal_level = level
        if level != previous:
            logger.info("Thermal level %s -> %s (demotions: %s)",
                        previous, level,
                        sorted(k.value for k in _THERMAL_DEMOTIONS.get(level, set())))
            self._audit_event("thermal_demotion", {"from": previous, "to": level})
        return level

    @property
    def thermal_level(self) -> int:
        with self._lock:
            return self._thermal_level

    def describe(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "platform": detect_platform(),
                "devices": [d.to_dict() for d in self._devices],
                "failed": sorted(self._failed),
                "thermal_level": self._thermal_level,
            }

    def _audit_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self._audit is None:
            return
        try:
            self._audit.append(event_type, payload)
        except Exception:
            pass



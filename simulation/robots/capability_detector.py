"""
ShugoCore Android Capability Detector
======================================

Detects hardware capabilities on Android devices for optimal model selection
and inference configuration. Works via:
- JNI bridge to Kotlin shell (primary)
- /proc filesystem parsing (fallback)
- Python stdlib only (no dependencies)

Detects:
- SoC (Snapdragon, MediaTek, Google Tensor, Exynos)
- NPU/DSP/GPU capabilities
- RAM availability → model size budget
- Thermal state → inference throttling decisions
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class HardwareCapabilities:
    """Detected hardware capabilities for an Android device."""

    # SoC info
    soc_manufacturer: str = "unknown"
    soc_model: str = "unknown"
    soc_name: str = "unknown"
    hardware: str = "unknown"

    # Compute capabilities
    has_npu: bool = False
    has_dsp: bool = False
    has_gpu: bool = False
    gpu_name: str = "unknown"
    gpu_api: str = "unknown"

    # Memory
    total_ram_bytes: int = 0
    available_ram_bytes: int = 0

    # Model budget (calculated)
    model_ram_budget_bytes: int = 0
    recommended_context_size: int = 2048

    # Thermal state
    thermal_status: int = 0
    battery_level: int = 100
    is_charging: bool = False

    # Inference config (derived)
    recommended_quantization: str = "Q4_K_M"
    recommended_gpu_layers: int = 0
    supports_gpu_offload: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "soc": {
                "manufacturer": self.soc_manufacturer,
                "model": self.soc_model,
                "name": self.soc_name,
                "hardware": self.hardware,
            },
            "compute": {
                "has_npu": self.has_npu,
                "has_dsp": self.has_dsp,
                "has_gpu": self.has_gpu,
                "gpu_name": self.gpu_name,
                "gpu_api": self.gpu_api,
            },
            "memory": {
                "total_ram_mb": self.total_ram_bytes // (1024 * 1024),
                "available_ram_mb": self.available_ram_bytes // (1024 * 1024),
                "model_budget_mb": self.model_ram_budget_bytes // (1024 * 1024),

class CapabilityDetector:
    """Detects Android device hardware capabilities."""

    SOC_DATABASE: Dict[str, Dict[str, Any]] = {
        "sm8650": {"manufacturer": "Qualcomm", "model": "Snapdragon 8 Gen 3", "has_npu": True, "has_dsp": True},
        "sm8550": {"manufacturer": "Qualcomm", "model": "Snapdragon 8 Gen 2", "has_npu": True, "has_dsp": True},
        "sm8475": {"manufacturer": "Qualcomm", "model": "Snapdragon 8+ Gen 1", "has_npu": True, "has_dsp": True},
        "sm8450": {"manufacturer": "Qualcomm", "model": "Snapdragon 8 Gen 1", "has_npu": True, "has_dsp": True},
        "sm8350": {"manufacturer": "Qualcomm", "model": "Snapdragon 888", "has_npu": True, "has_dsp": True},
        "sm7475": {"manufacturer": "Qualcomm", "model": "Snapdragon 7+ Gen 2", "has_npu": True, "has_dsp": True},
        "gs201": {"manufacturer": "Google", "model": "Tensor G2", "has_npu": True, "has_dsp": False},
        "gs301": {"manufacturer": "Google", "model": "Tensor G3", "has_npu": True, "has_dsp": False},
        "mt6989": {"manufacturer": "MediaTek", "model": "Dimensity 9300", "has_npu": True, "has_dsp": True},
        "mt6985": {"manufacturer": "MediaTek", "model": "Dimensity 9200", "has_npu": True, "has_dsp": True},
    }

    def __init__(self, bridge: Optional[Any] = None):
        self._bridge = bridge

    def detect(self) -> HardwareCapabilities:
        caps = HardwareCapabilities()
        if self._bridge is not None:
            try:
                self._detect_via_bridge(caps)
            except Exception as exc:
                logger.warning(f"Bridge detection failed: {exc}")
                self._detect_via_proc(caps)
        else:
            self._detect_via_proc(caps)
        self._calculate_model_budget(caps)
        self._calculate_inference_config(caps)
        return caps

    def _detect_via_bridge(self, caps: HardwareCapabilities) -> None:
        """Detect via JNI bridge to Kotlin shell."""
        try:
            hw_info = self._bridge.getHardwareInfo()
            if hw_info:
                caps.soc_name = hw_info.get("boardPlatform", "unknown")
                caps.hardware = hw_info.get("hardware", "unknown")
                caps.total_ram_bytes = hw_info.get("totalRam", 0)
                caps.available_ram_bytes = hw_info.get("availableRam", 0)
                caps.has_gpu = hw_info.get("hasGpu", False)
                caps.gpu_name = hw_info.get("gpuName", "unknown")

    def _detect_via_proc(self, caps: HardwareCapabilities) -> None:
        """Detect via /proc filesystem (fallback)."""
        cpuinfo = self._read_proc_file("/proc/cpuinfo")
        if cpuinfo:
            self._parse_cpuinfo(caps, cpuinfo)
        meminfo = self._read_proc_file("/proc/meminfo")
        if meminfo:
            self._parse_meminfo(caps, meminfo)
        self._detect_gpu(caps)

    def _read_proc_file(self, path: str) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except (FileNotFoundError, PermissionError, OSError):
            return None

    def _parse_cpuinfo(self, caps: HardwareCapabilities, cpuinfo: str) -> None:
        hardware_match = re.search(r"Hardware\s*:\s*(.+)", cpuinfo)
        if hardware_match:
            caps.hardware = hardware_match.group(1).strip()
        model_match = re.search(r"model name\s*:\s*(.+)", cpuinfo)
        if model_match:
            soc_name = model_match.group(1).strip()
            for key, info in self.SOC_DATABASE.items():
                if key.lower() in soc_name.lower():
                    caps.soc_manufacturer = info["manufacturer"]
                    caps.soc_model = info["model"]
                    caps.soc_name = key
                    caps.has_npu = info.get("has_npu", False)
                    caps.has_dsp = info.get("has_dsp", False)
                    break

    def _parse_meminfo(self, caps: HardwareCapabilities, meminfo: str) -> None:
        total_match = re.search(r"MemTotal\s*:\s+(\d+)\s+kB", meminfo)
        if total_match:
            caps.total_ram_bytes = int(total_match.group(1)) * 1024
        avail_match = re.search(r"MemAvailable\s*:\s+(\d+)\s+kB", meminfo)
        if avail_match:
            caps.available_ram_bytes = int(avail_match.group(1)) * 1024

    def _detect_gpu(self, caps: HardwareCapabilities) -> None:
        vulkan_paths = [
            "/system/lib64/libvulkan.so",
            "/system/lib/libvulkan.so",
            "/vendor/lib64/libvulkan.so",
        ]
        for path in vulkan_paths:
            if os.path.exists(path):
                caps.has_gpu = True
                caps.gpu_api = "Vulkan"
                break
        try:
            result = subprocess.run(
                ["getprop", "ro.hardware.egl"],
                capture_output=True, text=True, timeout=1.0
            )
            if result.stdout.strip():
                egl = result.stdout.strip().lower()
                if "adreno" in egl:
                    caps.gpu_name = "Adreno"
                elif "mali" in egl:
                    caps.gpu_name = "Mali"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    def _calculate_model_budget(self, caps: HardwareCapabilities) -> None:
        if caps.available_ram_bytes > 0:
            caps.model_ram_budget_bytes = int(caps.available_ram_bytes * 0.6)
        elif caps.total_ram_bytes > 0:
            caps.model_ram_budget_bytes = int(caps.total_ram_bytes * 0.4)
        available_mb = caps.available_ram_bytes // (1024 * 1024)
        if available_mb > 6000:
            caps.recommended_context_size = 8192
        elif available_mb > 3000:
            caps.recommended_context_size = 4096
        else:
            caps.recommended_context_size = 2048

    def _calculate_inference_config(self, caps: HardwareCapabilities) -> None:
        model_budget_mb = caps.model_ram_budget_bytes // (1024 * 1024)
        if model_budget_mb >= 8000:
            caps.recommended_quantization = "Q8_0"
        elif model_budget_mb >= 4000:
            caps.recommended_quantization = "Q5_K_M"
        elif model_budget_mb >= 2000:
            caps.recommended_quantization = "Q4_K_M"
        elif model_budget_mb >= 1000:
            caps.recommended_quantization = "Q3_K_M"
        else:
            caps.recommended_quantization = "Q2_K"
        if caps.has_gpu and caps.gpu_api == "Vulkan":
            caps.supports_gpu_offload = True
            if caps.available_ram_bytes > 4 * 1024 * 1024 * 1024:
                caps.recommended_gpu_layers = 20
            else:
                caps.recommended_gpu_layers = 10


def detect_capabilities(bridge: Optional[Any] = None) -> HardwareCapabilities:
    """Convenience function to detect hardware capabilities."""
    detector = CapabilityDetector(bridge=bridge)
    return detector.detect()


def recommend_model(capabilities: HardwareCapabilities) -> Dict[str, Any]:
    """Recommend a model configuration based on detected capabilities."""
    budget_mb = capabilities.model_ram_budget_bytes // (1024 * 1024)
    if budget_mb >= 7000:
        model_size = "7B"
    elif budget_mb >= 3500:
        model_size = "3B"
    elif budget_mb >= 1500:
        model_size = "1.5B"
    else:
        model_size = "0.5B"
    return {
        "model_size": model_size,
        "quantization": capabilities.recommended_quantization,
        "context_size": capabilities.recommended_context_size,
        "gpu_layers": capabilities.recommended_gpu_layers,
        "supports_gpu_offload": capabilities.supports_gpu_offload,
        "max_batch_size": 1 if capabilities.thermal_status >= 2 else 5,
    }
                caps.gpu_api = hw_info.get("gpuApi", "unknown")
        except AttributeError:
            pass
        try:
            power = self._bridge.getPowerStatus()
            if power:
                caps.battery_level = power.get("battery_level", 100)
                caps.is_charging = power.get("plugged", False)
        except AttributeError:
            pass
        try:
            thermal = self._bridge.getThermalStatus()
            if thermal is not None:
                caps.thermal_status = int(thermal)
        except (AttributeError, ValueError):
            pass
                "recommended_context_size": self.recommended_context_size,
            },
            "thermal": {
                "status": self.thermal_status,
                "battery_level": self.battery_level,
                "is_charging": self.is_charging,
            },
            "inference": {
                "recommended_quantization": self.recommended_quantization,
                "recommended_gpu_layers": self.recommended_gpu_layers,
                "supports_gpu_offload": self.supports_gpu_offload,
            },
        }
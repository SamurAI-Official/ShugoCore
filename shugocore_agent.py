# Android agent entrypoint (Chaquopy). Called from ShugoCoreService.
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("shugocore_android")


class AndroidAgent:
    def __init__(self, device_caps: Optional[str] = None,
                 api_url: Optional[str] = None):
        self.device_caps = device_caps or "Unknown"
        self.api_url = api_url or "http://127.0.0.1:11434"
        self.tick_count = 0
        # Phone telemetry pushed from the Kotlin ThermalMonitor each tick.
        self.telemetry: Dict[str, Any] = {}
        from model_backends import create_backend  # noqa: F401
        import android_inference  # noqa: F401  (registers "android" backend)
        from memory_system import MemoryManager
        self.memory = MemoryManager(agent_id=f"android-{self.device_caps}",
                                    auto_start=False)
        self.engine: Optional[Any] = self._initialize_engine()
        logger.info("AndroidAgent ready (device=%s, api=%s)", self.device_caps,
                    self.api_url)

    def _initialize_engine(self) -> Optional[Any]:
        try:
            # Lazy import: the full engine stack is optional on-device. If
            # decision_engine (or any transitive module) is not bundled or
            # its deps fail, the agent degrades to the stub observation loop
            # (telemetry/observations still flow) instead of crashing
            # agent construction.
            from decision_engine import DecisionEngine
            # `api_url` matches AndroidBackend.__init__'s signature. The true
            # on-device no-op was android_inference NOT being bundled/registered.
            return DecisionEngine(
                models=[{"id": "shugocore-local", "type": "text", "weight": 1.0,
                         "backend": {"type": "android", "api_url": self.api_url,
                                     "model_name": "shugocore-local",
                                     "device_caps": {"soc": self.device_caps}}}],
                vector_db_config={"type": "chroma"},
                memory_db_path=str(Path("semantic_memory.db")))
        except Exception as exc:
            logger.error("Failed to initialize engine: %s", exc)
            return None

    def update_telemetry(self, data: Optional[Dict[str, Any]] = None) -> None:
        """Receive a ThermalMonitor snapshot (Chaquopy Map -> dict)."""
        self.telemetry = dict(data) if data else self.telemetry or {}

    def tick(self) -> None:
        self.tick_count += 1
        try:
            observation = self._get_observation()
            self.memory.record_event("android_observation",
                payload={"tick": self.tick_count, "observation": observation},
                metadata={"source": "android_shell"})
            if self.engine is not None:
                context = self.memory.retrieve_context("maintain_agent_loop", top_k=3)
                self.engine.execute_task({"id": f"android-tick-{self.tick_count}",
                    "type": "maintain_agent_loop", "context": context})
            else:
                logger.debug("Tick %s: no engine (backend unavailable)", self.tick_count)
            if self.tick_count % 10 == 0:
                self._run_consolidation()
        except Exception as exc:
            logger.error("Tick %s error: %s", self.tick_count, exc)

    def _get_observation(self) -> Dict[str, Any]:
        """Observations from phone telemetry; safe stubs when none arrived."""
        t = self.telemetry or {}
        battery = t.get("battery_level", self._get_battery())
        mem_total = t.get("mem_total_mb", 0) or 0
        mem_avail = t.get("mem_avail_mb", 0) or 0
        memory_usage = (mem_total - mem_avail) if mem_total else self._get_memory_usage()
        return {
            "timestamp": t.get("timestamp_ms", time.time()), "battery": battery,
            "battery_plugged": t.get("is_charging", False), "memory_usage_mb": memory_usage,
            "memory_avail_mb": mem_avail, "cpu_temp_c": t.get("cpu_temp_c"),
            "accel": [t.get("accel_x", 0.0) or 0.0, t.get("accel_y", 0.0) or 0.0,
                      t.get("accel_z", 0.0) or 0.0],
            "thermal_state": t.get("thermal_state"),
        }

    def _get_battery(self) -> int:
        return 100

    def _get_memory_usage(self) -> int:
        return 0

    def _run_consolidation(self) -> None:
        try:
            self.memory.consolidate_now()
        except Exception as exc:
            logger.error("Consolidation error: %s", exc)

    def sensor_test_cycle(self, steps: int = 5) -> Dict[str, Any]:
        """Bounded OBSERVE->tick cycle; returns a structured report."""
        observations: List[Dict[str, Any]] = []
        for _ in range(steps):
            self.tick()
            observations.append(self._get_observation())
        status = self.get_status()
        status.update({"steps": steps, "observations": observations})
        return status

    def get_status(self) -> Dict[str, Any]:
        return {
            "tick_count": self.tick_count, "device_caps": self.device_caps,
            "engine": (self.engine.__class__.__name__ if self.engine else "None"),
            "tier1_entries": len(getattr(self.memory.tier1, "_events", []) or []),
            "telemetry_received": bool(self.telemetry),
        }

    def cleanup(self) -> None:
        try:
            self._run_consolidation()
        except Exception as exc:
            logger.error("Cleanup error: %s", exc)
        logger.info("Agent cleaned up")


def create_agent(device_caps: Optional[str] = None,
                 api_url: Optional[str] = None) -> AndroidAgent:
    return AndroidAgent(device_caps=device_caps, api_url=api_url)

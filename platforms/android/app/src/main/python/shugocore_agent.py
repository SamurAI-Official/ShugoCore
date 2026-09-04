# Python agent entrypoint for Android (Chaquopy).
#
# Called from Kotlin ShugoCoreService:
#   create_agent(soc) -> AndroidAgent   # build agent (memory + engine)
#   agent.tick()                        # one OBSERVE -> DECIDE -> ACT cycle
#   agent.get_status()                  # UI status dict
#   agent.cleanup()                     # flush + consolidate on shutdown
#
# The decision engine talks to the on-device llama.cpp stack through the
# "android" backend (LocalApiServer on 127.0.0.1:11434, Ollama-compatible).

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shugocore_android")


class AndroidAgent:
    """Android-specific agent wrapper with local inference backend."""

    def __init__(self, device_caps: Optional[str] = None):
        self.device_caps = device_caps or "Unknown"
        self.tick_count = 0

        # Import order matters: android_inference self-registers the
        # "android" backend type with the model_backends factory.
        from model_backends import create_backend  # noqa: F401 (factory check)
        import android_inference  # noqa: F401  (registers "android" backend)
        from decision_engine import DecisionEngine
        from memory_system import MemoryManager

        # Tier 0/1 are per-agent; consolidation runs synchronously from
        # tick() so the agent controls when maintenance work happens
        # (battery-friendly: no background thread spinning on-device).
        self.memory = MemoryManager(
            agent_id=f"android-{self.device_caps}",
            auto_start=False,
        )

        self.engine: Optional[Any] = self._initialize_engine(DecisionEngine)

        logger.info("AndroidAgent initialized (device: %s)", self.device_caps)

    def _initialize_engine(self, engine_cls) -> Optional[Any]:
        """Initialize the decision engine with the local API backend."""
        try:
            return engine_cls(
                models=[{
                    "type": "android",
                    "api_url": "http://127.0.0.1:11434",
                    "model_name": "shugocore-local",
                    "device_caps": {"soc": self.device_caps},
                }],
                vector_db_config={"type": "chroma"},
                memory_db_path=str(Path("semantic_memory.db")),
            )
        except Exception as exc:
            logger.error("Failed to initialize engine: %s", exc)
            return None

    def tick(self) -> None:
        """Execute one agent cycle (OBSERVE -> DECIDE -> ACT)."""
        self.tick_count += 1
        try:
            # Observe: phone telemetry into Tier 1 (append-only, gated writer).
            observation = self._get_observation()
            self.memory.record_event(
                "android_observation",
                payload={"tick": self.tick_count, "observation": observation},
                metadata={"source": "android_shell"},
            )

            # Decide + Act through the engine's single gated path.
            if self.engine is not None:
                context = self.memory.retrieve_context(
                    "maintain_agent_loop", top_k=3)
                self.engine.execute_task({
                    "id": f"android-tick-{self.tick_count}",
                    "type": "maintain_agent_loop",
                    "context": context,
                })
            else:
                logger.debug("Tick %s: no engine (backend unavailable)",
                             self.tick_count)

            # Periodic deterministic consolidation (every 10 ticks).
            if self.tick_count % 10 == 0:
                self._run_consolidation()

        except Exception as exc:
            logger.error("Tick %s error: %s", self.tick_count, exc)

    def _get_observation(self) -> Dict[str, Any]:
        """Collect observations from phone telemetry."""
        return {
            "timestamp": time.time(),
            "battery": self._get_battery(),
            "memory_usage": self._get_memory_usage(),
        }

    def _get_battery(self) -> int:
        """Battery level (stub on-device hook; real value comes from
        ThermalMonitor via the Kotlin side in a future revision)."""
        return 100

    def _get_memory_usage(self) -> int:
        """Process memory usage in MB (stub)."""
        return 0

    def _run_consolidation(self) -> None:
        """Run synchronous memory consolidation (Tier 1 -> Tier 2)."""
        try:
            self.memory.consolidate_now()
        except Exception as exc:
            logger.error("Consolidation error: %s", exc)

    def get_status(self) -> Dict[str, Any]:
        """Get agent status for UI display."""
        return {
            "tick_count": self.tick_count,
            "device_caps": self.device_caps,
            "engine": (self.engine.__class__.__name__
                       if self.engine is not None else "None"),
            "tier1_entries": len(getattr(self.memory.tier1, "_events", [])
                                 or []),
        }

    def cleanup(self) -> None:
        """Clean up resources on shutdown."""
        try:
            self._run_consolidation()
        except Exception as exc:
            logger.error("Cleanup error: %s", exc)
        logger.info("Agent cleaned up")


def create_agent(device_caps: Optional[str] = None) -> AndroidAgent:
    """Factory function called from Kotlin ShugoCoreService."""
    return AndroidAgent(device_caps=device_caps)

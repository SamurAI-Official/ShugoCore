# Python agent entrypoint for Android (Chaquopy)
# This module is executed within the Android app via Chaquopy

import logging
import json
from pathlib import Path
from typing import Optional

# Configure logging for Android
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shugocore_android")

# Import ShugoCore core components
try:
    from agent import Agent
    from memory_system import MemoryManager
    from decision_engine import DecisionEngine
    from safety_policy import SafetyPolicy
    from task_manager import TaskManager
except ImportError:
    # Fallback if running without full ShugoCore installation
    logger.warning("ShugoCore core components not available, using minimal agent")

class AndroidAgent:
    """Android-specific agent wrapper with local inference backend."""
    
    def __init__(self, device_caps: Optional[str] = None):
        self.device_caps = device_caps or "Unknown"
        self.memory = MemoryManager()
        self.engine = None
        self._initialize_engine()
        self.tick_count = 0
        logger.info(f"AndroidAgent initialized with device caps: {self.device_caps}")
    
    def _initialize_engine(self):
        """Initialize the decision engine with local API backend."""
        try:
            from model_backends import ModelBackend
            from android_inference import AndroidBackend
            
            # Try local API server first (llama.cpp via JNI)
            backend = AndroidBackend(
                api_url="http://127.0.0.1:11434",
                model_name=self.device_caps,
                device_caps=self.device_caps
            )
            self.engine = DecisionEngine(backend=backend)
        except Exception as e:
            logger.error(f"Failed to initialize engine: {e}")
            # Fallback to stub backend
            from model_backends import StubBackend
            self.engine = DecisionEngine(backend=StubBackend())
    
    def tick(self):
        """Execute one agent cycle (OBSERVE → DECIDE → ACT)."""
        self.tick_count += 1
        
        try:
            # Observe
            observation = self._get_observation()
            
            # Store in memory
            self.memory.append_tier1_observation(
                source="android",
                data={"tick": self.tick_count, "observation": observation}
            )
            
            # Decide
            if self.engine:
                decision = self.engine.decide(
                    context=self.memory.get_recent_context(5),
                    task="maintain_agent_loop"
                )
            else:
                decision = {"action": "noop"}
            
            # Act (log only on Android)
            logger.debug(f"Tick {self.tick_count}: {decision}")
            
            # Periodic memory consolidation
            if self.tick_count % 10 == 0:
                self._run_consolidation()
                
        except Exception as e:
            logger.error(f"Tick {self.tick_count} error: {e}")
    
    def _get_observation(self):
        """Collect observations from phone sensors."""
        return {
            "timestamp": self.tick_count,
            "battery": self._get_battery(),
            "memory_usage": self._get_memory_usage(),
        }
    
    def _get_battery(self):
        """Get battery level (stub - real impl would use Android APIs)."""
        return 100
    
    def _get_memory_usage(self):
        """Get memory usage (stub)."""
        return 0
    
    def _run_consolidation(self):
        """Run memory consolidation."""
        try:
            self.memory.consolidate_tier1()
        except Exception as e:
            logger.error(f"Consolidation error: {e}")
    
    def get_status(self) -> dict:
        """Get agent status for UI display."""
        return {
            "tick_count": self.tick_count,
            "device_caps": self.device_caps,
            "engine": str(self.engine.__class__.__name__) if self.engine else "None",
            "memory_tiers": {
                "tier1_entries": len(self.memory.get_tier1()),
            }
        }
    
    def cleanup(self):
        """Clean up resources on shutdown."""
        try:
            self._run_consolidation()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        logger.info("Agent cleaned up")


def create_agent(device_caps: Optional[str] = None) -> AndroidAgent:
    """Factory function called from Kotlin ShugoCoreService."""
    return AndroidAgent(device_caps=device_caps)
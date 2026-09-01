"""
ShugoCore Android runtime layer
===============================

Maps the Android app lifecycle onto ShugoCore's governor/fallback machinery
and owns device-state monitoring. This module is the ONLY place Android
app-shell concerns (foreground service, wake locks, secure storage, battery,
thermal) touch the framework.

Kotlin bridge contract (in addition to the ROS 2 contract in
:mod:`android_bridge`):

    isAlive() -> bool
    acquireWakeLock(timeoutMs: int) -> None      # partial wake lock
    acquireMulticastLock() -> None               # required for DDS discovery
    getPowerStatus() -> {"battery_level": int, "plugged": bool}
    getThermalStatus() -> int                    # 0 nominal .. 2 elevated
    getSecureSecret(name: str) -> Optional[str]  # Android Keystore-backed

All bridge calls are exception-guarded: a bridge that lags the contract
degrades gracefully (monitoring disabled), never crashes the runtime.

Lifecycle semantics (deliberately conservative):

- ``on_pause``  -> reports ``android_lifecycle_paused`` (engine pauses, work
  drains). Treated as an operator action because the OS or user caused it.
- ``on_resume`` -> operator-attributed resume via ``FallbackController.resume``.
  A pause caused by a safety trigger survives only if the trigger re-fires;
  the monitor loop re-reports within one interval.
- ``on_destroy`` -> stops monitoring; the app shell shuts the engine down.
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

class SecureStoreSecretProvider:
    """
    Populates a ``SecretResolver``'s override map from the Android Keystore
    via the bridge, at bind time. Environment variables do not exist in the
    Android app sandbox; this is the mobile replacement for the env-var flow.
    """

    def __init__(self, bridge: Any, secret_names: List[str]):
        self._bridge = bridge
        self._secret_names = [str(n)[:64] for n in (secret_names or [])]

    def bind(self, secrets: Any) -> Dict[str, bool]:
        """Inject every resolvable secret; returns per-name success map."""
        results: Dict[str, bool] = {}
        for name in self._secret_names:
            try:
                value = self._bridge.getSecureSecret(name)
            except Exception as exc:
                logger.warning("Secure secret '%s' unavailable: %s", name, exc)
                value = None
            if value:
                secrets.set(name, str(value))
                results[name] = True
            else:
                results[name] = False
        return results


class AndroidRuntime:
    """
    Android app-shell integration: lifecycle hooks, device-state monitoring,
    lock acquisition, and secure secret injection.
    """

    def __init__(self, bridge: Any,
                 fallbacks: Optional[Any] = None,
                 secrets: Optional[Any] = None,
                 acceleration: Optional[Any] = None,
                 secret_names: Optional[List[str]] = None,
                 power_poll_interval: float = 30.0,
                 power_low_threshold: int = 15,
                 thermal_pause_level: int = 2,
                 thermal_pause_streak: int = 2):
        self._bridge = bridge
        self.fallbacks = fallbacks
        self.secrets = secrets
        self.acceleration = acceleration
        self.power_poll_interval = max(5.0, float(power_poll_interval))
        self.power_low_threshold = max(0, min(100, int(power_low_threshold)))
        self.thermal_pause_level = max(0, min(2, int(thermal_pause_level)))
        self.thermal_pause_streak = max(1, int(thermal_pause_streak))
        self.secret_provider = SecureStoreSecretProvider(bridge, secret_names or [])
        self._monitor_stop = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._power_low_reported = False
        self._thermal_streak = 0
        self.state = "new"  # new | started | paused | destroyed

    # -- lifecycle -------------------------------------------------------------
    def on_create(self) -> None:
        """App-shell onCreate: locks, secrets, accelerator detection, monitor."""
        if self.state == "destroyed":
            raise RuntimeError("AndroidRuntime already destroyed")
        for lock_call, label in ((self._acquire_wake_lock, "wake lock"),
                                 (self._acquire_multicast_lock, "multicast lock")):
            try:
                lock_call()
            except Exception as exc:
                logger.warning("Android %s unavailable: %s", label, exc)
        if self.secrets is not None:
            bound = self.secret_provider.bind(self.secrets)
            missing = [name for name, ok in bound.items() if not ok]
            if missing:
                logger.warning("Secrets not resolvable on device: %s", missing)
        if self.acceleration is not None:
            try:
                self.acceleration.detect(bridge=self._bridge)
            except Exception as exc:
                logger.warning("Accelerator detection failed: %s", exc)
        self.state = "started"
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="shugocore-device-monitor", daemon=True)
        self._monitor_thread.start()
        logger.info("AndroidRuntime started")

    def on_pause(self) -> None:
        """App-shell onPause: engine pauses and drains in-flight work."""
        if self.state != "started":
            return
        self.state = "paused"
        if self.fallbacks is not None:
            self.fallbacks.report_violation(
                "android_lifecycle_paused", "app shell paused")
        logger.info("AndroidRuntime paused")

    def on_resume(self) -> None:
        """App-shell onResume: operator-attributed resume."""
        if self.state != "paused":
            return
        self.state = "started"
        if self.fallbacks is not None:
            self.fallbacks.resume(resumed_by="android_lifecycle")
        logger.info("AndroidRuntime resumed")

    def on_destroy(self) -> None:
        """App-shell onDestroy: stop monitoring (engine shutdown is the caller's)."""
        self._monitor_stop.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2.0)
            self._monitor_thread = None
        self.state = "destroyed"
        logger.info("AndroidRuntime destroyed")

    # -- device monitoring -------------------------------------------------------
    def _monitor_loop(self) -> None:
        """Low-frequency battery/thermal polling; degrades to no-op if the
        bridge lacks the contract methods."""
        while not self._monitor_stop.wait(self.power_poll_interval):
            self._check_power()
            self._check_thermal()

    def _check_power(self) -> None:
        status = self._bridge_call("getPowerStatus")
        if not isinstance(status, dict):
            return  # contract method absent or failed - skip
        try:
            battery = int(status.get("battery_level", 100))
            plugged = bool(status.get("plugged", False))
        except (TypeError, ValueError):
            return
        low = battery < self.power_low_threshold and not plugged
        if low and not self._power_low_reported:
            self._power_low_reported = True
            if self.fallbacks is not None:
                self.fallbacks.report_violation(
                    "device_power_low", f"battery at {battery}%")
        elif not low and self._power_low_reported:
            # Recovery: clear so a future drop re-reports.
            self._power_low_reported = False

    def _check_thermal(self) -> None:
        raw = self._bridge_call("getThermalStatus")
        if raw is None:
            return
        try:
            level = max(0, min(2, int(raw)))
        except (TypeError, ValueError):
            return
        if self.acceleration is not None:
            try:
                self.acceleration.apply_thermal_level(level)
            except Exception as exc:
                logger.warning("Thermal demotion failed: %s", exc)
        if level >= self.thermal_pause_level:
            self._thermal_streak += 1
            if self._thermal_streak >= self.thermal_pause_streak:
                self._thermal_streak = 0
                if self.fallbacks is not None:
                    self.fallbacks.report_violation(
                        "device_thermal_high", f"thermal status {level}")
        else:
            self._thermal_streak = 0

    def _bridge_call(self, method: str) -> Any:
        """Invoke an optional bridge contract method; None when unavailable."""
        fn = getattr(self._bridge, method, None)
        if fn is None:
            return None
        try:
            return fn()
        except Exception as exc:
            logger.debug("Bridge call %s failed: %s", method, exc)
            return None

    # -- lock helpers ------------------------------------------------------------
    def _acquire_wake_lock(self) -> None:
        fn = getattr(self._bridge, "acquireWakeLock", None)
        if fn is not None:
            fn(0)  # 0 = hold while the service is alive

    def _acquire_multicast_lock(self) -> None:
        fn = getattr(self._bridge, "acquireMulticastLock", None)
        if fn is not None:
            fn()

    # -- status --------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "power_low_reported": self._power_low_reported,
            "thermal_streak": self._thermal_streak,
            "acceleration": self.acceleration.describe() if self.acceleration else None,
        }



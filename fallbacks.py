"""
ShugoCore deterministic fallback controller
===========================================

Pure rule-based overrides that bypass model logic entirely. When a trigger
fires (stall, budget exhaustion, persistent circuit-breaker trips, memory or
audit failures), the controller latches the governor into a safe mode:

- ``pause``      (default): reject new tasks, let in-flight work drain
- ``safe_state``: continue read-only work; side-effecting actions refused
- ``halt``:      crash-stop; terminal until process restart

Severity is configurable per trigger. Every trigger and action is audited.
No LLM, no network: all decisions here are deterministic rules.
"""

import threading
from collections import defaultdict
from typing import Any, Dict, Optional


class FallbackHalt(RuntimeError):
    """Raised when the HALT escalation fires; unwinds the current execution."""


DEFAULT_SEVERITIES = {
    "circuit_breakers_open": "pause",
    "episodic_backlog": "pause",
    "maintenance_worker_failure": "pause",
    "task_stalled": "safe_state",
    "step_budget_exhausted": "safe_state",
    "task_deadline_exceeded": "safe_state",
    "invariant_violations": "safe_state",
    "memory_failure": "halt",
    "audit_failure": "halt",
    # Robotics triggers
    "collision_detected": "halt",
    "joint_limit_approached": "safe_state",
    "velocity_exceeded": "safe_state",
    "acceleration_exceeded": "safe_state",
    "workspace_boundary_violated": "safe_state",
    "emergency_stop_activated": "halt",
    "human_presence_detected": "safe_state",
    "planning_failure": "pause",
    "ros2_connection_lost": "safe_state",
    "gazebo_connection_lost": "pause",
}
# Violations that escalate on first occurrence.
_IMMEDIATE = {"step_budget_exhausted", "task_deadline_exceeded",
              "memory_failure", "audit_failure"}
# Violations that require repeated occurrences before escalation.
_THRESHOLDED = {"invariant_violations", "maintenance_worker_failure"}
_DEFAULT_THRESHOLDS = {
    "breakers_open": 2,         # hosts with open circuits
    "episodic_backlog": 200,    # unconsumed Tier 1 events
    "invariant_violations": 10,
    "maintenance_failures": 3,  # consecutive worker failures
    "stall_seconds": 120,       # no loop progress
}


class FallbackController:
    """Rule-based safety overrides with configurable escalation severity."""

    def __init__(self, governor: Any, memory: Optional[Any] = None,
                 execution_layer: Optional[Any] = None,
                 audit: Optional[Any] = None,
                 thresholds: Optional[Dict[str, Any]] = None,
                 severities: Optional[Dict[str, str]] = None):
        self.governor = governor
        self.memory = memory
        self.execution_layer = execution_layer
        self.audit = audit
        self.thresholds = {**_DEFAULT_THRESHOLDS, **(thresholds or {})}
        self.severities = {**DEFAULT_SEVERITIES, **(severities or {})}
        self.mode = "normal"  # normal | paused | safe_state | halted
        self._violations: Dict[str, int] = defaultdict(int)
        self._last_detail: Dict[str, str] = {}
        self._lock = threading.Lock()

    def attach(self, memory: Optional[Any] = None,
               execution_layer: Optional[Any] = None) -> None:
        """Wire collaborators created after the controller (avoids cycles)."""
        if memory is not None:
            self.memory = memory
        if execution_layer is not None:
            self.execution_layer = execution_layer

    # -- proactive check (cheap, called at the top of every loop pass) --------

    def evaluate(self) -> Dict[str, Any]:
        """Deterministic trigger check; call at the top of every loop pass."""
        if self.mode != "normal":
            return self.status()
        triggers = []
        breakers = (self.execution_layer.breakers_open_count()
                    if self.execution_layer is not None else 0)
        if breakers >= self.thresholds["breakers_open"]:
            triggers.append(("circuit_breakers_open",
                             f"{breakers} open circuit(s)"))
        if self.memory is not None:
            backlog = len(self.memory.tier1)
            if backlog >= self.thresholds["episodic_backlog"]:
                triggers.append(("episodic_backlog",
                                 f"{backlog} unconsumed Tier 1 events"))
        stalled = self.governor.seconds_since_progress()
        if (stalled is not None
                and stalled > float(self.thresholds["stall_seconds"])):
            triggers.append(("task_stalled", f"no progress for {stalled:.1f}s"))
        for kind, detail in triggers:
            self._fire(kind, detail)  # may raise FallbackHalt on 'halt'
        return self.status()


    # -- reactive reports (from the loop / memory worker) -----------------------

    def report_violation(self, kind: str, detail: str = "") -> None:
        """Record a violation; escalates when its severity rule fires."""
        with self._lock:
            self._violations[kind] += 1
            self._last_detail[kind] = str(detail)[:200]
            count = self._violations[kind]
        if kind in _THRESHOLDED:
            threshold = self.thresholds.get(
                {"invariant_violations": "invariant_violations",
                 "maintenance_worker_failure": "maintenance_failures"}[kind],
                0)
            if count >= threshold:
                self._fire(kind, detail)
        elif kind in _IMMEDIATE:
            self._fire(kind, detail)
        else:
            # Any other configured trigger fires on first report.
            self._fire(kind, detail)

    def _fire(self, kind: str, detail: str = "") -> None:
        severity = str(self.severities.get(kind, "pause")).lower()
        reason = f"{kind}: {str(detail)[:150]}"
        self._audit("fallback_trigger",
                    {"trigger": kind, "severity": severity,
                     "detail": str(detail)[:200]})
        if severity == "halt":
            self.mode = "halted"
            self.governor.halt(reason)
            self._audit("fallback_halt", {"reason": reason})
            raise FallbackHalt(reason)
        if severity == "safe_state":
            self.mode = "safe_state"
            self.governor.safe_state(reason)
        else:
            self.mode = "paused"
            self.governor.pause(reason)

    # -- operator surface ---------------------------------------------------------

    def resume(self, resumed_by: str = "") -> None:
        """Operator resume (attribution required). HALT is terminal."""
        self.governor.resume(resumed_by=resumed_by)
        with self._lock:
            self._violations.clear()
            self._last_detail.clear()
        self.mode = "normal"
        self._audit("fallback_resume", {"resumed_by": str(resumed_by)[:120]})

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {"mode": self.mode,
                    "governor_state": self.governor.state.value,
                    "violations": dict(self._violations)}

    def _audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event_type, payload)
        except Exception:
            pass


"""
ShugoCore execution state machine
=================================

Strict interlocks for the observation-action loop:

- explicit states with an allowed-transition matrix, enforced atomically
- re-entrancy protection (a tool handler re-entering ``execute_task`` is
  refused -> circular deadlocks are structurally impossible)
- per-task step budgets (model calls + tool dispatches)
- wall-clock task deadlines

Deterministic by construction: no model calls, no network - pure state
checks. The fallback controller (``fallbacks.py``) latches the governor
into PAUSED / SAFE_STATE / HALTED when rule-based triggers fire.
"""

import threading
import time
from enum import Enum
from typing import Any, Dict, Optional


class GovernorError(RuntimeError):
    """Raised when a state transition, budget or deadline rule is violated."""


class AgentState(Enum):
    IDLE = "idle"
    OBSERVING = "observing"
    GATING = "gating"
    DECIDING = "deciding"
    EXECUTING = "executing"
    EVALUATING = "evaluating"
    CONSOLIDATING = "consolidating"
    PAUSED = "paused"
    SAFE_STATE = "safe_state"
    HALTED = "halted"


# The allowed-transition matrix. Anything not listed here is forbidden.
_ALLOWED = {
    AgentState.IDLE: frozenset({AgentState.OBSERVING}),
    AgentState.OBSERVING: frozenset({AgentState.GATING, AgentState.IDLE}),
    AgentState.GATING: frozenset({AgentState.DECIDING, AgentState.IDLE}),
    AgentState.DECIDING: frozenset({AgentState.EXECUTING, AgentState.IDLE}),
    AgentState.EXECUTING: frozenset({AgentState.EVALUATING, AgentState.IDLE}),
    AgentState.EVALUATING: frozenset({AgentState.IDLE}),
    AgentState.CONSOLIDATING: frozenset({AgentState.IDLE}),
    AgentState.SAFE_STATE: frozenset({AgentState.OBSERVING}),
    AgentState.PAUSED: frozenset(),
    AgentState.HALTED: frozenset(),
}


class ExecutionGovernor:
    """
    Thread-safe governor for the agent loop.

    Latched modes are set by the fallback controller. While latched:
    - ``PAUSED``: no task may begin.
    - ``SAFE_STATE``: tasks may run, but the decision engine refuses all
      side-effecting actions.
    - ``HALTED``: nothing may run; terminal until process restart.
    When a task finishes while latched, the governor returns to the latched
    state instead of ``IDLE``.
    """

    def __init__(self, step_budget: int = 50, task_deadline_seconds: float = 120.0,
                 stall_seconds: float = 120.0, audit: Optional[Any] = None):
        self.step_budget = max(1, int(step_budget))
        self.task_deadline_seconds = max(0.0, float(task_deadline_seconds))
        self.stall_seconds = max(0.0, float(stall_seconds))
        self._audit = audit
        self._lock = threading.RLock()
        self._state = AgentState.IDLE
        self._latched: Optional[AgentState] = None
        self._task_started_at: Optional[float] = None
        self._steps_used = 0
        self._last_progress_at: Optional[float] = None
        self._current_task: Optional[str] = None

    # -- state access -----------------------------------------------------------

    @property
    def state(self) -> AgentState:
        with self._lock:
            return self._state

    @property
    def mode(self) -> AgentState:
        """
        The effective governor mode: a latched safe-mode (PAUSED / SAFE_STATE
        / HALTED) takes precedence over the transient per-task state.
        """
        with self._lock:
            return self._latched if self._latched is not None else self._state

    @property
    def current_task(self) -> Optional[str]:
        with self._lock:
            return self._current_task

    def seconds_since_progress(self) -> Optional[float]:
        with self._lock:
            if self._last_progress_at is None:
                return None
            return time.monotonic() - self._last_progress_at


    # -- task lifecycle -----------------------------------------------------------

    def begin_task(self, task_ref: Any = None) -> None:
        """Enter OBSERVING; refuses re-entrancy and latched modes."""
        with self._lock:
            if self._state is AgentState.PAUSED:
                raise GovernorError("execution is paused by the fallback controller")
            if self._state is AgentState.HALTED:
                raise GovernorError("execution is halted; process restart required")
            if self._state is not AgentState.IDLE and self._state is not AgentState.SAFE_STATE:
                raise GovernorError(
                    f"re-entrant execution blocked (state={self._state.value})")
            self._state = AgentState.OBSERVING
            self._task_started_at = time.monotonic()
            self._steps_used = 0
            self._last_progress_at = self._task_started_at
            self._current_task = str(task_ref) if task_ref is not None else None

    def step(self, next_state: AgentState) -> None:
        """Advance to the next loop state (matrix-checked, deadline-checked)."""
        self._check_deadline()
        with self._lock:
            if next_state not in _ALLOWED.get(self._state, frozenset()):
                raise GovernorError(
                    f"illegal transition {self._state.value} -> {next_state.value}")
            self._state = next_state
            self._last_progress_at = time.monotonic()

    def consume_step(self, count: int = 1) -> None:
        """Charge model calls / tool dispatches against the per-task budget."""
        self._check_deadline()
        with self._lock:
            if self._task_started_at is None:
                return  # no active task (standalone API use)
            self._steps_used += max(1, int(count))
            if self._steps_used > self.step_budget:
                raise GovernorError(
                    f"step budget exhausted ({self._steps_used} > {self.step_budget})")

    def end_task(self) -> None:
        """Close the task; returns to the latched state if one is active."""
        with self._lock:
            self._state = (self._latched if self._latched is not None
                           else AgentState.IDLE)
            self._task_started_at = None
            self._steps_used = 0
            self._current_task = None
            self._last_progress_at = time.monotonic()

    # -- latched modes (driven by the fallback controller) ----------------------

    def pause(self, reason: str = "") -> None:
        self._latch(AgentState.PAUSED, reason)

    def safe_state(self, reason: str = "") -> None:
        self._latch(AgentState.SAFE_STATE, reason)

    def halt(self, reason: str = "") -> None:
        self._latch(AgentState.HALTED, reason)

    def resume(self, resumed_by: str = "") -> None:
        """Operator resume from PAUSED / SAFE_STATE. HALT is terminal."""
        if not resumed_by:
            raise GovernorError("resume requires operator attribution ('resumed_by')")
        with self._lock:
            if self._state not in (AgentState.PAUSED, AgentState.SAFE_STATE):
                raise GovernorError(f"cannot resume from state={self._state.value}")
            self._latched = None
            self._state = AgentState.IDLE
            self._last_progress_at = time.monotonic()
        self._audit_append("governor_resume", {"resumed_by": str(resumed_by)[:120]})

    # -- internals --------------------------------------------------------------

    def _check_deadline(self) -> None:
        with self._lock:
            if (self._task_started_at is not None and self.task_deadline_seconds
                    and time.monotonic() - self._task_started_at > self.task_deadline_seconds):
                raise GovernorError("task deadline exceeded")

    def _latch(self, state: AgentState, reason: str) -> None:
        with self._lock:
            if self._state is AgentState.HALTED:
                return
            self._latched = state
            if self._task_started_at is None:
                self._state = state  # idle: latch immediately
            # else: an in-flight task finishes first, then end_task latches.
        self._audit_append("governor_latch",
                           {"state": state.value, "reason": str(reason)[:160]})

    def _audit_append(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self._audit is None:
            return
        try:
            self._audit.append(event_type, payload)
        except Exception:
            pass


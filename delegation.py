"""
ShugoCore Network Delegation Manager (v1.21)
=============================================

Routes inference tasks to the best available backend based on scope
(localhost / LAN / Internet), task tier, health, and consent policy.

Provider-side contract: the agent shell calls ``select_backend()`` before
the engine task; the engine reads the chosen scope from the task context.

No hard imports from the decision core.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)


class BackendScope(str, enum.Enum):
    """Network scope for a backend endpoint."""

    LOCALHOST = "localhost"
    LAN = "lan"
    INTERNET = "internet"


class TaskTier(str, enum.Enum):
    """Computational tier for routing decisions."""

    LIGHT = "light"        # sensor polling, attention, memory — always local
    STANDARD = "standard"  # speak, ask_user, conversation — local → LAN
    HEAVY = "heavy"        # multi-step, complex reasoning — LAN → internet
    SHARED = "shared"      # any task when no tier-specific backend exists


# Default action-type → tier mapping.  Individual registrations can override.
_DEFAULT_TIER_MAP: Dict[str, TaskTier] = {
    # LIGHT — cheap, always-local
    "record_observation": TaskTier.LIGHT,
    "probe_model": TaskTier.LIGHT,
    "consolidate": TaskTier.LIGHT,
    # STANDARD — conversational, best on local or LAN
    "speak": TaskTier.STANDARD,
    "ask_user": TaskTier.STANDARD,
    "speak_test": TaskTier.STANDARD,
    # HEAVY — complex reasoning benefits from bigger remote models
    "multi_step_process": TaskTier.HEAVY,
    "search_api": TaskTier.HEAVY,
    "news_api": TaskTier.HEAVY,
    "hardware_interaction": TaskTier.HEAVY,
    # SHARED — anything else
}

_DEFAULT_TIER_FALLBACK = TaskTier.SHARED

# How many consecutive errors before a backend is marked unhealthy.
_MAX_CONSECUTIVE_ERRORS = 3
# Cooldown (seconds) before an unhealthy backend is re-tried.
_UNHEALTHY_COOLDOWN_S = 30.0


class BackendEntry:
    """A registered backend endpoint with health tracking."""

    __slots__ = (
        "scope", "base_url", "backend_type", "task_tiers",
        "latency_ms", "consecutive_errors", "last_error_ts", "_lock",
    )

    def __init__(
        self,
        scope: BackendScope,
        base_url: str,
        backend_type: str = "ollama",
        task_tiers: Optional[FrozenSet[TaskTier]] = None,
    ) -> None:
        self.scope = scope
        self.base_url = str(base_url).rstrip("/")
        self.backend_type = str(backend_type)
        self.task_tiers: FrozenSet[TaskTier] = (
            task_tiers if task_tiers is not None else frozenset()
        )
        self.latency_ms: float = 0.0
        self.consecutive_errors: int = 0
        self.last_error_ts: float = 0.0
        self._lock = threading.Lock()

    def record_success(self, latency_ms: float) -> None:
        """Record a successful inference call."""
        with self._lock:
            self.latency_ms = latency_ms
            self.consecutive_errors = 0

    def record_error(self) -> None:
        """Record a failed inference call."""
        with self._lock:
            self.consecutive_errors += 1
            self.last_error_ts = time.time()

    @property
    def healthy(self) -> bool:
        """True if the backend is accepting traffic."""
        with self._lock:
            if self.consecutive_errors == 0:
                return True
            if self.consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                return (time.time() - self.last_error_ts) >= _UNHEALTHY_COOLDOWN_S
            return True

    def serves_tier(self, tier: TaskTier) -> bool:
        """True if this backend is registered for *tier*."""
        if not self.task_tiers:
            return True
        return tier in self.task_tiers

    def to_dict(self) -> Dict[str, Any]:
        """Snapshot for status serialization."""
        return {
            "scope": self.scope.value,
            "base_url": self.base_url,
            "backend_type": self.backend_type,
            "latency_ms": round(self.latency_ms, 1),
            "consecutive_errors": self.consecutive_errors,
            "healthy": self.healthy,
            "tiers": sorted(t.value for t in self.task_tiers),
        }


def _tier_for_action_type(action_type: Optional[str]) -> TaskTier:
    """Map an action type string to its computational tier."""
    if not action_type:
        return _DEFAULT_TIER_FALLBACK
    return _DEFAULT_TIER_MAP.get(action_type, _DEFAULT_TIER_FALLBACK)


class DelegationManager:
    """Registry of backend endpoints + routing by scope, tier, and policy.

    Thread-safe.  Designed to be held by the agent shell and consulted
    before every engine call.
    """

    def __init__(self) -> None:
        self._backends: List[BackendEntry] = []
        self._lock = threading.Lock()
        self._active_scope: Optional[BackendScope] = None
        self._active_url: Optional[str] = None

    # -- registration ---------------------------------------------------------

    def register(
        self,
        scope: BackendScope,
        base_url: str,
        backend_type: str = "ollama",
        task_tiers: Optional[FrozenSet[TaskTier]] = None,
    ) -> None:
        """Register or update a backend endpoint.

        If a backend with the same *scope* and *base_url* already exists,
        its metadata is refreshed (health is preserved on scope/url match).
        """
        entry = BackendEntry(scope, base_url, backend_type, task_tiers)
        with self._lock:
            for i, existing in enumerate(self._backends):
                if existing.scope == scope and existing.base_url == entry.base_url:
                    entry.consecutive_errors = existing.consecutive_errors
                    entry.last_error_ts = existing.last_error_ts
                    entry.latency_ms = existing.latency_ms
                    self._backends[i] = entry
                    return
            self._backends.append(entry)

    def unregister(self, scope: BackendScope, base_url: str) -> bool:
        """Remove a backend.  Returns True if it was found."""
        with self._lock:
            for i, entry in enumerate(self._backends):
                if entry.scope == scope and entry.base_url == base_url:
                    self._backends.pop(i)
                    if self._active_scope == scope and self._active_url == base_url:
                        self._active_scope = None
                        self._active_url = None
                    return True
        return False

    # -- routing --------------------------------------------------------------

    def _candidates_for_tier(self, tier: TaskTier) -> List[BackendEntry]:
        """Return backends sorted by scope priority for *tier*."""
        if tier == TaskTier.LIGHT:
            order = [BackendScope.LOCALHOST]
        elif tier == TaskTier.STANDARD:
            order = [BackendScope.LOCALHOST, BackendScope.LAN]
        elif tier == TaskTier.HEAVY:
            order = [BackendScope.LAN, BackendScope.INTERNET]
        else:
            order = [BackendScope.LOCALHOST, BackendScope.LAN, BackendScope.INTERNET]

        ordered: List[BackendEntry] = []
        seen: set = set()
        for scope in order:
            for entry in self._backends:
                if entry.scope == scope and id(entry) not in seen:
                    ordered.append(entry)
                    seen.add(id(entry))
        for entry in self._backends:
            if id(entry) not in seen:
                ordered.append(entry)
                seen.add(id(entry))
        return ordered

    # -- routing --------------------------------------------------------------

    def select_backend(
        self,
        action_type: Optional[str],
        network_policy: Dict[str, bool],
        consent_has_delegate_internet: bool = False,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Choose the best backend for *action_type* given the current policy.

        Returns ``(base_url, scope_value)`` or ``(None, None)`` when no
        backend is available (the caller should use the default local URL).

        Routing priority follows the task tier's preferred scope chain,
        then falls through to lower scopes if the preferred one is
        unhealthy or policy-blocked.
        """
        tier = _tier_for_action_type(action_type)

        with self._lock:
            candidates = self._candidates_for_tier(tier)

            for entry in candidates:
                if not self._policy_allows(entry.scope, network_policy,
                                           consent_has_delegate_internet):
                    continue
                if not entry.serves_tier(tier):
                    continue
                if not entry.healthy:
                    continue
                self._active_scope = entry.scope
                self._active_url = entry.base_url
                return entry.base_url, entry.scope.value

        # No healthy, policy-allowed backend found.  Fall back to localhost.
        for entry in self._backends:
            if entry.scope == BackendScope.LOCALHOST and entry.healthy:
                self._active_scope = entry.scope
                self._active_url = entry.base_url
                return entry.base_url, entry.scope.value

        return None, None

    @staticmethod
    def _policy_allows(
        scope: BackendScope,
        network_policy: Dict[str, bool],
        consent_has_delegate_internet: bool,
    ) -> bool:
        """Check network policy + consent for *scope*."""
        if scope == BackendScope.LOCALHOST:
            return bool(network_policy.get("localhost", True))
        if scope == BackendScope.LAN:
            return bool(network_policy.get("lan", True))
        if scope == BackendScope.INTERNET:
            return bool(network_policy.get("internet", False)) and consent_has_delegate_internet
        return False

    # -- health feedback ------------------------------------------------------

    def record_success(self, scope: BackendScope, base_url: str,
                       latency_ms: float) -> None:
        """Feed back a successful call for health tracking."""
        with self._lock:
            for entry in self._backends:
                if entry.scope == scope and entry.base_url == base_url:
                    entry.record_success(latency_ms)
                    return

    def record_error(self, scope: BackendScope, base_url: str) -> None:
        """Feed back a failed call for health tracking."""
        with self._lock:
            for entry in self._backends:
                if entry.scope == scope and entry.base_url == base_url:
                    entry.record_error()
                    return

    # -- introspection --------------------------------------------------------

    @property
    def active_scope(self) -> Optional[str]:
        """The scope value used for the most recent ``select_backend`` call."""
        return self._active_scope.value if self._active_scope else None

    @property
    def active_url(self) -> Optional[str]:
        return self._active_url

    def status(self) -> Dict[str, Any]:
        """Snapshot for ``get_status()``."""
        with self._lock:
            backends = [entry.to_dict() for entry in self._backends]
        return {
            "active_scope": self.active_scope,
            "active_url": self.active_url,
            "backends": backends,
        }
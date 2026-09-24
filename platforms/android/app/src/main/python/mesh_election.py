"""ShugoCore mesh primary election (Track 1).

Single-writer election over heartbeat advertisements: exactly one node
holds the primary lease and runs the agent loop side-effects; every
other live node falls back to peripheral mode (sensors + journal +
RPC offload server, never speaks).

Rule (deterministic, thermal-aware): paired + fresh heartbeat +
thermal < 3 + headroom > 0 are candidates; lowest priority wins, tie
breaks on smallest node_id. Lease 10s, timeout 30s. Partitioned nodes
fail closed to standalone; re-merge is a fresh election.

Also owns the split-layer command-line builder used by
:meth:`MeshCoordinator.rpc_split_spec`.
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from mesh_rpc import THERMAL_REFUSE_STATUS, default_chunk
except Exception:
    THERMAL_REFUSE_STATUS = 3
    try:
        from mesh_rpc import default_chunk
    except Exception:
        def default_chunk(machine, exe):
            return 24

LEASE_S = 10.0
HEARTBEAT_TIMEOUT_S = 30.0


def _now() -> float:
    return time.monotonic()


class MeshElection:
    """Deterministic primary election over heartbeat advertisements."""

    def __init__(self, node_id: str, priority: int = 100,
                 lease_s: float = LEASE_S,
                 heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S,
                 audit: Optional[Any] = None):
        self.node_id = str(node_id or "").strip() or "node-unknown"
        self.priority = int(priority)
        self.lease_s = max(1.0, float(lease_s))
        self.heartbeat_timeout_s = max(1.0, float(heartbeat_timeout_s))
        self.audit = audit
        self._lock = threading.Lock()
        self._nodes: Dict[str, Dict[str, Any]] = {}
        self._primary_id: Optional[str] = None
        self._lease_since: float = 0.0
        self._seq = 0

    def local_heartbeat(self, thermal_status: int = 0,
                        mem_available_bytes: int = 0,
                        rpc_endpoint: str = "",
                        paired: bool = True) -> Dict[str, Any]:
        self._seq += 1
        payload = {
            "node_id": self.node_id,
            "priority": self.priority,
            "thermal_status": int(thermal_status or 0),
            "mem_available_bytes": int(mem_available_bytes or 0),
            "rpc_endpoint": str(rpc_endpoint or ""),
            "paired": bool(paired),
            "seq": self._seq,
        }
        self.observe_heartbeat(payload)
        return dict(payload)

    def observe_heartbeat(self, payload, now=None):
        """Record one heartbeat advertisement. `now` is injectable (tests,
        replay); defaults to the monotonic clock."""
        if not isinstance(payload, dict):
            return False
        node_id = str(payload.get("node_id", "")).strip()
        if not node_id:
            return False
        try:
            priority = int(payload.get("priority", 100))
        except (TypeError, ValueError):
            priority = 100
        try:
            thermal = int(payload.get("thermal_status", 0))
        except (TypeError, ValueError):
            thermal = 0
        try:
            mem = int(payload.get("mem_available_bytes", 0))
        except (TypeError, ValueError):
            mem = 0
        ts = _now() if now is None else float(now)
        entry = {
            "node_id": node_id, "priority": priority,
            "thermal_status": thermal, "mem_available_bytes": mem,
            "rpc_endpoint": str(payload.get("rpc_endpoint", "") or "")[:256],
            "paired": bool(payload.get("paired", True)),
            "seq": payload.get("seq", 0), "last_seen": ts,
        }
        with self._lock:
            prev = self._nodes.get(node_id)
            if prev is not None:
                try:
                    if int(entry["seq"]) <= int(prev.get("seq", 0)):
                        return True
                except (TypeError, ValueError):
                    pass
            self._nodes[node_id] = entry
        return True

    def drop_node(self, node_id):
        with self._lock:
            self._nodes.pop(str(node_id), None)

    @staticmethod
    def _eligible(entry):
        if not entry.get("paired", True):
            return "unpaired"
        try:
            if int(entry.get("thermal_status", 0)) >= THERMAL_REFUSE_STATUS:
                return "thermal-critical"
        except (TypeError, ValueError):
            return "thermal-unknown"
        try:
            if int(entry.get("mem_available_bytes", 0)) <= 0:
                return "no-headroom"
        except (TypeError, ValueError):
            return "no-headroom"
        return None

    def _eligible_reasons(self) -> dict:
        """Return a mapping of node_id -> ineligibility reason for all
        known nodes. Used by tests to assert why a peer was excluded.
        """
        with self._lock:
            nodes = {nid: dict(entry) for nid, entry in self._nodes.items()}
        reasons: dict = {}
        for nid, entry in nodes.items():
            reason = self._eligible(entry)
            if reason is not None:
                reasons[nid] = reason
        return reasons

    def _live_candidates(self, now):
        with self._lock:
            nodes = list(self._nodes.values())
        out = []
        for entry in nodes:
            try:
                age = now - float(entry.get("last_seen", 0.0))
            except (TypeError, ValueError):
                continue
            if age > self.heartbeat_timeout_s:
                continue
            if self._eligible(entry) is not None:
                continue
            out.append(entry)
        out.sort(key=lambda e: (int(e.get("priority", 100)),
                                str(e.get("node_id", ""))))
        return out

    def tick(self, now=None):
        ts = _now() if now is None else float(now)
        candidates = self._live_candidates(ts)
        winner = candidates[0]["node_id"] if candidates else None
        with self._lock:
            previous = self._primary_id
            if winner != previous:
                self._primary_id = winner
                self._lease_since = ts
                if winner == self.node_id:
                    self._audit("mesh_primary_elected",
                                {"node_id": self.node_id,
                                 "candidates": len(candidates)})
                elif previous == self.node_id:
                    self._audit("mesh_follower_fallback",
                                {"node_id": self.node_id,
                                 "primary": winner or ""})
        return {"primary": winner, "is_primary": winner == self.node_id,
                "candidates": [e["node_id"] for e in candidates],
                "lease_since": self._lease_since, "lease_s": self.lease_s}

    def primary(self):
        with self._lock:
            return self._primary_id

    def is_primary(self):
        with self._lock:
            return self._primary_id == self.node_id

    def role(self):
        with self._lock:
            primary = self._primary_id
        if primary is None:
            return "unknown"
        return "primary" if primary == self.node_id else "follower"

    def live_peers(self, now=None):
        ts = _now() if now is None else float(now)
        with self._lock:
            nodes = list(self._nodes.values())
        out = []
        for entry in nodes:
            if str(entry.get("node_id")) == self.node_id:
                continue
            try:
                age = ts - float(entry.get("last_seen", 0.0))
            except (TypeError, ValueError):
                continue
            if age <= self.heartbeat_timeout_s:
                out.append({k: v for k, v in entry.items()
                            if k != "last_seen"})
        return out

    def status(self):
        with self._lock:
            nodes = {k: {kk: vv for kk, vv in v.items()}
                     for k, v in self._nodes.items()}
            primary = self._primary_id
        ineligible = {}
        for node_id, entry in nodes.items():
            reason = self._eligible(entry)
            if reason is not None:
                ineligible[node_id] = reason
        return {"node_id": self.node_id, "priority": self.priority,
                "primary": primary, "is_primary": primary == self.node_id,
                "role": self.role(), "nodes": nodes,
                "ineligible": ineligible, "lease_s": self.lease_s,
                "heartbeat_timeout_s": self.heartbeat_timeout_s}

    def _audit(self, event_type, payload):
        if self.audit is None:
            return
        try:
            self.audit.append(event_type, payload)
        except Exception as exc:
            logger.warning("audit append failed for '%s': %s",
                           event_type, exc)



def _make_split_command(
    machine, exe, host, port, layers, threads,
    chunk_size=None, check_interval_s=None,
    output=None, temp_dir=None,
):
    """Build the per-machine ggml-rpc-server command line up to execution.

    argv-only: returns (argv, env_overrides, log_dir, pidfile).  The
    caller maps those onto the host transport (ADB, subprocess, etc.).
    """
    base = [exe, "--model", machine,
            "--port", str(port),
            "--threads", str(threads),
            "--ctx-size", "0",
            "--n-gpu-layers", "0"]
    if chunk_size:
        base += ["--split", str(chunk_size)]
    else:
        base += ["--split", str(default_chunk(machine, exe))]
    if check_interval_s is not None:
        base += ["--grpc-timeout",
                 str(int(check_interval_s * 1000))]
    log_dir = None
    pidfile = None
    if output:
        log_dir = str(output)
    if temp_dir:
        pidfile = str(temp_dir)
    return (base, {}, log_dir, pidfile)

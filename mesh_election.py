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
import os
import subprocess
import sys
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

def parse_vm_stat(text, page_size=16384) -> int:
    """Available bytes from macOS ``vm_stat`` output.

    "Available" approximates what the kernel would hand out: free, inactive and
    speculative pages. ``os.sysconf("SC_AVPHYS_PAGES")`` is not defined on macOS,
    so without this a Mac node advertised no headroom -- and a node advertising
    none is ineligible in the election and skipped by the layer planner: alive,
    and contributing nothing.
    """
    counted = 0
    for line in (text or "").splitlines():
        name, sep, rest = line.partition(":")
        if not sep or name.strip() not in ("Pages free", "Pages inactive",
                                           "Pages speculative"):
            continue
        digits = "".join(ch for ch in rest if ch.isdigit())
        if digits:
            counted += int(digits)
    try:
        return max(0, counted) * max(0, int(page_size))
    except (TypeError, ValueError):
        return 0


def macos_available_memory(run=None) -> int:
    """Free physical memory on macOS (0 when it cannot be measured)."""
    runner = run or subprocess.run
    try:
        proc = runner(["vm_stat"], capture_output=True, text=True, timeout=10)
        text = getattr(proc, "stdout", "") or ""
    except Exception:
        return 0
    page_size = 16384                    # Apple silicon default, refined below
    try:
        proc = runner(["sysctl", "-n", "hw.pagesize"], capture_output=True,
                      text=True, timeout=10)
        reported = int((getattr(proc, "stdout", "") or "").strip())
        if reported > 0:
            page_size = reported
    except Exception:
        pass
    return parse_vm_stat(text, page_size)


def available_memory_bytes() -> int:
    """Best-effort free physical memory in bytes (0 when unmeasurable).

    The election refuses a candidate that reports no headroom, so a node which
    cannot measure memory must not advertise zero: on a desktop host that marks
    every host ineligible and would hand the primary lease to a phone by
    accident. POSIX and Android answer via ``sysconf``; macOS via ``vm_stat``
    (it does not define ``SC_AVPHYS_PAGES``); Windows via
    ``GlobalMemoryStatusEx``.
    """
    try:
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        size = int(os.sysconf("SC_PAGE_SIZE"))
        if pages > 0 and size > 0:
            return pages * size
    except (AttributeError, ValueError, OSError, TypeError):
        pass
    if sys.platform == "darwin":
        # macOS defines neither name above through os.sysconf, which is why a Mac
        # node used to advertise no headroom at all.
        available = macos_available_memory()
        if available > 0:
            return available
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys)
    except Exception:
        pass
    return 0


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
        # (peer, its claim, our winner) already reported, so a disagreement is named
        # once instead of on every heartbeat.
        self._disagreement_seen: set = set()

    def local_heartbeat(self, thermal_status: int = 0,
                        mem_available_bytes: int = 0,
                        rpc_endpoint: str = "",
                        paired: bool = True) -> Dict[str, Any]:
        self._seq += 1
        with self._lock:
            claim = self._primary_id or ""
        payload = {
            "node_id": self.node_id,
            "priority": self.priority,
            "thermal_status": int(thermal_status or 0),
            "mem_available_bytes": int(mem_available_bytes or 0),
            "rpc_endpoint": str(rpc_endpoint or ""),
            "paired": bool(paired),
            # Who this node believes holds the lease. Without it a fleet can disagree
            # about the lease and look healthy from every node at once: each answers
            # for itself, and only the *refused* sender ever learns otherwise.
            "primary": claim,
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
        if "mem_available_bytes" in payload:
            try:
                mem = int(payload["mem_available_bytes"])
            except (TypeError, ValueError):
                mem = 0            # reported but unusable: fail closed
        else:
            mem = None             # not reported: unknown, so still a candidate
        ts = _now() if now is None else float(now)
        entry = {
            "node_id": node_id, "priority": priority,
            "thermal_status": thermal, "mem_available_bytes": mem,
            "rpc_endpoint": str(payload.get("rpc_endpoint", "") or "")[:256],
            "paired": bool(payload.get("paired", True)),
            "primary": str(payload.get("primary", "") or "")[:64],
            "seq": payload.get("seq", 0), "last_seen": ts,
        }
        with self._lock:
            prev = self._nodes.get(node_id)
            if prev is not None:
                try:
                    seq = int(entry["seq"])
                    prev_seq = int(prev.get("seq", 0))
                except (TypeError, ValueError):
                    seq = prev_seq = None
                if seq is not None and prev_seq is not None:
                    if seq == prev_seq:
                        # The same advertisement again (a duplicated frame): the
                        # lease is what matters, so renew it without rewriting the
                        # record. Returning early *without* touching last_seen --
                        # the old behaviour -- let a live peer's lease lapse.
                        prev["last_seen"] = ts
                        return True
                    if seq < prev_seq:
                        # The peer restarted, so its counter began again. The old
                        # rule dropped every such advertisement while reporting it
                        # as observed, leaving a node that remembered a high
                        # sequence blind to that peer *forever* -- the peer
                        # advertised every 10 s and stayed invisible until the
                        # observer was restarted too. A rollback on one TCP stream
                        # is a restart, not reordering.
                        logger.info(
                            "mesh election: peer %s restarted (seq %s -> %s); "
                            "renewing its lease", node_id, prev_seq, seq)
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
        mem = entry.get("mem_available_bytes")
        if mem is None:
            # Unreported headroom is *unknown*, not zero: a node that cannot
            # measure its free memory stays a candidate instead of advertising
            # itself out of the election (which is how a Mac went invisible).
            return None
        try:
            if int(mem) <= 0:
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

    @staticmethod
    def _rank(entry) -> tuple:
        """The full ranking key: priority first, then node_id.

        Comparing priority *alone* makes the outcome depend on arrival order. Two nodes
        at the same priority are then mutually non-deposable, so whichever one won first
        keeps the lease for ever and a live fleet settles into camps that never agree on
        who leads -- observed on this bench: the desktop and the Mac both advertising
        priority 10, with the phones and the Mac holding to `shugo-mac` while the
        desktop held to itself, so every delegated action was refused by the very peers
        the desktop was asking. Rank is a total order (node_id is unique), so the winner
        is unique and fixed and the fleet converges on it.
        """
        try:
            priority = int(entry.get("priority", 100))
        except (TypeError, ValueError):
            priority = 100
        return (priority, str(entry.get("node_id", "")))

    def _note_disagreement(self, winner, candidates) -> None:
        """Report the first time a live peer claims a different primary.

        A fleet can disagree about the lease and look healthy from every node at once:
        each node answers for itself, and only the *refused* sender learns that its peers
        think otherwise. Naming it once per (peer, claim, winner) turns that into a line
        an operator can find, and an audited one rather than a guess.

        A peer that claims *us* is one heartbeat behind, not disagreeing.
        """
        if not winner:
            return
        for entry in candidates or []:
            node_id = str(entry.get("node_id", ""))
            claim = str(entry.get("primary") or "")
            if not claim or node_id == self.node_id or self.node_id == claim:
                continue
            if claim == winner:
                continue
            key = (node_id, claim, winner)
            if key in self._disagreement_seen:
                continue
            self._disagreement_seen.add(key)
            logger.warning("mesh election: %s claims %s, but %s leads here",
                           node_id, claim, winner)
            self._audit("mesh_lease_disagreement",
                        {"node_id": self.node_id, "peer": node_id,
                         "peer_primary": claim, "our_primary": winner})

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
        out.sort(key=self._rank)
        return out

    def tick(self, now=None):
        ts = _now() if now is None else float(now)
        candidates = self._live_candidates(ts)
        winner = candidates[0]["node_id"] if candidates else None
        # Incumbency: a holder that is *not worse* than the best candidate keeps the
        # lease. This is about the full rank, not priority alone -- priority alone makes
        # the fleet's leader depend on who was heard first, which is how two camps form
        # and never merge (see `_rank`). A strictly better candidate still wins.
        if winner and candidates:
            with self._lock:
                holder = self._primary_id
            if holder and holder != winner:
                incumbent = next((e for e in candidates
                                  if e.get("node_id") == holder), None)
                if incumbent is not None and (
                        self._rank(incumbent) <= self._rank(candidates[0])):
                    winner = holder
        self._note_disagreement(winner, candidates)
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

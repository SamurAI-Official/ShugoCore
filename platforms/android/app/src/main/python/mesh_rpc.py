"""Mesh RPC layer-split orchestration (Option 2).

Distributes transformer layers onto paired peripheral devices through
llama.cpp's RPC backend: a peripheral runs ``ggml-rpc-server`` (its memory and
CPU become an offload device) and the host's ``llama-server`` assigns layers to
it. Measured end-to-end on our hardware (see ``docs/layer_split_rpc.md``): with
every layer on a Tab S9 FE the host's RSS fell 677 MB -> 206 MB while the phone
held 402 MB resident, at 1.95 tok/s decode versus 140 tok/s local.

Two properties are enforced here rather than trusted from the transport:

* **The RPC server has no authentication** (llama.cpp: "Never expose the RPC
  server to an open network!"). Binding beyond a private address therefore
  requires an explicit ``allow_lan=True`` and is audited.
* **The capacity an RPC device advertises is not usable headroom.** A phone with
  ~504 MB actually available reported 5425 MiB free. Budgets are computed from
  *measured* free memory minus a reserve; the advertised figure only ever acts
  as an upper clamp.
"""
import ipaddress
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_RPC_PORT = 50052
RPC_BINARY_NAMES = ("ggml-rpc-server", "rpc-server")

# Leave this much of a peripheral's free RAM alone: the agent runtime, the
# resident model and the OS all need room, and an OOM-killed peripheral takes
# the whole generation with it.
DEFAULT_RESERVE_BYTES = 192 * 1024 * 1024

# Android thermal status at or above this is refused work (3 = severe,
# 4 = critical). Measured: the A51 sat at 4 with decisions ~100 s apart.
THERMAL_REFUSE_STATUS = 3

_POPEN = subprocess.Popen


def find_rpc_server(prefix: Optional[str] = None,
                    which: Optional[Any] = None) -> Optional[str]:
    """Locate an RPC server binary.

    Search order: ``SHUGOCORE_RPC_SERVER`` (explicit operator override),
    ``$PREFIX/bin/<name>`` (Termux package), then ``shutil.which`` on PATH.
    Returns None when nothing executable is found — the caller then keeps
    inference local rather than starting a server that cannot work.
    """
    if which is None:
        which = shutil.which
    override = (os.environ.get("SHUGOCORE_RPC_SERVER", "") or "").strip()
    if override:
        return override
    prefix = os.environ.get("PREFIX", "") if prefix is None else str(prefix)
    if prefix:
        for name in RPC_BINARY_NAMES:
            candidate = os.path.join(prefix, "bin", name)
            try:
                if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                    return candidate
            except Exception:
                continue
    for name in RPC_BINARY_NAMES:
        try:
            found = which(name)
        except Exception:
            found = None
        if found:
            return found
    return None


def is_private_bind(host: str) -> bool:
    """True when binding ``host`` cannot expose the socket to a public net.

    Loopback, RFC1918, link-local and CGNAT (100.64/10, e.g. Tailscale) count as
    private. The wildcard ``0.0.0.0``/``::`` does NOT: it includes whatever
    public interface the device happens to have.
    """
    text = str(host or "").strip()
    if not text:
        return False
    if text == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    if addr.is_unspecified:
        # 0.0.0.0 / :: — the wildcard binds every interface, including whatever
        # public one the device has.
        return False
    if addr.version == 4 and addr in ipaddress.ip_network("100.64.0.0/10"):
        # RFC 6598 shared address space (CGNAT, e.g. a Tailscale address) is a
        # private hop, but not every Python version counts it as private.
        return True
    # is_private covers RFC1918 + loopback; is_link_local covers 169.254/16.
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local)


def usable_headroom(mem_available_bytes: Optional[int],
                    advertised_bytes: Optional[int] = None,
                    reserve_bytes: int = DEFAULT_RESERVE_BYTES) -> int:
    """Bytes a peripheral can safely hold, or 0 when it cannot hold anything.

    Clamped to the *measured* free memory: an RPC device's advertised figure is
    its total memory, which is routinely far larger than what is free.
    """
    measured = int(mem_available_bytes or 0)
    if measured <= 0:
        return 0
    if advertised_bytes:
        measured = min(measured, int(advertised_bytes))
    return max(0, measured - int(reserve_bytes))



def plan_layer_split(nodes: List[Dict[str, Any]], total_layers: int,
                     bytes_per_layer: int,
                     local_layers_min: int = 1) -> Dict[str, Any]:
    """Assign layers to peripherals by measured headroom (fail-closed).

    Each node dict may carry ``device_id``, ``mem_available_bytes``,
    ``advertised_bytes``, ``thermal_status`` and ``paired``. Unpaired or
    thermally-critical nodes are skipped and reported; whatever no peripheral
    can hold stays local, so the model always still runs.

    Returns ``{"assignments", "remote_layers", "local_layers", "skipped",
    "headroom"}``.
    """
    total = max(0, int(total_layers))
    per_layer = max(1, int(bytes_per_layer))
    keep_local = max(0, min(int(local_layers_min), total))
    budget_layers = max(0, total - keep_local)

    skipped: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []
    headroom: Dict[str, int] = {}
    for node in nodes or []:
        device_id = str(node.get("device_id") or "").strip()
        if not device_id:
            continue
        if node.get("paired") is False:
            skipped.append({"device_id": device_id, "reason": "unpaired"})
            continue
        thermal = node.get("thermal_status")
        try:
            thermal_int = int(thermal) if thermal is not None else None
        except (TypeError, ValueError):
            thermal_int = None
        if thermal_int is not None and thermal_int >= THERMAL_REFUSE_STATUS:
            skipped.append({"device_id": device_id,
                            "reason": f"thermal_status={thermal_int}"})
            continue
        room = usable_headroom(node.get("mem_available_bytes"),
                               node.get("advertised_bytes"))
        headroom[device_id] = room
        layers = room // per_layer
        if layers <= 0:
            skipped.append({"device_id": device_id,
                            "reason": "insufficient_headroom"})
            continue
        candidates.append({"device_id": device_id, "layers": layers,
                           "room": room})

    # Largest headroom first: makes a split deterministic across runs and keeps
    # the fewest nodes involved for a given model.
    candidates.sort(key=lambda c: (-c["room"], c["device_id"]))
    assignments: Dict[str, int] = {}
    remaining = budget_layers
    for candidate in candidates:
        if remaining <= 0:
            break
        take = min(candidate["layers"], remaining)
        if take <= 0:
            continue
        assignments[candidate["device_id"]] = take
        remaining -= take

    remote = sum(assignments.values())
    return {"assignments": assignments, "remote_layers": remote,
            "local_layers": total - remote, "skipped": skipped,
            "headroom": headroom}



class MeshRpcLauncher:
    """Owns one ``ggml-rpc-server`` child process on this device.

    Mirrors ``TermuxLlamaServer``'s discipline: ``start()`` refuses to run when
    no binary exists, or when the bind would put the unauthenticated RPC socket
    on a non-private address without the operator explicitly allowing it, and
    ``stop()`` is idempotent. ``running()`` reports process liveness without
    touching the network.
    """

    def __init__(self, binary: Optional[str] = None,
                 host: str = "127.0.0.1",
                 port: int = DEFAULT_RPC_PORT,
                 threads: Optional[int] = None,
                 devices: Optional[List[str]] = None,
                 extra_args: Optional[List[str]] = None,
                 allow_lan: bool = False,
                 audit: Optional[Any] = None,
                 popen: Optional[Any] = None,
                 logger: Optional[Any] = None):
        self.binary = binary or find_rpc_server()
        self.host = str(host or "127.0.0.1")
        self.port = max(1, min(65535, int(port)))
        self.threads = (max(1, int(threads)) if threads
                        else max(1, (os.cpu_count() or 2) // 2))
        self.devices = list(devices or [])
        self.extra_args = list(extra_args or [])
        self.allow_lan = bool(allow_lan)
        self.audit = audit
        self._popen = popen or _POPEN
        self._log = logger or logging.getLogger(__name__)
        self._proc: Optional[Any] = None

    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def command(self) -> List[str]:
        cmd = [str(self.binary or ""), "-H", self.host, "-p", str(self.port),
               "-t", str(self.threads)]
        if self.devices:
            cmd.extend(["-d", ",".join(self.devices)])
        cmd.extend(self.extra_args)
        return cmd

    def start(self) -> bool:
        """Launch the peripheral RPC server; True when spawned."""
        if not self.binary:
            self._log.warning(
                "MeshRpcLauncher: no RPC server binary (set "
                "SHUGOCORE_RPC_SERVER or install one)")
            return False
        if not is_private_bind(self.host):
            if not self.allow_lan:
                self._log.warning(
                    "MeshRpcLauncher: refusing to bind %s — llama.cpp's RPC "
                    "server is unauthenticated; bind a private address or pass "
                    "allow_lan=True (audited)", self.host)
                self._audit("mesh_rpc_bind_refused", {"host": self.host})
                return False
            self._log.warning(
                "MeshRpcLauncher: binding %s on a non-private address — the RPC "
                "socket is unauthenticated; keep it behind a trusted link",
                self.host)
            self._audit("mesh_rpc_bind_exposed", {"host": self.host,
                                                  "port": self.port})
        try:
            self._proc = self._popen(
                self.command(), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except Exception as exc:
            self._log.error("MeshRpcLauncher: spawn failed: %s", exc)
            self._proc = None
            return False
        self._log.info("MeshRpcLauncher: spawned %s pid=%s on %s",
                       self.binary, getattr(self._proc, "pid", "?"),
                       self.endpoint())
        self._audit("mesh_rpc_started", {"endpoint": self.endpoint(),
                                         "threads": self.threads})
        return True

    def running(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except Exception:
            return False

    def stop(self) -> bool:
        """Terminate the child (idempotent); True when it is gone."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return True
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            wait = getattr(proc, "wait", None)
            if callable(wait):
                wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._audit("mesh_rpc_stopped", {"endpoint": self.endpoint()})
        return True

    def _audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event_type, payload)
        except Exception as exc:
            self._log.warning("audit append failed for '%s': %s",
                              event_type, exc)


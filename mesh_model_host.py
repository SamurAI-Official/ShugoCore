#!/usr/bin/env python3
"""Mesh model host: run the model with layers spread across the hive (Option 2).

``plan_layer_split()`` has existed since the layer-split work and was called by
nobody. This is the runtime that uses it: from the fleet's own advertisements
(each peer's *measured* headroom and thermal state, which the election already
carries), it decides which devices take how many layers, asks each of them to
start its RPC peripheral over the mesh, launches the host model with ``--rpc``,
then verifies that it answers.

Fail-closed at every step. No plan, no peripheral, or a failed probe means the
model still runs -- locally -- and the reason is reported rather than implied.
Two properties are enforced here rather than trusted from the transport:

* **the RPC socket is unauthenticated** (llama.cpp says so itself), so the
  peripheral is asked to bind a private address, LAN exposure is an explicit
  operator choice, and the launcher audits it;
* **an advertised device capacity is not usable headroom** (a phone with ~504 MB
  free reported 5425 MiB), so every budget comes from measured free memory minus
  a reserve.
"""
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from mesh_rpc import (DEFAULT_RESERVE_BYTES, DEFAULT_RPC_PORT,
                      plan_layer_split)

logger = logging.getLogger(__name__)

DEFAULT_MODEL_HOST_PORT = 8099
HEALTH_TIMEOUT_S = 120.0
ENDPOINT_TIMEOUT_S = 45.0
SETTLE_TIMEOUT_S = 60.0


def plan_for_fleet(live_peers, total_layers: int, bytes_per_layer: int, *,
                   local_layers_min: int = 1,
                   reserve_bytes: int = DEFAULT_RESERVE_BYTES,
                   exclude=()) -> Dict[str, Any]:
    """Layer assignment for the live hive.

    ``live_peers`` is whatever the election considers live: a peer that has
    stopped heartbeating is not offered layers, so a plan cannot include a device
    that is already gone. ``exclude`` is the operator's own list -- the machine you
    are working on should not be asked to hold layers just because it has memory.
    """
    skip = {str(name).strip() for name in (exclude or ()) if str(name).strip()}
    nodes: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    for peer in live_peers or []:
        if not isinstance(peer, dict):
            continue
        device_id = str(peer.get("node_id") or peer.get("device_id") or "").strip()
        if not device_id:
            continue
        if device_id in skip:
            excluded.append({"device_id": device_id, "reason": "excluded by operator"})
            continue
        entry = {"device_id": device_id,
                 "mem_available_bytes": peer.get("mem_available_bytes"),
                 "thermal_status": peer.get("thermal_status")}
        if peer.get("paired") is not None:
            entry["paired"] = peer.get("paired")
        nodes.append(entry)
    plan = plan_layer_split(nodes, total_layers, bytes_per_layer,
                            local_layers_min=local_layers_min,
                            reserve_bytes=int(reserve_bytes))
    # The reserve is a property of the peripheral, not of the plan, so report the
    # headroom the same way the planner measured it.
    plan["reserve_bytes"] = int(reserve_bytes)
    # What the planner was actually given. "insufficient_headroom" is only useful
    # if the number behind it is visible: a peer's advertised memory and the value
    # the planner decided on are not the same thing until you print both.
    plan["nodes"] = nodes
    plan["bytes_per_layer"] = int(bytes_per_layer)
    plan["total_layers"] = int(total_layers)
    if excluded:
        plan["skipped"] = list(plan.get("skipped") or []) + excluded
    return plan


def endpoint_map(peers=None, env=None, port=None) -> Dict[str, str]:
    """``device_id -> "host:port"`` for the mesh peers.

    ``peers`` is the agent's own form (``[(id, host, port), ...]``), which is what
    the transport actually dials; ``SHUGOCORE_MESH_PEERS`` is the fallback the
    runtime itself uses.

    ``port`` overrides the peer's own port, keeping the peer map's host. A mesh
    model host dials each device's *RPC* port on the host the transport uses, and
    mixing the two is invisible: the transport port is open as well, so a
    reachability check passes and then the model cannot offload.
    """
    out: Dict[str, str] = {}
    for entry in peers or []:
        try:
            device_id, host, peer_port = entry[0], entry[1], int(entry[2])
        except Exception:
            continue
        if device_id and host and 0 < peer_port < 65536:
            out[str(device_id)] = f"{host}:{int(port or peer_port)}"
    if out:
        return out
    raw = (env if env is not None else os.environ).get("SHUGOCORE_MESH_PEERS", "")
    for chunk in str(raw or "").split(","):
        name, _, address = chunk.partition("=")
        name, address = name.strip(), address.strip()
        if not name or ":" not in address:
            continue
        host, _, peer_port = address.rpartition(":")
        if host and peer_port.isdigit():
            out[name] = f"{host}:{int(port or peer_port)}"
    return out


def endpoint_reachable(endpoint, timeout: float = 2.0, connect=None) -> bool:
    """True when a TCP connect to ``host:port`` succeeds (0 when malformed)."""
    host, _, port = str(endpoint or "").rpartition(":")
    if not host or not port.isdigit():
        return False
    connector = connect or socket.create_connection
    try:
        with connector((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def host_extra_args(assignments, endpoints, *, context=2048, threads=0,
                    mmap=True) -> List[str]:
    """llama.cpp arguments that spread ``assignments`` across the hive.

    Pure: the only inputs are the plan and where each device can be reached, so
    the argument shape is testable without a server. One peripheral needs no
    split; several need `--tensor-split`, which is expressed as the layer counts
    themselves.
    """
    order = [device for device in sorted(assignments or {})
             if endpoints.get(device)]
    args: List[str] = []
    if order:
        servers = [endpoints[device] for device in order]
        args += ["--rpc", ",".join(servers),
                 "-dev", ",".join(f"RPC{i}" for i in range(len(servers)))]
        remote = sum(int(assignments[device]) for device in order)
        args += ["-ngl", str(max(1, remote))]
        if len(servers) > 1:
            # More than one peripheral needs the split stated: the layer counts
            # are the proportions llama.cpp should use.
            args += ["-ts", ",".join(str(int(assignments[d])) for d in order)]
    if context:
        args += ["-c", str(int(context))]
    if threads:
        args += ["-t", str(int(threads))]
    if not mmap:
        args += ["--no-mmap"]
    return args


def probe_completion(base_url: str, timeout: float = 60.0) -> bool:
    """Ask the host for one token: `/health` alone does not prove it can infer.

    A server can be healthy before the model is loaded, and llama.cpp answers
    `/health` while a model is still being mmapped.
    """
    import json
    import urllib.request

    payload = {"prompt": "ok", "n_predict": 1, "temperature": 0}
    request = urllib.request.Request(
        base_url.rstrip("/") + "/completion",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(getattr(response, "status", 0) or 0) < 300
    except Exception:
        return False


def health_ok(base_url: str, timeout: float = 5.0) -> bool:
    """True when the host answers its own health endpoint."""
    import urllib.request

    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/health",
                                    timeout=timeout) as response:
            return 200 <= int(getattr(response, "status", 0) or 0) < 300
    except Exception:
        return False


def _default_launcher(model_path, binary, *, port, extra_args):
    """The host model process: the same launcher class the agent already uses."""
    from android_inference import TermuxLlamaServer  # noqa: WPS433

    return TermuxLlamaServer(model_path=model_path, binary=binary,
                             host="127.0.0.1", port=port,
                             extra_args=list(extra_args or []))


class MeshModelHost:
    """Owns the host model process and the peripherals it offloads to.

    Every collaborator is injectable -- launcher, socket connector, prober, clock
    -- so the whole start sequence can be exercised without a model, a socket, a
    peer, or a wait.
    """

    def __init__(self, model_path, *, agent=None, binary=None,
                 port=DEFAULT_MODEL_HOST_PORT, rpc_port=DEFAULT_RPC_PORT,
                 total_layers=24, context=2048, threads=0,
                 reserve_bytes=DEFAULT_RESERVE_BYTES, local_layers_min=1,
                 allow_lan=False, mmap=None, launcher=None, connect=None,
                 prober=None, live_peers=None, peers=None, sleep=None,
                 exclude=(),
                 reach_timeout=ENDPOINT_TIMEOUT_S,
                 health_timeout=HEALTH_TIMEOUT_S,
                 settle_timeout=SETTLE_TIMEOUT_S):
        self.model_path = str(model_path or "").strip()
        self.agent = agent
        self.binary = binary
        self.port = max(1, min(65535, int(port)))
        self.rpc_port = max(1, min(65535, int(rpc_port)))
        self.total_layers = max(1, int(total_layers))
        self.context = max(0, int(context))
        self.threads = max(0, int(threads))
        self.reserve_bytes = int(reserve_bytes)
        self.local_layers_min = max(0, int(local_layers_min))
        self.allow_lan = bool(allow_lan)
        self.exclude = {str(name).strip() for name in (exclude or ())
                        if str(name).strip()}
        self.mmap = mmap
        self.reach_timeout = float(reach_timeout)
        self.health_timeout = float(health_timeout)
        self.settle_timeout = float(settle_timeout)
        self._make_launcher = launcher or _default_launcher
        self._connect = connect
        self._probe = prober or probe_completion
        self._live = live_peers
        # A static list is an answer; a callable is a live query, like the election.
        self._live_is_explicit = live_peers is not None and not callable(live_peers)
        self._peers = peers
        self._sleep = sleep or time.sleep
        self._launcher = None
        self._state: Dict[str, Any] = {"mode": "off", "reason": "not started"}

    # -- inputs ---------------------------------------------------------------
    def bytes_per_layer(self) -> int:
        """The model's size spread evenly over its layers (1 when unmeasurable)."""
        try:
            size = int(os.path.getsize(self.model_path))
        except OSError:
            size = 0
        return max(1, size // self.total_layers) if size else 1

    def fleet_peers(self) -> List[tuple]:
        """The transport's own peer list: what this node can actually dial."""
        source = self._peers
        if callable(source):
            source = source()
        if source:
            return list(source)
        reader = getattr(self.agent, "mesh_peer_endpoints", None)
        if not callable(reader):
            return []
        try:
            return list(reader() or [])
        except Exception:
            return []

    def endpoints(self) -> Dict[str, str]:
        """Where each peer's *model* port is: the transport host, the RPC port."""
        return endpoint_map(self.fleet_peers(), port=self.rpc_port)

    def live_peers(self) -> List[Dict[str, Any]]:
        """The election's live set: a peer that stopped beating takes no layers."""
        source = self._live
        if callable(source):
            source = source()
        if source is not None:
            return list(source or [])
        election = getattr(self.agent, "mesh_election", None)
        try:
            return list(election.live_peers()) if election is not None else []
        except Exception:
            return []

    def plan(self) -> Dict[str, Any]:
        return plan_for_fleet(self.live_peers(), self.total_layers,
                              self.bytes_per_layer(),
                              local_layers_min=self.local_layers_min,
                              reserve_bytes=self.reserve_bytes,
                              exclude=self.exclude)

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> Dict[str, Any]:
        """Plan, wake the peripherals, launch the model, verify it answers."""
        if not self.model_path:
            return self._record({"mode": "off", "reason": "no model path"})
        if not self.binary:
            from android_inference import find_llama_server  # noqa: WPS433
            self.binary = find_llama_server()
        plan = self.plan()
        if self._should_wait_for_peers(plan):
            # A node that just booted has an election verdict before it has peers,
            # so planning immediately reports "no peripheral" and the hive hosts
            # the model locally for ever. Wait for the mesh to say something --
            # the same trap --say hit.
            deadline = time.monotonic() + max(0.0, self.settle_timeout)
            while time.monotonic() < deadline and not self.live_peers():
                self._sleep(2.0)
            plan = self.plan()
        assignments = {str(k): int(v)
                       for k, v in (plan.get("assignments") or {}).items()}
        endpoints = self.endpoints()
        started: List[Dict[str, Any]] = []
        unreachable: List[Dict[str, Any]] = []
        for device in sorted(assignments):
            endpoint = endpoints.get(device, "")
            if not endpoint:
                # No dialable address means nothing can be verified, so the layers
                # stay on the host rather than being promised to a device this
                # node cannot reach.
                assignments.pop(device, None)
                unreachable.append({"device_id": device, "reason": "no endpoint"})
                continue
            self._ask_peripheral(device, "start")
            if self._wait_reachable(endpoint):
                started.append({"device_id": device, "endpoint": endpoint,
                                "layers": assignments[device]})
            else:
                assignments.pop(device, None)
                unreachable.append({"device_id": device,
                                    "reason": "peripheral did not answer"})
        remote = sum(assignments.values())
        if not remote:
            # Say why the hive is hosting this locally: otherwise "local" looks
            # like it chose not to use the fleet rather than that it could not.
            reasons = [f"{item.get('device_id')}:{item.get('reason')}"
                       for item in (plan.get("skipped") or [])]
            state_reason = ("no peripheral with usable headroom" if not reasons
                            else "skipped " + ", ".join(reasons[:4]))
        else:
            state_reason = ""
        if not remote:
            # Print the planner's inputs whenever it declines to offload: the
            # difference between "the device has no room" and "the advertisement
            # said so" is invisible otherwise.
            logger.info("mesh model host: nothing assigned -- reserve=%s B, "
                        "bytes/layer=%s B, nodes=%s, skipped=%s",
                        plan.get("reserve_bytes"), plan.get("bytes_per_layer"),
                        plan.get("nodes"), plan.get("skipped"))
        extra = host_extra_args(assignments, endpoints, context=self.context,
                                threads=self.threads,
                                mmap=self._effective_mmap(remote))
        state: Dict[str, Any] = {
            "mode": "split" if remote else "local",
            "remote_layers": remote, "local_layers": self.total_layers - remote,
            "assignments": assignments, "started": started,
            "skipped": plan.get("skipped") or [], "unreachable": unreachable,
            "headroom": plan.get("headroom") or {}, "extra_args": extra,
            "plan_nodes": plan.get("nodes") or [],
            "bytes_per_layer": plan.get("bytes_per_layer"),
            "reserve_bytes": plan.get("reserve_bytes"),
            "host_port": self.port, "rpc_port": self.rpc_port,
            "launcher": type(self._launcher).__name__ if self._launcher else "",
            "reason": state_reason, "launched": False, "health": False,
            "probe": False}
        if remote and not extra:
            state["reason"] = "no usable peripheral endpoints"
        state["launched"] = self._launch(extra)
        if not state["launched"]:
            state["reason"] = state["reason"] or "host model did not start"
            return self._record(state)
        state.update(self._verify())
        if not state["health"]:
            state["reason"] = state["reason"] or "host model did not answer /health"
        elif not state["probe"]:
            state["reason"] = "host model answered /health but failed its probe"
        return self._record(state)

    def stop(self) -> Dict[str, Any]:
        """Stop the host model and release the peripherals (best effort)."""
        state = dict(self._state)
        launcher, self._launcher = self._launcher, None
        if launcher is not None:
            try:
                launcher.stop()
            except Exception as exc:
                logger.warning("mesh model host stop failed: %s", exc)
        for entry in state.get("started") or []:
            self._ask_peripheral(str((entry or {}).get("device_id")), "stop")
        self._state = {"mode": "off", "reason": "stopped"}
        return dict(self._state)

    def status(self) -> Dict[str, Any]:
        return dict(self._state)

    def summary_line(self) -> str:
        """One status-line field: what this hive is actually hosting."""
        state = self._state
        mode = state.get("mode", "off")
        if mode == "split":
            devices = ",".join(f"{device}:{layers}" for device, layers in
                               sorted((state.get("assignments") or {}).items()))
            verified = "" if state.get("health") and state.get("probe") \
                else ",unverified"
            return (f"split(layers={state.get('remote_layers')}/"
                    f"{self.total_layers} dev={devices}{verified})")
        if mode == "local":
            return f"local({state.get('reason') or 'no peripherals'})"
        return f"{mode}({state.get('reason') or 'not started'})"

    # -- internals ------------------------------------------------------------
    def _should_wait_for_peers(self, plan) -> bool:
        """Wait for a hive to finish arriving; a solo node starts straight away.

        A booting node plans against whoever has been discovered and has
        heartbeated so far -- and on a fresh start that can be one desktop-class
        peer, so the plan looks definitive while the phones are still quiet and the
        hive hosts the model locally for ever. A node that knows of no peers at all
        is solo, not late, and must not be delayed. A *static* ``live_peers`` list
        means the caller already answered the question; a callable is a live query,
        like the real election.
        """
        if self._live_is_explicit or self.settle_timeout <= 0:
            return False
        if plan.get("assignments"):
            return False
        return bool(self.fleet_peers())

    def _effective_mmap(self, remote: int) -> bool:
        """Auto: mmap locally, release the host's copy when offloading.

        With mmap the host keeps the GGUF mapped and its RSS stays at the
        local-only figure (measured 445-476 MB against 170 MB without), so the
        offload would be invisible. Host memory is the point of the split, so it
        defaults off once layers are remote.
        """
        if self.mmap is not None:
            return bool(self.mmap)
        return not remote

    def _ask_peripheral(self, device: str, action: str) -> Dict[str, Any]:
        """Ask a peer to start/stop its RPC peripheral (fire and forget).

        Fire and forget on purpose: what matters is whether the socket becomes
        reachable, and that is checked directly. A reply would only say what the
        peer believed at the time.
        """
        delegate = getattr(self.agent, "_mesh_delegate", None)
        if not callable(delegate):
            return {"status": "error", "message": "no delegation channel"}
        payload = {"action_type": "mesh_rpc",
                   "params": {"action": action, "port": self.rpc_port,
                              "lan": 1 if self.allow_lan else 0}}
        try:
            return dict(delegate(device, payload) or {})
        except Exception as exc:
            return {"status": "error", "message": type(exc).__name__}

    def _wait_reachable(self, endpoint: str) -> bool:
        """Poll the peripheral's socket until it accepts, or the budget runs out."""
        deadline = time.monotonic() + max(0.0, self.reach_timeout)
        while True:
            if endpoint_reachable(endpoint, connect=self._connect):
                return True
            if time.monotonic() >= deadline:
                return False
            self._sleep(2.0)

    def _launch(self, extra: List[str]) -> bool:
        """Start the host model, fail-closed when no binary or model is there."""
        try:
            launcher = self._make_launcher(self.model_path, self.binary,
                                           port=self.port, extra_args=extra)
        except Exception as exc:
            logger.warning("mesh model host launcher failed: %s", exc)
            return False
        if launcher is None:
            return False
        self._launcher = launcher
        try:
            started = bool(launcher.start())
        except Exception as exc:
            logger.warning("mesh model host launch failed: %s", exc)
            return False
        if started:
            logger.info("mesh model host: %s on :%s %s", self.binary, self.port,
                        " ".join(extra) if extra else "(local only)")
        return started

    def _verify(self) -> Dict[str, bool]:
        """Health, then one token: a bound socket is not a working model.

        `/health` answers while a model is still being loaded, so a real
        generation is the only honest check that the split works.
        """
        base = f"http://127.0.0.1:{self.port}"
        deadline = time.monotonic() + max(0.0, self.health_timeout)
        healthy = False
        while not healthy and time.monotonic() < deadline:
            healthy = health_ok(base)
            if not healthy:
                self._sleep(1.0)
        return {"health": healthy,
                "probe": bool(self._probe(base)) if healthy else False}

    def _record(self, state: Dict[str, Any]) -> Dict[str, Any]:
        self._state = dict(state)
        logger.info("mesh model host %s", self.summary_line())
        audit = getattr(getattr(self.agent, "engine", None), "audit", None)
        if audit is not None:
            try:
                audit.append("mesh_model_host_" + str(state.get("mode", "off")),
                             {key: state.get(key) for key in
                              ("mode", "remote_layers", "local_layers",
                               "assignments", "started", "skipped",
                               "unreachable", "reason", "health", "probe")})
            except Exception:
                pass
        return dict(self._state)

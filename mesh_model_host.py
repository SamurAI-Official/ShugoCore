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

The plan is not one-off. A node plans before part of its fleet has been discovered,
and the first live cross-device run missed a phone by one second and then hosted the
model locally for the rest of the process's life. ``reconcile()`` re-plans on a
cadence instead: a device that appears gets offered the layers the host is holding,
one that goes thermally critical or drops out gives its layers back, and the host
model itself is watched -- restarted with fewer remote layers each time until it
serves.

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
# A device that went thermally critical is not offered layers again immediately:
# the next advertisement it sends may still look cool.
THERMAL_PIN_S = 300.0
# A candidate that did not answer is not re-woken on every check either.
UNREACHABLE_PIN_S = 300.0
# Re-launching the host model costs a reload, so a rebalance happens at most this
# often and only once a change has persisted across checks.
REBALANCE_COOLDOWN_S = 300.0
REBALANCE_STABLE_RUNS = 2
MAX_RESTARTS = 3


def exclusion_reasons(exclude) -> Dict[str, str]:
    """Normalise ``exclude`` into ``{device_id: reason}``.

    A reason is what makes an exclusion actionable: "peripheral did not answer" and
    "the operator is using that machine" are different problems, and a plan that
    reports one as the other sends the operator after the wrong thing.
    """
    if isinstance(exclude, dict):
        return {str(name).strip(): str(reason or "excluded by operator")
                for name, reason in exclude.items() if str(name).strip()}
    return {str(name).strip(): "excluded by operator"
            for name in (exclude or ()) if str(name).strip()}


def plan_for_fleet(live_peers, total_layers: int, bytes_per_layer: int, *,
                   local_layers_min: int = 1,
                   reserve_bytes: int = DEFAULT_RESERVE_BYTES,
                   exclude=()) -> Dict[str, Any]:
    """Layer assignment for the live hive.

    ``live_peers`` is whatever the election considers live: a peer that has
    stopped heartbeating is not offered layers, so a plan cannot include a device
    that is already gone. ``exclude`` is the caller's own list -- names, or
    ``{name: reason}`` -- covering the operator's preferences (the machine you are
    working on should not hold layers just because it has memory) and what a check
    already learned (a candidate whose peripheral did not answer).
    """
    skip = exclusion_reasons(exclude)
    nodes: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    for peer in live_peers or []:
        if not isinstance(peer, dict):
            continue
        device_id = str(peer.get("node_id") or peer.get("device_id") or "").strip()
        if not device_id:
            continue
        if device_id in skip:
            excluded.append({"device_id": device_id, "reason": skip[device_id]})
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


def split_difference(current, desired) -> Dict[str, Any]:
    """What changed between the running split and the plan the hive now supports.

    Both are ``{device_id: layers}``. A *detach* is a device holding layers that the
    plan no longer offers them to (thermally critical, gone, out of headroom); an
    *attach* is a device the plan has just started using; a *resize* is a device
    keeping layers, but not the same number. Remote-layer totals are reported as a
    delta so a caller can see the direction without doing the arithmetic twice.
    """
    current = {str(k): int(v) for k, v in (current or {}).items()}
    desired = {str(k): int(v) for k, v in (desired or {}).items()}
    detach = sorted(d for d in current if d not in desired)
    attach = sorted(d for d in desired if d not in current)
    resize = sorted(d for d in current if d in desired and current[d] != desired[d])
    return {"changed": bool(detach or attach or resize),
            "detach": detach, "attach": attach, "resize": resize,
            "remote_delta": sum(desired.values()) - sum(current.values())}


def degrade_split(assignments, headroom=None, *, drop=None) -> Dict[str, Any]:
    """Give up exactly one device's layers, weakest first.

    The watchdog uses this when the host model will not come up with the split it was
    given: restart with fewer remote layers rather than failing, repeatedly, until
    the model runs locally if it has to. The weakest device is the one with the least
    measured headroom, because that is the one whose RPC peripheral is most likely to
    be the reason the model cannot load.
    """
    current = {str(k): int(v) for k, v in (assignments or {}).items()}
    if not current:
        return {"assignments": {}, "dropped": None, "layers": 0,
                "reason": "nothing remote left to give up"}
    if drop and str(drop) in current:
        chosen = str(drop)
    else:
        chosen = min(current, key=lambda device: (
            int((headroom or {}).get(device, 0)), device))
    remaining = {d: n for d, n in current.items() if d != chosen}
    return {"assignments": remaining, "dropped": chosen, "layers": current[chosen],
            "reason": f"degraded: {chosen} gave up {current[chosen]} layer(s)"}


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
                 settle_timeout=SETTLE_TIMEOUT_S,
                 health=None, port_serving=None, clock=None,
                 thermal_pin_s=THERMAL_PIN_S,
                 unreachable_pin_s=UNREACHABLE_PIN_S,
                 cooldown_s=REBALANCE_COOLDOWN_S,
                 stable_runs=REBALANCE_STABLE_RUNS,
                 max_restarts=MAX_RESTARTS):
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
        self.thermal_pin_s = float(thermal_pin_s)
        self.unreachable_pin_s = float(unreachable_pin_s)
        self.cooldown_s = float(cooldown_s)
        self.stable_runs = max(1, int(stable_runs))
        self.max_restarts = max(0, int(max_restarts))
        # Two different questions, so two injectable answers: does something already
        # answer on our port *before* we start, and does the model answer *after*.
        self._health = health or health_ok
        self._port_serving = port_serving or (lambda base: health_ok(base))
        self._clock = clock or time.monotonic
        self._refused: Dict[str, Dict[str, Any]] = {}
        self._pending_sig: Optional[tuple] = None
        self._pending_runs = 0
        self._last_restart = 0.0
        self._relaunches = 0
        self._failures = 0
        self._reconciles = 0
        self._last_action = ""
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

    def plan(self, extra_exclude=None) -> Dict[str, Any]:
        """The plan the live fleet supports, with pins and any extra exclusions.

        ``extra_exclude`` is how a check reports what it just learned -- a candidate
        that was in the plan and did not answer -- with its reason, so the plan says
        what actually happened instead of blaming the operator.
        """
        exclude = exclusion_reasons(self.exclude)
        exclude.update(exclusion_reasons(extra_exclude))
        plan = plan_for_fleet(self.live_peers(), self.total_layers,
                              self.bytes_per_layer(),
                              local_layers_min=self.local_layers_min,
                              reserve_bytes=self.reserve_bytes,
                              exclude=exclude)
        return self._apply_pins(plan)

    # -- lifecycle ------------------------------------------------------------
    def start(self, *, assignments: Optional[Dict[str, int]] = None,
              cause: str = "") -> Dict[str, Any]:
        """Plan, wake the peripherals, launch the model, verify it answers.

        ``assignments`` and ``cause`` are the reconcile path: an exact split the
        caller has already decided on (possibly a degraded one) and why, so the state
        still explains itself after a restart.
        """
        if not self.model_path:
            return self._record({"mode": "off", "reason": "no model path"})
        if not self.binary:
            from android_inference import find_llama_server  # noqa: WPS433
            self.binary = find_llama_server()
        plan = self.plan()
        if assignments is None:
            if self._should_wait_for_peers(plan):
                # A booting node has an election verdict before it has peers: a solo
                # node must start straight away, while a hive waits a bounded time
                # for the rest of it to check in.
                deadline = self._clock() + max(0.0, self.settle_timeout)
                while self._clock() < deadline and not self.live_peers():
                    self._sleep(2.0)
                plan = self.plan()
            assignments = {str(k): int(v)
                           for k, v in (plan.get("assignments") or {}).items()}
        else:
            # A reconcile: the caller's verdict is the fleet's, and re-planning here
            # would quietly undo a deliberate degrade.
            assignments = {str(k): int(v) for k, v in (assignments or {}).items()}
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
            reply = self._ask_peripheral(device, "start")
            if self._wait_reachable(endpoint):
                started.append({"device_id": device, "endpoint": endpoint,
                                "layers": assignments[device]})
            else:
                assignments.pop(device, None)
                unreachable.append({"device_id": device,
                                    "reason": self._refusal_reason(reply)})
                # Held out for a while, so the next check does not re-ask a device that
                # has just been given its chance and stayed silent.
                self._pin(device, self._refusal_reason(reply),
                          seconds=self.unreachable_pin_s)
        remote = sum(assignments.values())
        if not remote:
            # Say why the hive is hosting this locally. A device the plan *did* choose
            # that then never answered is a different problem from a fleet with no
            # room, and reporting one as the other sends the operator after the wrong
            # thing.
            reasons = [f"{item.get('device_id')}:{item.get('reason')}"
                       for item in (plan.get("skipped") or [])]
            reasons += [f"{item.get('device_id')}:{item.get('reason')}"
                        for item in unreachable]
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
            "reason": state_reason or str(cause or ""),
            "relaunches": self._relaunches,
            "launched": False, "health": False,
            "probe": False}
        if remote and not extra:
            self._note(state, "no usable peripheral endpoints")
        if self._port_serving(f"http://127.0.0.1:{self.port}"):
            # Something already answers on our port -- usually a host model left
            # behind by an earlier run. Launching anyway means the verification
            # below talks to *that* server, and the split gets reported as verified
            # by a model that is not ours.
            self._note(state, f"port {self.port} is already serving another "
                              "process")
            return self._record(state)
        state["launched"] = self._launch(extra)
        if not state["launched"]:
            self._note(state, "host model did not start")
            return self._record(state)
        state.update(self._verify())
        if remote and not state["health"]:
            # Intent is not a mode: a launch that never answered is reported as
            # what it is, not as a working split.
            state["mode"] = "split-unhealthy"
        if not state["health"]:
            self._note(state, "host model did not answer /health")
        elif not state["probe"]:
            self._note(state, "host model answered /health but failed its probe")
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
        # Pins survive a stop on purpose: a device that just went critical must not
        # be handed layers by the next start either.
        self._pending_sig, self._pending_runs = None, 0
        self._failures = 0
        self._state = {"mode": "off", "reason": "stopped"}
        return dict(self._state)

    def status(self) -> Dict[str, Any]:
        return dict(self._state)

    def note_reconcile(self, action: str) -> None:
        """Count checks and remember the last verdict, for the status line.

        A hold is silent on purpose (a hive that reloads every check would lose its KV
        cache for nothing), so without this the status line cannot tell "checked and
        satisfied" from "never checked" -- which is exactly the question asked of it
        when a split did not appear.
        """
        self._reconciles += 1
        self._last_action = str(action or "?")

    def summary_line(self) -> str:
        """One status-line field: what this hive is actually hosting."""
        state = self._state
        mode = state.get("mode", "off")
        checked = (f",recon={self._reconciles}:{self._last_action}"
                   if self._reconciles else "")
        if mode == "split":
            devices = ",".join(f"{device}:{layers}" for device, layers in
                               sorted((state.get("assignments") or {}).items()))
            verified = "" if state.get("health") and state.get("probe") \
                else ",unverified"
            restarts = int(state.get("relaunches") or 0)
            relaunched = f",relaunch={restarts}" if restarts else ""
            return (f"split(layers={state.get('remote_layers')}/"
                    f"{self.total_layers} dev={devices}{verified}{relaunched}{checked})")
        if mode == "local":
            return f"local({state.get('reason') or 'no peripherals'}{checked})"
        return f"{mode}({state.get('reason') or 'not started'}{checked})"

    def reconcile(self, *, force: bool = False) -> Dict[str, Any]:
        """Re-plan against the live hive: the watchdog first, then the rebalance.

        A plan made before a device arrives is stale the moment it does -- the first
        live cross-device run missed a phone by one second and then hosted the model
        locally for the rest of the process's life. So on every check: make sure the
        host model is still serving what it was given, then re-plan and re-launch only
        when the fleet's verdict has *stayed* different (a phone that blips must not
        cost a restart) and the cooldown has passed.

        Returns ``{"action": "hold"|"deferred"|"restart"|"gave_up", ...}``.
        """
        state = dict(self._state)
        mode = state.get("mode")
        if not mode or mode in ("off", "stopped"):
            return {"action": "hold", "reason": "not started"}
        if mode == "failed":
            return {"action": "hold", "reason": "gave up restarting"}
        watchdog = self._watchdog_plan(state)
        if watchdog.get("action") == "gave_up":
            return self._give_up(str(watchdog.get("reason") or "unable to serve"))
        if watchdog.get("action") == "hold" and watchdog.get("port_conflict"):
            # Owned by another process: an environment problem, so it is reported and
            # nothing else happens -- no restart, no degrade.
            return watchdog
        if watchdog.get("action") == "restart":
            return self._relaunch(watchdog.get("assignments") or {},
                                  cause=str(watchdog.get("reason") or "watchdog"))
        plan = self.plan()
        desired = {str(k): int(v)
                   for k, v in (plan.get("assignments") or {}).items()}
        current = {str(k): int(v)
                   for k, v in (state.get("assignments") or {}).items()}
        difference = split_difference(current, desired)
        if not difference["changed"]:
            self._pending_sig, self._pending_runs = None, 0
            return {"action": "hold", "difference": difference}
        self._note_thermal_detaches(difference, plan)
        signature = tuple(sorted(desired.items()))
        self._pending_runs = (self._pending_runs + 1
                              if signature == self._pending_sig else 1)
        self._pending_sig = signature
        if not force and self._pending_runs < self.stable_runs:
            return {"action": "deferred", "difference": difference,
                    "reason": f"change seen {self._pending_runs}/"
                              f"{self.stable_runs} time(s)"}
        waited = self._clock() - self._last_restart
        if not force and waited < self.cooldown_s:
            return {"action": "deferred", "difference": difference,
                    "reason": f"cooldown ({waited:.0f}s of {self.cooldown_s:.0f}s)"}
        # Wake what we are about to *add* before stopping anything: only then is the new
        # plan proven, and only then is a reload worth the KV cache it costs.
        preflight = self._preflight(difference.get("attach") or [])
        for item in preflight["unreachable"]:
            self._pin(str(item["device_id"]),
                      str(item.get("reason") or "unreachable"),
                      seconds=self.unreachable_pin_s)
        if preflight["unreachable"]:
            plan = self.plan({str(item["device_id"]): str(item.get("reason"))
                              for item in preflight["unreachable"]})
            desired = {str(k): int(v)
                       for k, v in (plan.get("assignments") or {}).items()}
            difference = split_difference(current, desired)
            if not difference["changed"]:
                self._pending_sig, self._pending_runs = None, 0
                return self._hold_unreachable(state, plan, preflight)
        return self._relaunch(desired, cause=self._difference_reason(difference, plan))

    def _watchdog_plan(self, state) -> Dict[str, Any]:
        """Is the host model still serving, and what should be tried next?

        A model that died, or that cannot serve the split it was given, is restarted.
        Each further failure gives up one device's layers -- weakest first -- until the
        model runs locally rather than not at all.
        """
        launcher = self._launcher
        alive = bool(launcher is not None and launcher.running())
        base = f"http://127.0.0.1:{self.port}"
        if not alive and self._port_serving(base):
            # Something else owns our port -- usually a host model left behind by an
            # earlier run. That is an environment problem, not a dead split: restarting
            # cannot fix it, and degrading would throw away layers that are working. Say
            # so and wait, rather than calling the model dead and shrinking the hive.
            return {"action": "hold", "port_conflict": True,
                    "reason": f"port {self.port} is owned by another process"}
        # Ask rather than trust the verdict recorded at launch: a peripheral that dies
        # mid-session takes the model's ability to serve with it, and a stopped process
        # says nothing about it.
        serving = alive and bool(self._health(base))
        if serving and not state.get("probe"):
            # The launch-time generation failed, so ask again before calling it dead.
            serving = bool(self._probe(base))
        if serving:
            self._failures = 0
            return {"action": "hold"}
        what = "died" if not alive else "cannot serve this split"
        current = {str(k): int(v)
                   for k, v in (state.get("assignments") or {}).items()}
        if self._failures >= self.max_restarts:
            return {"action": "gave_up",
                    "reason": f"host model {what} and restarting did not help"}
        if self._failures == 0:
            return {"action": "restart", "assignments": current,
                    "reason": f"host model {what}"}
        degraded = degrade_split(current, state.get("headroom") or {})
        return {"action": "restart", "assignments": degraded["assignments"],
                "reason": f"host model {what}; {degraded['reason']}"}

    def _relaunch(self, assignments, *, cause: str) -> Dict[str, Any]:
        """Stop what is running, run this exact split, and report what happened.

        Only the dropped devices are released: the peripherals we keep are re-asked by
        ``start()``, which costs one round trip and keeps the release path honest --
        a device that loses its layers must actually stop serving them.
        """
        previous = {str(k): int(v)
                    for k, v in (self._state.get("assignments") or {}).items()}
        keep = {str(device) for device in (assignments or {})}
        launcher, self._launcher = self._launcher, None
        if launcher is not None:
            try:
                launcher.stop()
            except Exception as exc:
                logger.warning("mesh model host stop before relaunch failed: %s", exc)
        for device in sorted(set(previous) - keep):
            self._ask_peripheral(device, "stop")
        self._relaunches += 1
        self._last_restart = self._clock()
        state = self.start(assignments=dict(assignments or {}), cause=cause)
        if state.get("health"):
            self._failures = 0
        else:
            self._failures += 1
        logger.info("mesh model host relaunched (%s): %s", cause, self.summary_line())
        return {"action": "restart", "cause": cause, "failures": self._failures,
                "difference": split_difference(previous, assignments),
                "state": state}

    def _give_up(self, reason: str) -> Dict[str, Any]:
        """Stop trying, keeping the reason the last attempt actually failed for."""
        state = dict(self._state)
        state["mode"] = "failed"
        state["reason"] = "; ".join(
            part for part in (reason, str(self._state.get("reason") or "")) if part)
        return {"action": "gave_up", "reason": state["reason"],
                "state": self._record(state)}

    # -- internals ------------------------------------------------------------
    @staticmethod
    def _refusal_reason(reply) -> str:
        """Why a device did not answer, in its own words when it refused.

        "peripheral did not answer" is true but useless on its own: a peer that refused
        the ask -- because it does not accept us as the lease holder, say -- is a
        different problem from a peripheral that failed to start, and only the peer
        knows which it is.
        """
        if isinstance(reply, dict):
            status = str(reply.get("status") or "").strip().lower()
            message = str(reply.get("message") or "").strip()
            if status and status != "ok" and message:
                return f"peripheral did not answer ({status}: {message[:100]})"
        return "peripheral did not answer"

    def _preflight(self, devices) -> Dict[str, Any]:
        """Wake the candidates and keep only the devices that really answer.

        Reachability is a TCP connect, not a peer's claim -- and it is checked *before*
        anything running is stopped, because a plan that assumes a device will answer
        is how a working split got traded for an unreachable one: the A16 went from 8
        remote layers to 3 when a newly attached Tab never answered.
        """
        endpoints = self.endpoints()
        answered: Dict[str, str] = {}
        unreachable: List[Dict[str, Any]] = []
        for device in sorted({str(d) for d in (devices or []) if str(d)}):
            endpoint = endpoints.get(device, "")
            if not endpoint:
                unreachable.append({"device_id": device, "reason": "no endpoint"})
                continue
            reply = self._ask_peripheral(device, "start")
            if self._wait_reachable(endpoint):
                answered[device] = endpoint
            else:
                unreachable.append({"device_id": device,
                                    "reason": self._refusal_reason(reply)})
        return {"endpoints": answered, "unreachable": unreachable}

    @staticmethod
    def _merge_unreachable(existing, incoming) -> List[Dict[str, Any]]:
        """De-duplicate by device, so the reported list cannot grow without bound."""
        merged: Dict[str, Dict[str, Any]] = {}
        for item in list(existing or []) + list(incoming or []):
            if not isinstance(item, dict) or not item.get("device_id"):
                continue
            device = str(item["device_id"])
            merged[device] = {"device_id": device,
                              "reason": str(item.get("reason") or "")}
        return [merged[key] for key in sorted(merged)]

    def _hold_unreachable(self, state, plan, preflight) -> Dict[str, Any]:
        """Report a candidate that failed pre-flight, leaving the model alone.

        The running split is still the best plan -- the only thing that changed was a
        device that never answered -- so reloading would cost the KV cache and buy
        nothing.
        """
        held = dict(state)
        held["unreachable"] = self._merge_unreachable(
            state.get("unreachable"), preflight.get("unreachable"))
        held["skipped"] = plan.get("skipped") or held.get("skipped") or []
        names = ", ".join(str(item.get("device_id")) for item
                          in preflight.get("unreachable") or [])
        held["reason"] = (f"kept the running split: {names} did not answer"
                          if names else "kept the running split")
        self._record(held)
        return {"action": "hold", "reason": held["reason"],
                "unreachable": preflight.get("unreachable") or []}

    def _note_thermal_detaches(self, difference, plan) -> None:
        """Remember a device the plan dropped for heat, so it is not re-offered.

        ``THERMAL_REFUSE_STATUS`` used to be only a planning rule. A device that goes
        critical *while holding layers* has to give them up, and one that has just
        shed them must not be handed them straight back because its next
        advertisement still looks cool.
        """
        reasons = self._plan_reasons(plan)
        for device in difference.get("detach") or []:
            why = str(reasons.get(str(device)) or "")
            if why.startswith("thermal_status="):
                self._pin(str(device), why)

    def _pin(self, device: str, reason: str, *, seconds=None) -> None:
        """Hold a device out of the plan for a while, with the reason it is held."""
        if not device:
            return
        hold = self.thermal_pin_s if seconds is None else float(seconds)
        self._refused[device] = {"reason": reason,
                                 "until": self._clock() + max(0.0, hold)}
        logger.info("mesh model host: holding %s out of the plan for %gs (%s)",
                    device, hold, reason)

    def _apply_pins(self, plan) -> Dict[str, Any]:
        """Hold pinned devices out of a plan, with the reason they are being held."""
        now = self._clock()
        for device in [d for d, info in self._refused.items()
                       if float(info.get("until") or 0) <= now]:
            self._refused.pop(device, None)
        if not self._refused:
            return plan
        assignments = dict(plan.get("assignments") or {})
        for device in sorted(set(assignments) & set(self._refused)):
            assignments.pop(device, None)
            held = str(self._refused[device].get("reason") or "thermal")
            plan.setdefault("skipped", []).append(
                {"device_id": device, "reason": f"held out: {held}"})
        plan["assignments"] = assignments
        plan["remote_layers"] = sum(assignments.values())
        total = int(plan.get("total_layers") or self.total_layers)
        plan["local_layers"] = total - plan["remote_layers"]
        return plan

    def _difference_reason(self, difference, plan) -> str:
        """Why this rebalance, naming the devices and the plan's own reason."""
        reasons = self._plan_reasons(plan)
        parts = [f"detached {device} ({reasons.get(str(device)) or 'gone'})"
                 for device in difference.get("detach") or []]
        parts += [f"attached {device}" for device in difference.get("attach") or []]
        parts += [f"resized {device}" for device in difference.get("resize") or []]
        return "; ".join(parts) or "rebalance"

    @staticmethod
    def _plan_reasons(plan) -> Dict[str, str]:
        """A plan's own explanation, by device."""
        return {str(item.get("device_id")): str(item.get("reason"))
                for item in (plan.get("skipped") or []) if isinstance(item, dict)}

    @staticmethod
    def _note(state: Dict[str, Any], text: str) -> None:
        """Append to a state's reason rather than replacing it.

        The first reason is usually the true one -- a device was detached, or the port
        was already taken -- and a later failure must not erase it.
        """
        existing = str(state.get("reason") or "")
        state["reason"] = "; ".join(part for part in (existing, text) if part)

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
        # Someone is out there -- either configured or already heard from -- so the
        # rest of the hive may still be arriving. Discovery fills the peer map over
        # time, which is exactly the window the phones were missing from.
        return bool(self.fleet_peers()) or bool(self.live_peers())

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
        deadline = self._clock() + max(0.0, self.reach_timeout)
        while True:
            if endpoint_reachable(endpoint, connect=self._connect):
                return True
            if self._clock() >= deadline:
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
        deadline = self._clock() + max(0.0, self.health_timeout)
        healthy = False
        while not healthy and self._clock() < deadline:
            healthy = self._health(base)
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
                               "unreachable", "reason", "health", "probe",
                               "relaunches")})
            except Exception:
                pass
        return dict(self._state)

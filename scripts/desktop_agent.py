#!/usr/bin/env python3
"""Headless host node: full ShugoCore agent + persistent Tier 2 + the mesh.

One entry point for every non-Android node in a fleet (Windows desktop, macOS
laptop, or Termux if you prefer Python to the app shell), so a mesh can be
stood up without the Android build:

    python scripts/desktop_agent.py --device-caps desktop \
        --data-dir runtime/desktop \
        --peer shugo-mac=192.168.1.60:9000 --peer shugo-a51=192.168.1.61:9000

Passing ``--deploy-target <adb-serial>`` (repeatable) additionally gives this
node the fleet-rollout capability (``fleet_deploy`` / ``fleet_status``) over the
ADB link the devices already use -- USB or the wireless-debugging transport.
It stays disabled otherwise, and the handler refuses every rollout while its
allowlist is empty, so a node cannot deploy anything by accident.

The advertised mesh id is ``shugo-<device-caps>`` (the README's ``shugo-mac`` /
``shugo-a51`` convention), and that is the id the *other* nodes list as their
peer.

Configuration is forwarded to the agent's own bootstrap, which reads
``SHUGOCORE_MESH_PORT`` / ``SHUGOCORE_MESH_PEERS`` / ``SHUGOCORE_MESH_TOKEN``,
so setting those in the environment works with no flags at all (Android nodes
use ``mesh_peers.json`` instead).

The loop is the same ``agent.tick()`` the Android app drives, so the governor,
policy gates, consent/approval, audit chain and memory consolidation are all
live. Tier 2 lives in ``<data-dir>/semantic_memory.db``, which is what peers
pull over ``sync`` -- ``--seed-fact`` puts knowledge there so a freshly started
node is not empty when another device tests the mesh against it.

Election identity is ``shugo-<device-caps>`` with ``--mesh-priority`` (lower
wins the primary lease; Android custodians default to 500). Heartbeats ride the
ShugoNet mesh itself, so a Python-only fleet elects a real primary -- and a node
that *restarts* is handled: its counter starts over and the receiver renews the
lease instead of going blind to that peer until it is restarted too.

Builds travel the mesh as well: ``--share-dir`` is what this node offers to peers
(bare file names only), ``--artifact-dir`` is where what they send lands, and
``--artifact-fetch NAME=PEER`` / ``--artifact-offer NAME=PEER`` ship or receive a
build at startup -- the same command a new laptop or the Mac runs to pick up the
current APK from the hub. Every transfer is chunked and digest-verified on both
ends, and the mesh shared secret is required (``--token``,
``SHUGOCORE_MESH_TOKEN`` or ``<data-dir>/mesh_token.txt``): a token-gated node
refuses every frame without it.

Ctrl+C shuts down cleanly (mesh socket, memory worker, consolidation).
"""
import argparse
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shugocore_agent import create_agent  # noqa: E402
from node_identity import load_or_create as _load_identity  # noqa: E402
from persona import PersonaShaper, _chat_endpoint as _persona_endpoint  # noqa: E402
from capabilities import KNOWN as KNOWN_CAPABILITIES, CapabilityMap  # noqa: E402
from fleet_deploy import (  # noqa: E402
    FleetDeployHandler,
    SubprocessAdbRunner,
    register_fleet_handlers,
)

log = logging.getLogger("desktop_agent")

_STOP = False


def _slug(text: str) -> str:
    """Bounded, filename-safe device slug ('shugo-<slug>' is the mesh id)."""
    out = "".join(c if (c.isalnum() or c in "-_") else "-"
                  for c in str(text).lower())
    return (out.strip("-") or "node")[:32]


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Headless ShugoCore host node (agent + memory + mesh)")
    ap.add_argument("--device-caps", default=None,
                    help="short slug; the mesh advertises 'shugo-<slug>' "
                         "(default: this hostname)")
    ap.add_argument("--data-dir", default="runtime/host",
                    help="persistent dir (semantic_memory.db, audit chain) "
                         "relative to the repo root unless absolute "
                         "(default: runtime/host)")
    ap.add_argument("--api-url", default="http://127.0.0.1:11434",
                    help="model backend base URL (default: local Ollama)")
    ap.add_argument("--model", default=None,
                    help="model id to reason with (default: the engine's "
                         "built-in 'shugocore-local'). Host backends must "
                         "name a model they actually serve: the built-in id "
                         "is an on-device placeholder, and a backend that "
                         "does not know it answers 404, which surfaced only "
                         "as a rule_fallback decision every cycle")
    ap.add_argument("--port", type=int, default=None,
                    help="mesh listen port (default: 9000 or "
                         "SHUGOCORE_MESH_PORT)")
    ap.add_argument("--peer", action="append", default=[],
                    metavar="ID=HOST:PORT",
                    help="peer to dial; repeatable")
    ap.add_argument("--token", default=None,
                    help="mesh shared secret (default: SHUGOCORE_MESH_TOKEN)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between agent ticks (default: 1.0)")
    ap.add_argument("--status-every", type=float, default=60.0,
                    help="seconds between status lines; 0 disables "
                         "(default: 60)")
    ap.add_argument("--seed-fact", action="append", default=[],
                    metavar="TEXT",
                    help="write a Tier 2 fact before serving (repeatable)")
    ap.add_argument("--sync", action="append", default=[], metavar="PEER",
                    help="pull the named peer's Tier 2 (repeatable; 'all' = "
                         "every configured peer). With --sync-interval, also "
                         "keeps pulling on that interval instead of once at "
                         "startup")
    ap.add_argument("--sync-interval", type=float, default=0.0,
                    help="seconds between mesh syncs; 0 = only the one-shot "
                         "startup pull (default: 0). A node that only syncs "
                         "at boot never sees a peer's later facts, so its "
                         "shared-fact counts and the UI's mesh numbers go "
                         "stale for the life of the process")
    ap.add_argument("--mesh-priority", type=int, default=10,
                    help="election priority: LOWER wins the primary lease "
                         "(Android custodians default to 500, so a host's 10 "
                         "takes the lease) (default: 10)")
    ap.add_argument("--mesh-node-id", default=None,
                    help="election identity (default: 'shugo-<device-caps>'; "
                         "the agent's fallback would be 'android-<caps>')")
    ap.add_argument("--share-dir", default=None,
                    help="directory this node offers over the mesh; peers pull "
                         "build artifacts from it by file name "
                         "(default: <data-dir>/shared)")
    ap.add_argument("--artifact-dir", default=None,
                    help="directory where builds pulled from peers land "
                         "(default: <data-dir>/artifacts)")
    ap.add_argument("--artifact-fetch", action="append", default=[],
                    metavar="NAME=PEER",
                    help="pull one artifact from a peer at startup "
                         "(repeatable; chunked and digest-verified)")
    ap.add_argument("--artifact-offer", action="append", default=[],
                    metavar="NAME=PEER",
                    help="offer one of my shared artifacts to a peer at "
                         "startup; the peer pulls it (repeatable)")
    ap.add_argument("--say", action="append", default=[], metavar="TEXT[@DEVICE]",
                    help="speak this text through the hive at startup: routed to "
                         "the device closest to the operator, or to @DEVICE when "
                         "forced (repeatable)")
    ap.add_argument("--response-target", default=None,
                    help="force the answering device for --say (same as policy "
                         "response_target)")
    ap.add_argument("--model-host", default=None, metavar="GGUF",
                    help="serve this model with layers spread over the hive "
                         "(layer-split offload; off by default)")
    ap.add_argument("--model-host-binary", default=None,
                    help="llama-server built with -DGGML_RPC=ON")
    ap.add_argument("--model-host-port", type=int, default=8099,
                    help="port the host model listens on (loopback)")
    ap.add_argument("--model-host-layers", type=int, default=0,
                    help="total layers to divide between host and hive")
    ap.add_argument("--model-host-context", type=int, default=2048)
    ap.add_argument("--model-host-threads", type=int, default=0)
    ap.add_argument("--model-host-reserve-mb", type=int, default=192,
                    help="peripheral memory left alone (OS + agent runtime)")
    ap.add_argument("--model-host-lan", action="store_true",
                    help="let peripherals bind their (unauthenticated) RPC "
                         "socket on the network instead of loopback; audited")
    ap.add_argument("--model-host-exclude", action="append", default=[],
                    metavar="DEVICE",
                    help="never offload to this device (repeatable) -- e.g. the "
                         "machine you are working on")
    ap.add_argument("--persona-url", default="", metavar="URL",
                    help="OpenAI-compatible endpoint of the model that phrases what this "
                         "node says (the Mac in this fleet): host:port or a base URL. "
                         "Unset means the node speaks its own draft")
    ap.add_argument("--persona-model", default="persona", metavar="NAME",
                    help="model name to ask the persona endpoint for")
    ap.add_argument("--persona-recheck", type=float, default=60.0, metavar="SECONDS",
                    help="how often to look for the phrasing service when --persona-url "
                         "is 'auto' (0 disables; a service found once is kept even if it "
                         "later goes quiet)")
    ap.add_argument("--persona-timeout", type=float, default=8.0, metavar="SECONDS",
                    help="how long to wait for the persona model before speaking the "
                         "draft instead (style is never worth losing the line)")
    ap.add_argument("--model-host-arg", action="append", default=[],
                    metavar="ARG",
                    help="extra llama.cpp argument for the host model (repeatable), "
                         "e.g. --parallel 4; kept for every relaunch, and read (not "
                         "just forwarded) so the cache each device pays is accounted. "
                         "Give it as --model-host-arg=--parallel when the value itself "
                         "starts with a dash")
    ap.add_argument("--model-host-reconcile", type=float, default=60.0,
                    metavar="SECONDS",
                    help="how often to check the split: restart the host model if it "
                         "stopped serving, and re-plan when a device joins or leaves "
                         "(0 disables; a change must persist across two checks and "
                         "rebalances are at most 5 minutes apart)")
    ap.add_argument("--deploy-target", action="append", default=[],
                    metavar="SERIAL",
                    help="allow ADB deployment to this device serial "
                         "(repeatable; passing at least one enables the "
                         "fleet_deploy capability on this node)")
    ap.add_argument("--deploy-artifact-root", default=None,
                    help="only APKs inside this directory may be deployed "
                         "(default: platforms/android/app/build/outputs/apk)")
    ap.add_argument("--adb", default=None,
                    help="path to the adb binary (default: PATH lookup)")
    return ap.parse_args(argv)


def _install_signals() -> None:
    def _handle(signum, _frame):
        global _STOP
        log.info("signal %s received; finishing the current tick", signum)
        _STOP = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError, AttributeError):
            pass


def _apply_model(agent, model: str) -> str:
    """Point the engine's model registry at ``model``; returns the id in use.

    The engine bootstraps with a hardcoded ``shugocore-local`` id — correct
    on-device, where the local inference server serves that name. On a host
    the backend is Ollama (or an OpenAI-compatible server), which does not
    know that id and answers 404; the agent then reported the same rule-based
    fallback every cycle, so the backend looked healthy while the model was
    never consulted. Re-pointing the registry is the same override the
    desktop UI's ``AgentController._apply_backend`` performs.

    Returns the id actually in force so the caller can log it honestly
    (including when the engine exposes no registry to override).
    """
    model = (model or "").strip()
    if not model:
        return "shugocore-local"
    engine = getattr(agent, "engine", None)
    models = getattr(engine, "models", None) if engine is not None else None
    if not isinstance(models, list) or not models:
        log.warning("engine has no model registry; --model %r not applied",
                    model)
        return model
    models[0]["id"] = model
    backend = models[0].get("backend")
    if isinstance(backend, dict):
        # The android backend client carries its own model name; leaving it
        # stale would send the placeholder over the wire even after the
        # registry id changed.
        backend["model_name"] = model
    cache = getattr(engine, "_backend_cache", None)
    if isinstance(cache, dict):
        cache.clear()
    return model


def _sync_once(runtime, targets, reason: str) -> Dict[str, int]:
    """Pull Tier 2 from each target peer; returns a small outcome tally.

    The mesh only moves memory when a node *asks*, so this is the whole of
    the data path. Per-peer failures are logged and counted rather than
    raised: one unresponsive peer must not abort the others, and the agent
    loop must keep running. ``imported: 0`` is a successful pull that found
    nothing new, which is the common case on a converged fleet and must not
    be reported as an error.
    """
    tally = {"ok": 0, "imported": 0, "failed": 0}
    for peer_id in targets:
        try:
            result = runtime.sync(peer_id) or {}
        except Exception as exc:
            tally["failed"] += 1
            log.warning("sync %s failed (%s): %s", peer_id, reason,
                        f"{type(exc).__name__}: {exc}")
            continue
        status = str(result.get("status", "unknown"))
        if status == "success":
            tally["ok"] += 1
            try:
                tally["imported"] += int(result.get("imported", 0) or 0)
            except (TypeError, ValueError):
                pass
            # Only the interesting half: a pull that changed nothing is noise.
            if int(result.get("imported", 0) or 0) > 0:
                log.info("sync %s -> %s", peer_id, result)
        else:
            tally["failed"] += 1
            log.warning("sync %s -> %s (%s)", peer_id, result, reason)
    return tally


def _status_line(agent, runtime, ticks) -> str:
    try:
        status = agent.get_status() or {}
    except Exception as exc:                      # never kill the loop
        log.warning("get_status failed: %s", exc)
        return f"tick {ticks} | status unavailable ({exc})"
    loop = status.get("loop", {}) if isinstance(status, dict) else {}
    try:
        mesh = runtime.status() or {}
    except Exception as exc:
        log.warning("mesh status failed: %s", exc)
        mesh = {}
    connected = mesh.get("connected_peers", []) or []
    stats = mesh.get("stats", {}) or {}
    election = getattr(agent, "mesh_election", None)
    try:
        lease = election.status() if election is not None else {}
    except Exception as exc:
        log.warning("election status failed: %s", exc)
        lease = {}
    telemetry = getattr(agent, "telemetry", None)
    declared = (telemetry.get("mesh_peers")
                if isinstance(telemetry, dict) else None)
    heard = (mesh.get("heartbeat") or {}).get("heard")
    # `beats` is how many peers have *ever* been heard (a distinct-node count),
    # so it cannot show whether the hive is live right now; `rx` counts every
    # advertisement frame received, which is what proves a peer is still
    # advertising after a redeploy or a peer restart.
    rx = stats.get("heartbeats_received")
    tx = stats.get("heartbeats_sent")
    artifacts = mesh.get("artifacts") or {}
    shared = len(runtime.list_artifacts() or [])
    art_in = artifacts.get("received")
    art_out = artifacts.get("sent")
    routing = (status.get("response_routing") or {}) if isinstance(status, dict) else {}
    delegated = (status.get("delegated") or {}) if isinstance(status, dict) else {}
    # The election's own view: how many peers it considers live, and who it thinks
    # holds the lease. Without this, "the hive has a primary" and "this node sees
    # one" are indistinguishable from the status line.
    try:
        live_peers = list(election.live_peers()) if election is not None else []
    except Exception:
        live_peers = []
    live = len(live_peers)
    # How many live peers say they are followers: the posture is a fleet property, so it
    # belongs on the line that describes the fleet.
    followers = len([peer for peer in live_peers
                     if str(peer.get("role") or "") == "follower"])
    # Who phrases this node's speech: off, the model and node it names, or resolved.
    shaper = getattr(getattr(agent, "engine", None), "persona_shaper", None)
    try:
        persona = str(shaper.label()) if shaper is not None else "off"
    except Exception:
        persona = "configured"
    # Where the hive's services are, resolved from what nodes advertise (capabilities.py).
    # verify=False: a status line must not open sockets.
    try:
        known = CapabilityMap(live_peers, verify=False)
        placement = ",".join(f"{name}@{known.resolve(name)['node']}"
                             for name in KNOWN_CAPABILITIES
                             if known.resolve(name).get("node"))
    except Exception:
        placement = ""
    model_host = getattr(agent, "_model_host", None)
    host_line = (model_host.summary_line() if model_host is not None else "off")
    return (f"tick {ticks} | cycles={loop.get('cycles')} "
            f"rate={loop.get('success_rate')} | node={lease.get('node_id', '?')} "
            f"prio={lease.get('priority', '?')} role={status.get('mesh_role', '?')} "
            f"primary={status.get('mesh_primary')} connected={len(connected)} "
            f"mesh_peers={len(declared or [])} beats={heard} live={live} "
            f"followers={followers} "
            f"rx={rx} tx={tx} "
            f"say_to={routing.get('device')} deleg_sent={delegated.get('sent')} "
            f"artifacts={shared} art_in={art_in} art_out={art_out} "
            f"imported={stats.get('imported')} persona={persona} "
            f"placement={placement} model_host={host_line}")


def _enable_fleet_deploy(agent, args) -> None:
    """Wire the ADB rollout capability when the operator allows targets.

    Off by default and fail-closed in both directions: without a
    ``--deploy-target`` nothing is registered on this node, and the handler
    itself refuses every rollout while its allowlist is empty.
    """
    targets = [t.strip() for t in (args.deploy_target or []) if t.strip()]
    if not targets:
        log.info("fleet deploy disabled (pass --deploy-target SERIAL to allow)")
        return
    engine = getattr(agent, "engine", None)
    layer = getattr(engine, "execution_layer", None)
    if layer is None:
        log.warning("fleet deploy requested but the agent has no execution "
                    "layer; skipping")
        return
    adb = SubprocessAdbRunner(args.adb)
    if not adb.available():
        log.warning("fleet deploy requested but adb is not runnable ('%s'); "
                    "skipping", adb.adb_path)
        return
    root = args.deploy_artifact_root or str(
        REPO_ROOT / "platforms" / "android" / "app" / "build"
        / "outputs" / "apk")
    handler = FleetDeployHandler(adb=adb, allowed_targets=targets,
                                 artifact_root=root,
                                 audit=getattr(engine, "audit", None))
    register_fleet_handlers(layer, handler)
    log.info("fleet deploy enabled: %d target(s) %s, artifact root %s",
             len(targets), ", ".join(targets), root)


def _split_target(spec: str):
    """Parse ``NAME=PEER`` into (name, peer); None when malformed."""
    if "=" not in str(spec):
        return None
    name, _, peer = str(spec).partition("=")
    name, peer = name.strip(), peer.strip()
    if not name or not peer:
        return None
    return name, peer


def _startup_artifacts(runtime, args) -> None:
    """Run the startup build jobs: ``--artifact-fetch`` / ``--artifact-offer``.

    This is how a node ships or receives a build over the mesh -- the operator's
    alternative to an ADB cable, and the path a laptop or the Mac uses to pick up
    the current APK from the hub.
    """
    jobs = ([("fetch", spec) for spec in args.artifact_fetch]
            + [("offer", spec) for spec in args.artifact_offer])
    if not jobs:
        return
    time.sleep(2.0)                          # let the peer dials settle
    for action, spec in jobs:
        target = _split_target(spec)
        if target is None:
            log.warning("ignoring malformed artifact spec %r (want NAME=PEER)",
                        spec)
            continue
        name, peer = target
        try:
            if action == "fetch":
                result = runtime.fetch_artifact(peer, name)
            else:
                result = runtime.offer_artifact(peer, name)
        except Exception as exc:
            log.warning("artifact %s %s failed: %s", action, name, exc)
            continue
        log.info("artifact %s %s <-> %s: %s", action, name, peer, result)


def _startup_model_host(agent, args) -> None:
    """Serve the model with layers on the hive, when the operator asks for it.

    Off by default and fail-closed: without ``--model-host`` nothing is planned and
    nothing is delegated. With it, the orchestrator plans from the fleet's own
    advertised headroom, asks each chosen device to start its RPC peripheral over
    the mesh, launches the same llama-server the agent would use locally, verifies
    it answers, and says where to point the agent.
    """
    if not getattr(args, "model_host", None):
        return
    from mesh_model_host import MeshModelHost  # noqa: WPS433

    host = MeshModelHost(
        args.model_host, agent=agent, binary=args.model_host_binary or None,
        port=args.model_host_port, total_layers=args.model_host_layers,
        context=args.model_host_context, threads=args.model_host_threads,
        reserve_bytes=int(args.model_host_reserve_mb) * 1024 * 1024,
        allow_lan=bool(args.model_host_lan),
        exclude=args.model_host_exclude,
        extra_args=args.model_host_arg,
        peers=lambda: agent.mesh_peer_endpoints())
    agent._model_host = host
    state = host.start()
    log.info("model host: %s", host.summary_line())
    if state.get("mode") == "split":
        log.info("point the agent at it with --api-url http://127.0.0.1:%s",
                 state.get("host_port"))

def _phrase_line(agent, text: str) -> str:
    """Shape one spoken line with the persona model, if this node has one.

    Uses the decision engine's own step rather than a second implementation, so an
    operator's line gets exactly the treatment a decided one gets: speech only, before
    anything gates it, and the draft kept when the phrasing node cannot be reached.
    """
    engine = getattr(agent, "engine", None)
    shape = getattr(engine, "_apply_persona", None)
    if not callable(shape):
        return text
    decision = {"action_type": "speak", "params": {"text": text}}
    try:
        decision = shape(decision, {"type": "operator_say"}, False)
    except Exception as exc:
        log.warning("persona phrasing skipped: %s", exc)
        return text
    persona = decision.get("persona") if isinstance(decision, dict) else {}
    persona = persona if isinstance(persona, dict) else {}
    spoken = str((decision.get("params") or {}).get("text") or text)
    if persona.get("source") == "persona":
        log.info("persona %s phrased the operator's line: %r -> %r",
                 persona.get("label"), text, spoken)
    elif persona.get("source") == "unavailable":
        log.info("persona unavailable; speaking the operator's line as typed: %s",
                 persona.get("reason"))
    return spoken


def _apply_persona_instructions(agent, shaper) -> None:
    """Hand the phrasing model the character text the primary owns.

    Called whenever the shaper *becomes* usable, not only at startup: the service is
    normally resolved from the fleet a little later (the phrasing node may be
    mid-restart when this one boots), and a shaper enabled late was otherwise phrased
    with no character at all. Idempotent.
    """
    if getattr(shaper, "instructions", ""):
        return
    try:
        # The personality text stays the primary's: the phrasing node is handed what to
        # sound like, never the authority to decide what is said.
        from personality.prompt import personality_system_prompt
        shaper.instructions = personality_system_prompt(agent.personality)
    except Exception as exc:
        log.warning("persona instructions unavailable: %s", exc)


def _peer_hosts(agent) -> Dict[str, str]:
    """``node_id -> the address its peers dial``, from the mesh peer map.

    A locator that says "here" (loopback) only means something read alongside the address
    that node is actually reached at.
    """
    try:
        return {str(entry[0]): str(entry[1])
                for entry in (agent.mesh_peer_endpoints() or [])
                if len(entry) >= 2 and entry[0] and entry[1]}
    except Exception:
        return {}


def _resolve_persona(agent, shaper, *, announce: bool = False) -> bool:
    """Point the shaper at whichever live peer advertises the phrasing service.

    Called at startup *and* on a cadence, because a service that appears a moment after
    this node boots is the normal case rather than the exception -- the Mac may be
    mid-restart when the PC starts. A resolution that finds nobody leaves the shaper as it
    is: a service found once and then gone quiet is better retried (and failed open) than
    forgotten. Returns True when the locator changed.
    """
    try:
        peers = list(agent.mesh_election.live_peers())
    except Exception:
        peers = []
    resolved = CapabilityMap(peers, verify=True, hosts=_peer_hosts(agent)).resolve("persona")
    locator = str(resolved.get("locator") or "")
    if locator:
        endpoint = _persona_endpoint(locator)
        if endpoint != shaper.url:
            # Normalised the same way an explicit URL is, so both paths behave alike.
            shaper.url = endpoint
            _apply_persona_instructions(agent, shaper)      # newly usable: give it a voice
            log.info("persona resolved from the fleet: %s (%s)",
                     shaper.label(), resolved["reason"])
            return True
        if announce:
            log.info("persona already points at %s (%s)", shaper.label(),
                     resolved["reason"])
        return False
    if announce:
        log.info("persona=auto but nobody offers it: %s", resolved.get("reason"))
    return False


def _startup_say(agent, args) -> None:
    """Speak once through the hive: routed, or forced to a named device.

    This is the operator-facing form of the routing rule -- the primary decides
    the words, the device closest to the operator says them -- and with @DEVICE
    it also exercises the delegated path deterministically (useful on a bench
    where nobody is standing in front of a camera).
    """
    if not getattr(args, "say", None):
        return
    if args.response_target:
        policy = getattr(agent, "policy", None)
        if isinstance(policy, dict):
            policy["response_target"] = args.response_target
        log.info("response target forced to %s", args.response_target)
    time.sleep(2.0)                          # let the peer dials settle
    # A delegated action is judged against the *receiver's* election, which holds
    # its previous lease holder until a heartbeat or two after a restart. Wait
    # until this node is seen as primary by a live peer before addressing one --
    # otherwise the first delegation of a fresh start is refused as "not the
    # primary", which is exactly the transient we kept hitting.
    #
    # A *routed* answer needs one more thing: the observation must list the peers,
    # because the router scores their facts. A node that just booted has an
    # election verdict before its first observation, so the first routed
    # utterance used to answer "no device reports speech output" while the phones
    # were plainly able to speak.
    routed = [spec for spec in args.say if "@" not in str(spec)]
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        try:
            election = getattr(agent, "mesh_election", None)
            settled = (election is not None
                       and str(election.tick().get("primary") or "")
                       == str(agent.node_id)
                       and len(election.live_peers()) >= 1)
            if settled and routed:
                candidates = agent._response_candidates()
                settled = any(not c.get("is_self") for c in candidates)
        except Exception:
            settled = False
        if settled:
            break
        time.sleep(1.0)
    else:
        log.warning("hive has not settled on this node as primary; speaking anyway")
    # An operator's line spoken at startup should be phrased like any other, and the
    # phrasing service is normally resolved a moment *after* this node booted -- so ask
    # once more now that the fleet is known, before anything is said. A no-op for a shaper
    # that is explicitly configured or already enabled.
    shaper = getattr(getattr(agent, "engine", None), "persona_shaper", None)
    if shaper is not None and not getattr(shaper, "enabled", False):
        try:
            _resolve_persona(agent, shaper)
        except Exception as exc:
            log.warning("persona resolve before say failed: %s", exc)
    for spec in args.say:
        text, _, device = str(spec).partition("@")
        text, device = text.strip(), device.strip()
        if not text:
            continue
        # An operator's line is phrased like any other line the hive speaks.
        text = _phrase_line(agent, text)
        if device:
            result = agent._mesh_delegate(device, {
                "action_type": "speak", "params": {"text": text}})
            result = dict(result or {}, delegated_to=device, forced=True)
        else:
            result = agent._route_response("speak", {"text": text})
        log.info("say %r -> %s", text, result or "answered locally")
        if result is None and not device:
            chosen, why = agent.select_response_node()
            log.info("no device to speak through: %s", why)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    _install_signals()

    caps = _slug(args.device_caps or socket.gethostname())
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = REPO_ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    if args.port is not None:
        os.environ["SHUGOCORE_MESH_PORT"] = str(args.port)
    if args.peer:
        os.environ["SHUGOCORE_MESH_PEERS"] = ",".join(args.peer)
    if args.token:
        os.environ["SHUGOCORE_MESH_TOKEN"] = args.token
    if args.share_dir:
        os.environ["SHUGOCORE_ARTIFACT_ROOT"] = str(args.share_dir)
    if args.artifact_dir:
        os.environ["SHUGOCORE_ARTIFACT_DIR"] = str(args.artifact_dir)

    mesh_id = args.mesh_node_id or _load_identity(str(data_dir), caps=caps)
    log.info("booting node: mesh id '%s' (election prio %s), data dir %s",
             mesh_id, args.mesh_priority, data_dir)
    # Phrasing is a hive service (persona.py): the primary owns the words *and* the gate,
    # a model on another node owns the wording. Unset by default, so a node without one
    # behaves exactly as it did. "auto" resolves it from the fleet instead of naming a
    # node: whoever advertises the capability gets asked (capabilities.py).
    auto_persona = str(args.persona_url).strip().lower() == "auto"
    shaper = PersonaShaper("" if auto_persona else args.persona_url, args.persona_model,
                           timeout=args.persona_timeout)
    agent = create_agent(device_caps=caps, api_url=args.api_url,
                         data_dir=str(data_dir), mesh_node_id=mesh_id,
                         mesh_priority=args.mesh_priority,
                         persona_shaper=shaper)
    if auto_persona:
        # First attempt now; the loop retries, because the phrasing service may simply not
        # have heartbeated yet (which is exactly what a fresh boot looks like).
        _resolve_persona(agent, shaper, announce=True)
    if shaper.enabled:
        _apply_persona_instructions(agent, shaper)
        log.info("persona: %s (timeout %ss)", shaper.label(), shaper.timeout)
    # AndroidAgent.__init__ already bootstraps, so it has already started the
    # mesh using the environment set above. Bootstrapping again would re-enter
    # _start_shugonet(), which nulls shugonet_runtime before its "already
    # running" early return -- leaving this handle dead while the original
    # runtime keeps serving. Only bootstrap if the factory did not.
    if getattr(agent, "engine", None) is None:
        log.info("factory did not bootstrap the agent; bootstrapping now")
        agent._bootstrap()
    # Must run before the tick loop: the engine is already built here, and
    # every cycle until this lands asks the backend for 'shugocore-local'.
    model_in_use = _apply_model(agent, args.model)
    log.info("reasoning model: %s (backend %s)", model_in_use, args.api_url)

    runtime = getattr(agent, "shugonet_runtime", None)
    if runtime is None:
        log.error("mesh runtime not started (started=%r) - check the "
                  "SHUGOCORE_MESH_* settings and whether port %s is free",
                  getattr(agent, "_shugonet_started", None),
                  os.environ.get("SHUGOCORE_MESH_PORT", "9000"))
        return 1
    log.info("mesh listening on port %s (peers=%r)",
             os.environ.get("SHUGOCORE_MESH_PORT", "9000"),
             os.environ.get("SHUGOCORE_MESH_PEERS", ""))
    # The effective peer set is env + <data-dir>/mesh_peers.json, merged by the
    # agent. Log what was actually registered: "configured" is what we will
    # dial (and retry), "connected" is what is up right now.
    configured = sorted(getattr(runtime, "_outbound", {}) or {})
    log.info("mesh peers configured (%s): %s", len(configured),
             ", ".join(configured) or "none")

    memory = getattr(agent, "memory", None)
    tier2 = getattr(memory, "tier2", None)
    seeded = 0
    for text in args.seed_fact:
        try:
            tier2.store_fact(text)
            seeded += 1
        except Exception as exc:
            log.warning("seed fact failed (%r): %s", text, exc)
    if args.seed_fact:
        log.info("seeded %s/%s Tier 2 fact(s)", seeded, len(args.seed_fact))

    if args.sync:
        # The mesh only moves memory when a node *asks*: pull once at startup
        # (the Android equivalent is "sync your memory with your peer").
        peers = list(getattr(runtime, "_outbound", {}) or {})
        targets = peers if "all" in args.sync else [p for p in args.sync
                                                    if p != "all"]
        time.sleep(2.0)                     # let the peer dials settle
        log.info("startup sync: %d peer(s)", len(targets))
        _sync_once(runtime, targets, "startup")

    _enable_fleet_deploy(agent, args)
    _startup_artifacts(runtime, args)
    _startup_model_host(agent, args)
    _startup_say(agent, args)

    ticks = 0
    next_status = (time.monotonic() + args.status_every
                   if args.status_every and args.status_every > 0 else None)
    # Continuous sync: resolved once, from the same peer set the startup pull
    # used. Re-reading the peers each round would pick up a dial that has not
    # connected yet and spam a not-yet-listening peer with timeouts.
    sync_peers = []
    if args.sync_interval and args.sync_interval > 0:
        configured = list(getattr(runtime, "_outbound", {}) or {})
        sync_peers = configured if "all" in args.sync else [
            p for p in args.sync if p != "all"]
        if sync_peers:
            log.info("continuous mesh sync every %gs across %d peer(s): %s",
                     args.sync_interval, len(sync_peers), ", ".join(sync_peers))
        else:
            log.warning("--sync-interval %gs set but no --sync peers selected; "
                        "continuous sync disabled", args.sync_interval)
    next_sync = (time.monotonic() + args.sync_interval
                 if sync_peers else None)
    # The host model is watched on a cadence of its own: a model that stopped serving
    # has to come back, and a fleet that has changed shape has to be re-planned rather
    # than staying as it was when the node booted.
    host = getattr(agent, "_model_host", None)
    reconcile_every = float(getattr(args, "model_host_reconcile", 0) or 0)
    next_reconcile = (time.monotonic() + reconcile_every
                      if host is not None and reconcile_every > 0 else None)
    # The phrasing service is looked for again on its own cadence: it may appear, or move,
    # long after this node booted.
    persona_recheck = float(getattr(args, "persona_recheck", 0) or 0)
    next_persona = (time.monotonic() + persona_recheck
                    if auto_persona and persona_recheck > 0 else None)
    try:
        while not _STOP:
            agent.tick()
            ticks += 1
            if next_sync is not None and time.monotonic() >= next_sync:
                # On the agent thread: a sync is a short request/response, and
                # blocking here keeps it serialized with tick() so two model
                # calls and a sync never contend for the same backend.
                _sync_once(runtime, sync_peers, "periodic")
                next_sync = time.monotonic() + args.sync_interval
            if next_reconcile is not None and time.monotonic() >= next_reconcile:
                # On the agent thread, like the sync: a relaunch stops and starts the
                # model, so it must not overlap a model call.
                try:
                    result = host.reconcile()
                    host.note_reconcile(result.get("action"))
                    if result.get("action") not in (None, "hold", "deferred"):
                        log.info("model host %s: %s", result.get("action"),
                                 host.summary_line())
                    elif result.get("action") == "deferred":
                        log.debug("model host reconcile deferred: %s",
                                  result.get("reason"))
                except Exception as exc:
                    log.warning("model host reconcile failed: %s", exc)
                next_reconcile = time.monotonic() + reconcile_every
            if next_persona is not None and time.monotonic() >= next_persona:
                try:
                    _resolve_persona(agent, shaper)
                except Exception as exc:
                    log.warning("persona re-resolve failed: %s", exc)
                next_persona = time.monotonic() + persona_recheck
            if next_status is not None and time.monotonic() >= next_status:
                log.info("%s", _status_line(agent, runtime, ticks))
                next_status = time.monotonic() + args.status_every
            if args.interval > 0:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("shutting down (%s ticks): %s", ticks,
                 _status_line(agent, runtime, ticks))
        for step, fn in (("model host stop", getattr(host, "stop", None)),
                         ("mesh stop", getattr(runtime, "stop", None)),
                         ("agent cleanup", getattr(agent, "cleanup", None))):
            if not callable(fn):
                continue
            try:
                fn()
            except Exception as exc:
                log.warning("%s failed: %s", step, exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

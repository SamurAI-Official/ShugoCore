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
wins the primary lease; Android custodians default to 500). Note that peer
*heartbeats* are published by the Android Kotlin layer over DDS
(``DeviceMeshManager`` -> ``update_mesh_peers``); the ShugoNet TCP mesh carries
no heartbeat/health message yet, so a Python-only fleet serves memory normally
but honestly reports ``role=standalone`` on every node.

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

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shugocore_agent import create_agent  # noqa: E402
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
                    help="pull the named peer's Tier 2 once at startup "
                         "(repeatable; 'all' = every configured peer)")
    ap.add_argument("--mesh-priority", type=int, default=10,
                    help="election priority: LOWER wins the primary lease "
                         "(Android custodians default to 500, so a host's 10 "
                         "takes the lease) (default: 10)")
    ap.add_argument("--mesh-node-id", default=None,
                    help="election identity (default: 'shugo-<device-caps>'; "
                         "the agent's fallback would be 'android-<caps>')")
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
    return (f"tick {ticks} | cycles={loop.get('cycles')} "
            f"rate={loop.get('success_rate')} | node={lease.get('node_id', '?')} "
            f"prio={lease.get('priority', '?')} role={status.get('mesh_role', '?')} "
            f"primary={status.get('mesh_primary')} connected={len(connected)} "
            f"imported={stats.get('imported')}")


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

    mesh_id = args.mesh_node_id or f"shugo-{caps}"
    log.info("booting node: mesh id '%s' (election prio %s), data dir %s",
             mesh_id, args.mesh_priority, data_dir)
    agent = create_agent(device_caps=caps, api_url=args.api_url,
                         data_dir=str(data_dir), mesh_node_id=mesh_id,
                         mesh_priority=args.mesh_priority)
    # AndroidAgent.__init__ already bootstraps, so it has already started the
    # mesh using the environment set above. Bootstrapping again would re-enter
    # _start_shugonet(), which nulls shugonet_runtime before its "already
    # running" early return -- leaving this handle dead while the original
    # runtime keeps serving. Only bootstrap if the factory did not.
    if getattr(agent, "engine", None) is None:
        log.info("factory did not bootstrap the agent; bootstrapping now")
        agent._bootstrap()

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
        for peer_id in targets:
            try:
                result = runtime.sync(peer_id)
                log.info("sync %s -> %s", peer_id, result)
            except Exception as exc:
                log.warning("sync %s failed: %s", peer_id, exc)

    _enable_fleet_deploy(agent, args)

    ticks = 0
    next_status = (time.monotonic() + args.status_every
                   if args.status_every and args.status_every > 0 else None)
    try:
        while not _STOP:
            agent.tick()
            ticks += 1
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
        for step, fn in (("mesh stop", getattr(runtime, "stop", None)),
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

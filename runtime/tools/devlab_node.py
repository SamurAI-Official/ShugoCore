#!/usr/bin/env python3
"""Run one bare node for the dev lab, so a hub and a peer can be two real processes.

``dev_task`` needs a peer: a node that is *not* the one asking, reachable over the same
transport a phone or the Mac uses. Two nodes on one machine, over loopback, each with its own
data dir and the fleet's token, are a real mesh -- the frames, the heartbeat, the lease and
the delegation all take the path they take on the LAN, and nothing is stubbed.

    python runtime/tools/devlab_node.py --port 9001 --device-caps devlab --priority 500 \\
        --data-dir runtime/devlab --peers shugo-desktop=127.0.0.1:9000 --seconds 120

It prints one line per delegated task it runs, taken from its *own* audit chain, and those
lines are the evidence: they come from the peer's process, not from the hub's word for it.
"""
import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CLIENTS = ROOT / "clients" / "desktop"
if str(CLIENTS) not in sys.path:
    sys.path.insert(0, str(CLIENTS))

DEVTASK_EVENTS = ("dev_task_started", "dev_task_finished", "dev_task_refused")
LOG_CATEGORIES = ("MESH", "FLEET", "AGENT")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=9001)
    ap.add_argument("--device-caps", default="devlab")
    ap.add_argument("--priority", type=int, default=500)
    ap.add_argument("--data-dir", default=os.path.join("runtime", "devlab"))
    ap.add_argument("--peers", default="")
    ap.add_argument("--token", default="")
    ap.add_argument("--seconds", type=float, default=120.0)
    return ap.parse_args(argv)


def read_new_events(path, seen: int):
    """The node's own audit entries we have not printed yet, and the new offset."""
    events = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except Exception:
        return events, seen
    for line in lines[seen:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        kind = str(entry.get("event_type") or entry.get("event")
                   or entry.get("type") or "")
        if kind in DEVTASK_EVENTS:
            events.append((kind, entry))
    return events, len(lines)


def listening(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    """True when something on this machine accepts a connection on ``port``.

    The runtime reports a started server whether or not the bind succeeded, so a node can
    believe it is reachable while the port belongs to another process -- and then a hub's
    ``send`` succeeds at the socket level while this node receives nothing, which looks exactly
    like a refusal. Asking the port directly is the only honest answer.
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def state_line(controller) -> dict:
    """This node's own transport view, for the periodic state line.

    ``received`` counts frames this node's listener accepted, so it separates "the hub's frame
    never arrived" from "it arrived and we did not act on it" -- a difference the sender cannot
    see, because all it knows is what its own socket did.
    """
    try:
        snapshot = controller.snapshot() or {}
    except Exception:
        snapshot = {}
    mesh = snapshot.get("mesh") or {}
    stats = mesh.get("stats") or {}
    heartbeat = mesh.get("heartbeat") or {}
    peers = [str(name) for name in (mesh.get("connected_peers") or [])]
    role = ""
    primary = ""
    try:
        status = controller.agent.get_status() or {}
        role = str(status.get("mesh_role") or "")
    except Exception:
        role = ""
    try:
        primary = str(controller.agent.mesh_election.primary() or "")
    except Exception:
        primary = ""
    return {"received": stats.get("received"), "sent": stats.get("sent"),
            "errors": stats.get("errors"), "heartbeats_rx": heartbeat.get("rx"),
            "heartbeats_tx": heartbeat.get("tx"), "connected_peers": peers,
            "mesh_role": role, "election_primary": primary}


def read_new_logs(snapshot: dict, seen: int) -> list:
    """The node's own LogBus entries we have not printed yet.

    The agent's ``log()`` goes to the LogBus (in memory, for the UI), so without this the
    single most informative line a peer produces -- "refused delegated action from X: ..." --
    would exist only inside that process and never reach an operator's terminal.
    """
    lines = []
    for entry in (snapshot or {}).get("logs") or []:
        try:
            seq = int(entry.get("seq") or 0)
        except (TypeError, ValueError):
            continue
        if seq <= seen:
            continue
        seen = max(seen, seq)
        category = str(entry.get("category") or "")
        message = str(entry.get("message") or "")
        # Skip the per-cycle heartbeat of the node's own loop: it is honest but it buries the
        # one line that matters ("refused delegated action from X: ...") in 40 others.
        if category in LOG_CATEGORIES and not message.startswith("cycle="):
            lines.append(f"[{category}] {message}")
    return lines, seen


def main(argv=None) -> int:
    args = parse_args(argv)
    import logging

    import shugocore_desktop as console          # the node the operator terminal builds

    spec = console.backend_by_label("Stub (offline)")
    controller = console.AgentController(interval=1.0, sync_interval=0.0)
    mesh = {"port": args.port, "peers": args.peers, "priority": args.priority,
            "token": args.token or os.environ.get("SHUGOCORE_MESH_TOKEN", "")}
    error = controller.start(spec, spec.get("url", ""), "", data_dir=args.data_dir,
                             device_caps=args.device_caps, mesh=mesh) or ""
    if error:
        print(f"[devlab] node did not start: {error}", flush=True)
        return 1
    logging.getLogger().setLevel(logging.WARNING)
    # One clean line per task, so the peer's own record of what it ran is greppable without
    # the node's decision chatter around it.
    logging.getLogger("dev_tasks").setLevel(logging.INFO)
    node_id = str(getattr(controller.agent, "node_id", "") or args.device_caps)
    audit_path = os.path.join(args.data_dir, "audit_chain.jsonl")
    if listening(args.port):
        print(f"[devlab] listening on 127.0.0.1:{args.port}", flush=True)
    else:
        print(f"[devlab] ERROR nothing is listening on 127.0.0.1:{args.port} "
              f"(another process may hold the port)", flush=True)
    print(f"[devlab] node {node_id} up on port {args.port} "
          f"(peers='{args.peers}', audit={audit_path})", flush=True)
    seen = 0
    seen_logs = 0
    next_state = 0.0
    deadline = time.monotonic() + max(1.0, args.seconds)
    try:
        while time.monotonic() < deadline:
            time.sleep(0.5)
            events, seen = read_new_events(audit_path, seen)
            for kind, entry in events:
                print(f"[devlab-audit] {kind} {json.dumps(entry, sort_keys=True)}",
                      flush=True)
            try:
                snapshot = controller.snapshot() or {}
            except Exception:
                snapshot = {}
            lines, seen_logs = read_new_logs(snapshot, seen_logs)
            for line in lines:
                print(f"[devlab-log] {line}", flush=True)
            if time.monotonic() >= next_state:
                # This node's own transport counters: the difference between "the hub's frame
                # never arrived" and "it arrived and we did not act on it" is otherwise
                # invisible from the sender, which only knows what its socket did.
                next_state = time.monotonic() + 5.0
                print(f"[devlab-state] {json.dumps(state_line(controller), sort_keys=True)}",
                      flush=True)
    finally:
        try:
            controller.stop()
        except Exception:
            pass
        print(f"[devlab] node {node_id} down", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

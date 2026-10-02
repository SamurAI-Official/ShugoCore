#!/usr/bin/env python3
"""Bring the desk node up, listen to the hive, and write what it saw.

The matrix can only judge the mesh claims if something records a fleet. This is that
recorder: it builds the same node the operator terminal does (the same ``AgentController``,
so the mesh transport, the identity and the token are the real ones), holds the lease the way
a hub is supposed to, syncs memory with each peer so the imported counter is *earned* rather
than assumed, and writes a transcript in the shape the existing parsers read:

    python runtime/tools/fleet_status.py --seconds 45 --sync-interval 15

The output (``runtime/evidence/fleet_status.txt`` by default) carries a hub line
(``role=primary``) and a memory line (``imported=N``) that ``claim_matrix`` judges, plus a row
per peer. Nothing is invented: if the fleet is quiet the file says so, if the sync imported
nothing it says ``imported=0``, and the claim then stays unproven -- which is the honest
answer, and the reason this exists rather than a hand-written status line.
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CLIENTS = ROOT / "clients" / "desktop"
if str(CLIENTS) not in sys.path:
    sys.path.insert(0, str(CLIENTS))

DEFAULT_OUT = os.path.join("runtime", "evidence", "fleet_status.txt")


def _collect(agent, controller) -> dict:
    """Everything the transcript needs, from the agent's own surfaces."""
    try:
        status = agent.get_status() or {}
    except Exception:
        status = {}
    try:
        peers = list(((agent.telemetry or {}).get("mesh_peers") or []))
    except Exception:
        peers = []
    lease = ""
    try:
        lease = str(getattr(agent, "mesh_election", None).primary() or "")
    except Exception:
        lease = ""
    try:
        mesh = dict(controller.snapshot().get("mesh") or {})
    except Exception:
        mesh = {}
    # Durability beats a session delta: this is the persisted `shared_from` provenance, so it
    # still says "memory crossed the mesh" on a fleet that has already converged.
    shared = 0
    for holder, attribute in ((getattr(agent, "user_memory", None), "facts"),
                              (getattr(agent, "memory", None), "facts"),
                              (getattr(getattr(agent, "memory", None), "tier2", None), "")):
        store = getattr(holder, attribute, None) if attribute else holder
        counter = getattr(store, "count_shared", None)
        if not callable(counter):
            continue
        try:
            shared = int(counter() or 0)
        except Exception:
            shared = 0
        if shared:
            break
    return {"role": str(status.get("mesh_role") or ""), "peers": peers, "lease": lease,
            "mesh": mesh, "sync": dict(controller.sync_state), "shared": shared,
            "node_id": str(getattr(agent, "node_id", "") or "")}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=45.0,
                    help="how long to listen to the hive before writing the transcript")
    ap.add_argument("--sync-interval", type=float, default=15.0,
                    help="seconds between memory syncs (a 1s floor applies; the durable "
                         "shared-fact count is read regardless of syncing)")
    ap.add_argument("--data-dir", default=os.path.join("runtime", "desktop"),
                    help="the node's data dir: it holds the identity AND the fleet token, "
                         "which is why the desk node's own dir is the default")
    ap.add_argument("--device-caps", default="desktop")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--priority", type=int, default=1,
                    help="the hub's documented priority: the PC runs the model and holds "
                         "the gate, the Mac is 10, phones are 500")
    ap.add_argument("--peers", default="")
    ap.add_argument("--mesh-token", default="")
    ap.add_argument("--out", default=DEFAULT_OUT)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    started = time.time()
    controller = None
    error = ""
    listened = 0.0
    try:
        import shugocore_desktop as console      # the node the operator terminal builds
        spec = console.backend_by_label("Stub (offline)")
        controller = console.AgentController(interval=1.0,
                                             sync_interval=max(1.0, args.sync_interval))
        mesh = {"port": args.port, "peers": args.peers, "priority": args.priority,
                "token": args.mesh_token or os.environ.get("SHUGOCORE_MESH_TOKEN", "")}
        error = controller.start(spec, spec.get("url", ""), "",
                                 data_dir=args.data_dir,
                                 device_caps=args.device_caps, mesh=mesh) or ""
        # The node configures the root logger as it boots; the transcript is the evidence, so
        # it stays curated instead of carrying every INFO line two or three times.
        import logging
        logging.getLogger().setLevel(logging.WARNING)
        deadline = time.monotonic() + max(1.0, args.seconds)
        while not error and time.monotonic() < deadline:
            time.sleep(1.0)
            listened += 1.0
        collected = _collect(controller.agent, controller) if not error else {}
        collected.update({"started": started, "seconds": listened, "port": args.port,
                          "priority": args.priority})
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        collected = {"started": started, "seconds": listened, "port": args.port,
                     "priority": args.priority}
    finally:
        try:
            if controller is not None:
                controller.stop()
        except Exception:
            pass
    lines = status_lines(node_id=str(collected.pop("node_id", "")
                                     or f"shugo-{args.device_caps}"),
                         error=error, **collected)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    for line in lines:
        print(line, flush=True)
    return 1 if error else 0


def human_bytes(value) -> str:
    """Bytes, as an operator reads them."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(number) < 1024.0 or unit == "TiB":
            return f"{number:.2f} {unit}" if unit != "B" else f"{int(number)} B"
        number /= 1024.0
    return f"{number:.2f} TiB"


def status_lines(*, node_id, port, priority, role, lease, peers, mesh, sync,
                 shared=0, started=None, seconds=0.0, error="") -> list:
    """The transcript, as lines. Pure, so a test can read it with the real parsers.

    Order matters to the parsers, and deliberately: the *first* ``role=`` is this node's own,
    and the *first* ``imported=`` is the fact count that matters, so both are stated before
    anything else that could confuse them.
    """
    lines = []
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started or time.time()))
    lines.append(f"fleet status: {stamp}  node={node_id or 'unknown'}  "
                 f"port={port}  priority={priority}  window={seconds:.0f}s")
    if error:
        lines.append(f"error={error}")
    lines.append(f"hub role={role or 'unknown'} lease={lease or 'nobody'} "
                 f"peers={len(peers or [])}")
    for peer in peers or []:
        name = peer.get("device_id") or peer.get("node_id") or "?"
        lines.append(
            "peer {name} prio={prio} thermal={thermal} mem_available={mem} "
            "({mem_h}) paired={paired} can_speak={speaks} source={source}".format(
                name=name,
                prio=peer.get("priority", "?"),
                thermal=peer.get("thermal_status", "?"),
                mem=peer.get("mem_available_bytes", 0),
                mem_h=human_bytes(peer.get("mem_available_bytes", 0)),
                paired=peer.get("paired", "?"),
                speaks=peer.get("can_speak", "?"),
                source=peer.get("source", "?")))
    if mesh:
        beat = mesh.get("heartbeat") or {}
        lines.append("mesh stats: rx={rx} tx={tx} errors={errors} connected={connected} "
                     "configured={configured}".format(
                         rx=beat.get("rx"), tx=beat.get("tx"),
                         errors=(mesh.get("stats") or {}).get("errors"),
                         connected=mesh.get("connected_count"),
                         configured=len(mesh.get("configured_peers") or [])))
    if sync or shared:
        # The fact count the claim is about, and the *durable* one comes first: what this node
        # holds from its peers (`shared_from` provenance, persisted), not what this process
        # happened to pull. A converged fleet imports nothing in a session and is still a fleet
        # whose memory crossed the mesh -- the client's own sync comment says so, and reading a
        # session delta as the claim would fail the second run of every healthy hive.
        session = int((sync or {}).get("imported") or 0)
        runtime_total = int(((mesh or {}).get("stats") or {}).get("imported") or 0)
        facts = int(shared or 0) or max(session, runtime_total)
        lines.append(f"memory across the mesh: imported={facts} "
                     f"(durable {int(shared or 0)}, this session {session}, "
                     f"runtime total {runtime_total}, rounds {(sync or {}).get('rounds', 0)}, "
                     f"failed {(sync or {}).get('failed', 0)})")
    lines.append(f"verdict: role={role or 'unknown'} peers={len(peers or [])} "
                 f"lease={lease or 'nobody'}")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())

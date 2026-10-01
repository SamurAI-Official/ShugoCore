"""Diagnostic: boot two host nodes in-process and inspect the election state.

    python runtime/tools/heartbeat_probe.py

Boots two real agents (the same ``create_agent`` the launcher uses) clustered on
loopback, waits past two heartbeat intervals, then prints each node's transport
heartbeat counters, the ``telemetry['mesh_peers']`` it merged, and the election
verdict -- the three places a mesh heartbeat can silently die.
"""
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from shugocore_agent import create_agent  # noqa: E402


def boot(caps, port, peers, priority):
    os.environ["SHUGOCORE_MESH_PORT"] = str(port)
    os.environ["SHUGOCORE_MESH_PEERS"] = peers
    os.environ.pop("SHUGOCORE_MESH_TOKEN", None)
    data_dir = REPO / "runtime" / f"probe_{caps}"
    data_dir.mkdir(parents=True, exist_ok=True)
    return create_agent(device_caps=caps, data_dir=str(data_dir),
                        mesh_node_id=f"shugo-{caps}", mesh_priority=priority)


def report(label, agent):
    runtime = getattr(agent, "shugonet_runtime", None)
    print(f"--- {label} ---")
    print("  runtime heartbeat:", None if runtime is None else runtime.status()["heartbeat"])
    print("  runtime stats:", None if runtime is None else {
        k: v for k, v in runtime.status()["stats"].items()
        if "heartbeat" in k})
    peers = (agent.telemetry or {}).get("mesh_peers")
    print("  telemetry mesh_peers:", peers)
    election = getattr(agent, "mesh_election", None)
    if election is None:
        print("  election: None")
    else:
        print("  election live_peers:", [p.get("node_id") for p in election.live_peers()])
        print("  election tick:", election.tick())
        print("  role:", election.role())
    try:
        print("  role label:", agent._mesh_role_label())
    except Exception as exc:
        print("  role label failed:", exc)


def main():
    a = boot("probea", 9021, "shugo-probeb=127.0.0.1:9022", 50)
    b = boot("probeb", 9022, "shugo-probea=127.0.0.1:9021", 10)
    print("booted both nodes; waiting 25s for two heartbeat intervals...")
    time.sleep(25)
    print("ticking both nodes so _mesh_heartbeat_tick observes the merged peers")
    for _ in range(3):
        for agent in (a, b):
            try:
                agent.tick()
            except Exception as exc:
                print(f"  tick failed for {agent.device_caps}: {exc}")
        time.sleep(6)
    report("A (prio 50, expect follower)", a)
    report("B (prio 10, expect primary)", b)
    for agent in (a, b):
        runtime = getattr(agent, "shugonet_runtime", None)
        if runtime is not None:
            runtime.stop()
        cleanup = getattr(agent, "cleanup", None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

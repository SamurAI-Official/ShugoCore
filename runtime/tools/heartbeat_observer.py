"""Observe the live fleet's heartbeats: what do the running nodes advertise?

    python runtime/tools/heartbeat_observer.py [id=host:port,id=host:port]

Boots a throwaway node that peers with the given nodes, ticks a few times, and
prints the advertisements it received plus its own election verdict -- the
external view that separates a broken *producer* (nothing arrives, or arrives
with zero headroom) from a broken *consumer* (arrives but is not merged).
"""
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from shugocore_agent import create_agent  # noqa: E402

DEFAULT_PEERS = ("shugo-testlab-a=127.0.0.1:9011,"
                 "shugo-testlab-b=127.0.0.1:9012")


def main(argv):
    os.environ["SHUGOCORE_MESH_PORT"] = "9013"
    os.environ["SHUGOCORE_MESH_PEERS"] = argv[0] if argv else DEFAULT_PEERS
    os.environ.pop("SHUGOCORE_MESH_TOKEN", None)
    data_dir = REPO / "runtime" / "probe_observer"
    data_dir.mkdir(parents=True, exist_ok=True)
    agent = create_agent(device_caps="observer", data_dir=str(data_dir),
                         mesh_node_id="shugo-observer", mesh_priority=20)
    for _ in range(6):
        try:
            agent.tick()
        except Exception as exc:
            print("tick failed:", exc)
        time.sleep(8)

    runtime = agent.shugonet_runtime
    print("transport heartbeat:", json.dumps(runtime.status()["heartbeat"]))
    print("stats:", json.dumps({k: v for k, v in runtime.status()["stats"].items()
                                if "heartbeat" in k}))
    print("advertisements received:")
    print(json.dumps(runtime.heartbeat_snapshot(), indent=2, default=str))
    print("telemetry mesh_peers:")
    print(json.dumps((agent.telemetry or {}).get("mesh_peers"), default=str))
    print("election:", json.dumps(agent.mesh_election.tick(), default=str))
    print("role:", agent._mesh_role_label())
    runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

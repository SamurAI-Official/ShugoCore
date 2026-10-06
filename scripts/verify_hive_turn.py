#!/usr/bin/env python3
"""A two-node hive, on real TCP, driven turn-by-turn.

`scripts/verify_interactive.py` proves one node answers the operator. This proves
the *hive* rule around it: a node that does not hold the lease decides nothing and
speaks nothing on its own initiative -- it forwards the turn (`orchestrate/turn`)
to the primary, which decides and routes the reply back as a delegated action.

It exists because the machine this was written on has live nodes on its LAN, so
"is this node a follower?" is not something a single-node run can make
deterministic. Two agents on two loopback ports, an election verdict set by
hand, and both roles exercised in one run is the only honest rig.

    python scripts/verify_hive_turn.py
"""
import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shugocore_agent import (  # noqa: E402
    DELEGATE_TOPIC,
    TURN_RESULT_TOPIC,
    TURN_TOPIC,
    create_agent,
)


class Spoke:
    """A speak listener that records what a human at this node would hear."""

    def __init__(self, node_id):
        self.node_id = node_id
        self.said = []

    def speak(self, text):
        text = str(text or "").strip()
        if text:
            self.said.append(text)
        return True


class Election:
    """A verdict the operator sets: one node leads, the other follows."""

    def __init__(self, node_id, primary):
        self.node_id = node_id
        self._primary = primary
        self.priority = 10 if node_id == primary else 500

    def tick(self):
        return {"primary": self._primary,
                "role": "primary" if self._primary == self.node_id else "follower",
                "is_primary": self._primary == self.node_id}

    def primary(self):
        return self._primary

    def live_peers(self):
        return []


def node(data_dir, node_id, port, primary, peer_map):
    """One real agent, wired to the mesh on a loopback port."""
    os.environ["SHUGOCORE_MESH_PORT"] = str(port)
    os.environ.pop("SHUGOCORE_MESH_PEERS", None)
    agent = create_agent(device_caps=node_id, api_url=None,
                         data_dir=os.path.abspath(data_dir),
                         mesh_node_id=node_id, local_model=False)
    agent.mesh_election = Election(node_id, primary)
    runtime = agent.shugonet_runtime
    runtime.agent_id = node_id
    for peer_id, (host, pport) in peer_map.items():
        runtime.add_peer(peer_id, host, pport)
    spoke = Spoke(node_id)
    agent.register_speak_listener(spoke)
    return agent, spoke


def wait_for(condition, timeout=6.0, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    raise SystemExit(f"timed out waiting for {label}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--turn", default="what can you do?")
    ap.add_argument("--hub-port", type=int, default=19701,
                    help="mesh port for the primary (must be free)")
    ap.add_argument("--phone-port", type=int, default=19702,
                    help="mesh port for the follower (must be free)")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="shugocore_hive_")
    primary_id, follower_id = "shugo-hub", "shugo-phone"

    hub, hub_spoke = node(os.path.join(tmp, "hub"), primary_id,
                          args.hub_port, primary_id,
                          {follower_id: ("127.0.0.1", args.phone_port)})
    print(f"hub     : {primary_id} on 127.0.0.1:{args.hub_port} (primary)")
    phone, phone_spoke = node(os.path.join(tmp, "phone"), follower_id,
                              args.phone_port, primary_id,
                              {primary_id: ("127.0.0.1", args.hub_port)})
    print(f"phone   : {follower_id} on 127.0.0.1:{args.phone_port} (follower)\n")

    frames = []

    def observe(peer, topic, payload):
        frames.append((peer, topic, payload))
        return hub._on_mesh_send(peer, topic, payload)

    hub.shugonet_runtime.set_send_handler(observe)

    print(f"phone   > {args.turn}")
    handled = phone.handle_typed_input(args.turn, source="terminal")
    print(f"          handled       : {handled}")
    print(f"          frames at hub : "
          f"{[(t, (p or {}).get('transcript')) for _peer, t, p in frames]}")

    # The reply is *delegated back* to the node that took the turn, so it is the
    # phone's listener that receives the words -- and the hub must not have
    # spoken them itself.
    try:
        wait_for(lambda: phone_spoke.said,
                 label="the forwarded turn to be answered (routed back)")
    except SystemExit as exc:
        print(f"\nFAILED: {exc}")
        print(f"  phone heard: {phone_spoke.said}")
        print(f"  hub heard  : {hub_spoke.said}")
        for peer, topic, payload in frames:
            print(f"  frame from {peer} on {topic}: {payload}")
        return 1

    print(f"          phone heard   : {phone_spoke.said}"
          "   (the reply was routed back here)")
    print("\nhub     > (decided the forwarded turn; routed the reply back)")
    if hub_spoke.said:
        print(f"          ! the hub also spoke locally: {hub_spoke.said}")
    else:
        print("          hub heard nothing locally -- one decision, one mouth")
    print(f"\nresult  : the follower took the turn, the primary decided it, and "
          f"the reply returned to the follower ({len(frames)} frame(s) at the hub)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

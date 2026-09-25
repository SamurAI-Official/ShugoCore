#!/usr/bin/env python3
"""Live two-agent ShugoNet memory-sharing smoke.

Proves the combined-memory contract with real TCP and real SQLite memory:
two agents each learn something, sync, and afterwards each agent's OWN
memory contains the other's knowledge and can answer a memory query from it
(no stubbed facts).

Modes
-----
1. Local (default): two agents on loopback, separate Tier 2 stores.

       python3 tests/two_agent_memory_smoke.py

2. Remote: pair a local agent with a running peer — e.g. an Android node
   reachable via ``adb forward tcp:19000 tcp:9000``. Seeds a local fact,
   queries the peer's memory, pulls the peer's Tier 2 via sync, then verifies
   that knowledge is usable from the local agent's own recall.

       python3 tests/two_agent_memory_smoke.py --remote 127.0.0.1:19000 \
           --peer-id shugo-device --query "favorite color"

   Against a token-protected peer, export the same secret here too
   (``SHUGOCORE_MESH_TOKEN``) -- a hardened node rejects every message that
   does not carry it, so without this the probe reports "timed out".

Exit code 0 = verdict STABLE, 1 = FAILED. Read-only with respect to the
process environment; all memory is written to a temp directory.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_runtime import ShugonetAgentRuntime  # noqa: E402
from memory_system import MemoryManager, SemanticMemory  # noqa: E402

FACT_A = "agent A knows the rooftop garden is on level nine"
FACT_B = "agent B knows the server room needs badge access"


def wait_bound(runtime: ShugonetAgentRuntime, timeout: float = 5.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        server = runtime._server
        if server is not None and server._server_sock is not None:
            return server._server_sock.getsockname()[1]
        time.sleep(0.01)
    raise SystemExit("shugonet server did not bind in time")


class Agent:
    """One mesh peer: MemoryManager + ShugonetAgentRuntime over real TCP."""

    def __init__(self, agent_id: str, tmpdir: str, port: int = 0):
        self.agent_id = agent_id
        self.memory = MemoryManager(
            agent_id=agent_id,
            semantic=SemanticMemory(db_path=os.path.join(tmpdir, f"{agent_id}.db")),
            auto_start=False)
        # A token-protected peer (SHUGOCORE_MESH_TOKEN) rejects every message
        # that does not carry the shared secret, so this side must present it
        # too -- otherwise --remote reports "request failed: timed out" against
        # a hardened node. Unset keeps the previous tokenless behaviour.
        self.runtime = ShugonetAgentRuntime(
            agent_id=agent_id, host="127.0.0.1", port=port, memory=self.memory,
            auth_token=os.environ.get("SHUGOCORE_MESH_TOKEN") or None)

    def start(self) -> int:
        self.runtime.start()
        return wait_bound(self.runtime)

    def learn(self, content: str) -> int:
        return self.memory.tier2.store_fact(content)

    def knows(self, content: str) -> bool:
        return self.memory.tier2.content_exists(content)

    def recall(self, query: str, top_k: int = 5):
        return self.memory.tier2.search(query, top_k=top_k, reinforce=False)

    def stop(self) -> None:
        self.runtime.stop()
        self.memory.shutdown()


def parse_remote(spec: str):
    host, _, port_s = str(spec).rpartition(":")
    return host.strip(), int(port_s)


def run_local(json_out: bool) -> int:
    """Two-agent loopback: each agent ends up knowing BOTH facts."""
    tmp = tempfile.mkdtemp(prefix="shugo-mesh-")
    checks = {}
    try:
        a = Agent("agent-a", tmp)
        b = Agent("agent-b", tmp)
        port_a = a.start()
        port_b = b.start()
        a.runtime.add_peer("agent-b", "127.0.0.1", port_b)
        b.runtime.add_peer("agent-a", "127.0.0.1", port_a)

        a.learn(FACT_A)
        b.learn(FACT_B)
        checks["isolated_before_sync"] = (not b.knows(FACT_A)
                                          and not a.knows(FACT_B))

        pull_b = b.runtime.sync("agent-a")
        pull_a = a.runtime.sync("agent-b")
        checks["sync_ok"] = (pull_b.get("status") == "success"
                             and pull_a.get("status") == "success")
        checks["b_imported_a_fact"] = b.knows(FACT_A)
        checks["a_imported_b_fact"] = a.knows(FACT_B)
        checks["b_memory_size"] = len(b.memory.tier2.facts_since(limit=100)) == 2
        checks["a_memory_size"] = len(a.memory.tier2.facts_since(limit=100)) == 2

        hits = b.recall("rooftop garden level nine")
        checks["b_recalls_peer_fact"] = any(
            "rooftop garden" in h["content"] for h in hits)

        replies = b.runtime.query("rooftop garden level nine", peers=["agent-a"])
        joined = " ".join(r["fact"] for rep in replies
                          for r in rep.get("results", []))
        checks["query_is_real_memory"] = "rooftop garden" in joined
        checks["query_not_stub"] = "stub" not in joined.lower()

        b.stop()
        a.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return _verdict("local-two-agent", checks, json_out)


def run_remote(remote: str, peer_id: str, query: str, json_out: bool) -> int:
    """Local agent vs a running peer (Android node via adb forward)."""
    host, port = parse_remote(remote)
    tmp = tempfile.mkdtemp(prefix="shugo-mesh-")
    checks = {}
    try:
        me = Agent("host-agent", tmp)
        me.start()
        me.runtime.add_peer(peer_id, host, port)
        me.learn("the host agent prefers decaf after 14:00")

        replies = me.runtime.query(query, peers=[peer_id])
        results = [r for rep in replies for r in rep.get("results", [])]
        joined = " ".join(r.get("fact", "") for r in results)
        checks["peer_reachable"] = bool(replies)
        checks["query_returned_facts"] = bool(results)
        checks["query_not_stub"] = "stub" not in joined.lower()

        pulled = me.runtime.sync(peer_id)
        checks["sync_ok"] = pulled.get("status") == "success"
        imported = int(pulled.get("imported", 0))
        checks["peer_memory_imported"] = imported > 0
        # Whatever the peer knew is now usable from OUR own memory.
        usable = False
        for fact in me.memory.tier2.facts_since(limit=200):
            if fact.get("metadata", {}).get("shared_from") == peer_id:
                usable = True
                break
        checks["imported_facts_usable_locally"] = usable

        if json_out:
            print(json.dumps({"imported": imported,
                              "query_results": len(results)}, indent=2))
        me.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return _verdict(f"remote-peer:{peer_id}", checks, json_out)


def _verdict(label: str, checks, json_out: bool) -> int:
    ok = all(bool(v) for v in checks.values())
    if json_out:
        print(json.dumps({"label": label, "verdict": "STABLE" if ok else "FAILED",
                          "checks": {k: bool(v) for k, v in checks.items()}},
                         indent=2, sort_keys=True))
    else:
        for name, value in checks.items():
            print(f"  [{'PASS' if value else 'FAIL'}] {name}")
        print(f"{label}: {'STABLE' if ok else 'FAILED'}")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Two-agent memory-sharing smoke.")
    ap.add_argument("--remote", help="peer host:port (omit for local two-agent mode)")
    ap.add_argument("--peer-id", default="remote-peer", help="peer identifier")
    ap.add_argument("--query", default="favorite color",
                    help="memory question asked of the peer")
    ap.add_argument("--json", action="store_true", dest="json_out")
    args = ap.parse_args(argv)
    if args.remote:
        return run_remote(args.remote, args.peer_id, args.query, args.json_out)
    return run_local(args.json_out)


if __name__ == "__main__":
    raise SystemExit(main())